#include "batch_ota_task.h"
#include <QVariant>
#include "MlinkSession.h"
#include "BleLink.h"
#include "BleWinrtGatt.h"
#include "workflows/AppController.h"
#include "security/DeviceSerial.h"
#include <QVariant>

RttDeviceOtaTask::RttDeviceOtaTask(const RttOtaDevice &device, const RttOtaImage &image,
                                 security::ServerConfig config, const QString &cache, QObject *parent)
    : RttDeviceOtaTask(device, image, std::make_unique<security::ServerBackend>(config, cache),
                       std::make_unique<MlinkSession>(), config.account(), cache, !config.authorization.isEmpty(), parent) {}

RttDeviceOtaTask::RttDeviceOtaTask(const RttOtaDevice &device, const RttOtaImage &image,
                                 std::unique_ptr<security::AppBackend> backend, std::unique_ptr<MlinkSession> session,
                                 const QString &account, const QString &cache, bool allowServer, QObject *parent)
    : RttOtaTask(parent), m_backend(std::move(backend)), m_session(std::move(session)),
      m_device(device), m_image(image), m_account(account), m_allowServer(allowServer)
{
    setDetailedLogging(!m_image.compactLog);
    m_session->setProperty("rttFitOtaToAtt", m_image.fitAtt);
    m_session->setProperty("rttOtaEventDrive", m_image.eventDriven);
    if (auto *server = dynamic_cast<security::ServerBackend *>(m_backend.get()))
        connect(server, &security::ServerBackend::logLine, this, &RttOtaTask::logLine);
    connect(m_session.get(), &MlinkSession::logLine, this, &RttOtaTask::logLine);
    // Install this before AppController so a model absent from the actual
    // image is rejected before a credential fetch, including shared images.
    connect(m_session.get(), &MlinkSession::publicIdentityReady, this, [this](const QString &model, const QString &) {
        if (!m_done && !m_image.models.contains(model))
            finish(RttOtaResult::Failed, QStringLiteral("固件未声明支持此设备型号，未请求凭据或开始升级。"));
    });
    m_app = std::make_unique<AppController>(*m_session, *m_backend, cache);
    connect(m_app.get(), &AppController::stageChanged, this, [this](const QString &detail) {
        if (!m_done) { emit stage(QStringLiteral("App 连接与认证：%1").arg(detail)); emit logLine(detail); }
    });
    connect(m_app.get(), &AppController::bindingRequired, this, [this](const QString &) {
        if (m_done) return;
        if (m_device.bindingApproved) {
            m_device.bindingApproved = false; // One explicit continuation, never an automatic loop.
            emit stage(QStringLiteral("已确认绑定窗口，按原流程认领并连接 App"));
            if (!m_app->continueBinding() && !m_done) finish(RttOtaResult::Failed, QStringLiteral("绑定未能继续，请检查设备窗口和服务器。"));
        } else finish(RttOtaResult::NeedsBinding, QStringLiteral("请在设备上打开绑定窗口，选中此结果行后点击“绑定窗口已打开，继续选中设备”。未自动发起认领。"));
    });
    connect(m_session.get(), &MlinkSession::deviceInfo, this,
            [this](const QString &model, const QString &serial, const QString &version, const QString &) {
        if (m_done) return; m_model = model; m_serial = serial; m_version = version;
        emit identity(model, serial, version);
    });
    connect(m_app.get(), &AppController::finished, this, [this](bool ok, const QString &detail) {
        if (m_done || m_otaStarted) return;
        if (!ok) { finish(m_cancelled ? RttOtaResult::Cancelled : RttOtaResult::Failed, detail); return; }
        m_prepared = true;
        if (property("rttHoldTransfer").toBool()) {
            m_deadline.stop();
            emit logLine(QStringLiteral("[batch-open] App3 ready; holding OTA until peer preparation completes"));
            emit prepared();
        } else releasePreparedTransfer();
    }, Qt::QueuedConnection);
    connect(m_session.get(), &MlinkSession::otaOperationStarted, this, [this]() {
        if (!m_done) { emit transferStarted(); emit stage(QStringLiteral("查询升级目标 / 加密传输")); }
    });
    connect(m_session.get(), &MlinkSession::otaProgress, this, [this](int, int, quint32 done, quint32 total) {
        if (!m_done) emit progress(done, total);
    });
    connect(m_session.get(), &MlinkSession::otaRebooted, this, [this](const QString &) {
        if (m_done) return; m_rebooted = true;
        emit transferStopped(); preferThroughput(false);
        emit stage(QStringLiteral("已发送重启，正在重连、认证并校验版本"));
    });
    connect(m_session.get(), &MlinkSession::otaFinished, this, [this](bool ok, const QString &detail) {
        if (m_done || !m_otaStarted) return;
        if (ok) { finish(RttOtaResult::Success, QStringLiteral("重启后重新认证，已确认版本 %1").arg(m_image.version)); return; }
        const bool aborted = m_cancelled && detail.contains("abort", Qt::CaseInsensitive);
        const bool unknown = detail.startsWith("UNKNOWN") || (aborted && m_rebooted);
        finish(unknown ? RttOtaResult::Unknown : aborted ? RttOtaResult::Cancelled : RttOtaResult::Failed,
               aborted ? QStringLiteral("%1；已停止本机任务，请按设备实际版本判断，可能已收到固件。").arg(detail) : detail);
    }, Qt::QueuedConnection);
    m_deadline.setSingleShot(true); m_deadline.setInterval(90000);
    connect(&m_deadline, &QTimer::timeout, this, [this]() { finish(RttOtaResult::Failed, QStringLiteral("App 连接或认证超过 90 秒，已释放本机连接。")); });
    m_drain.setInterval(50);
    connect(&m_drain, &QTimer::timeout, this, &RttDeviceOtaTask::tryOta);
}

RttDeviceOtaTask::~RttDeviceOtaTask()
{
    m_done = true; m_deadline.stop(); m_drain.stop();
    preferThroughput(false);
    m_app->cancel();
}

void RttDeviceOtaTask::setDetailedLogging(bool enabled)
{
    m_image.compactLog = !enabled;
    m_session->setProperty("rttCompactOtaLog", !enabled);
}

void RttDeviceOtaTask::preferThroughput(bool enabled)
{
    if (auto *gatt = m_session->ble()->findChild<BleWinrtGatt *>()) gatt->setThroughputPreferred(enabled);
}

void RttDeviceOtaTask::start()
{
    if (m_started || m_done) return;
    m_started = true; m_deadline.start();
    if (!m_image.unchanged()) { finish(RttOtaResult::Failed, QStringLiteral("固件文件已变化，请重新选择后开始新批次。")); return; }
    const auto type = m_image.models.size() == 1 ? security::credentialType(m_image.models.first()) : QString();
    const bool started = m_app->connectAutomatic({type, {}}, m_account, m_device.info, m_allowServer);
    if (!started && !m_done) finish(RttOtaResult::Failed, QStringLiteral("无法开始 App 连接，请检查设备、账号和原有缓存。"));
}

void RttDeviceOtaTask::releasePreparedTransfer()
{
    if (m_done || !m_prepared || m_otaStarted) return;
    m_prepared = false;
    m_wait.start(); m_drain.start(); tryOta();
}

void RttDeviceOtaTask::tryOta()
{
    if (m_done || m_otaStarted) return;
    if (m_cancelled) { finish(RttOtaResult::Cancelled, QStringLiteral("开始传输前已取消。")); return; }
    if (!m_image.models.contains(m_model) || m_serial.isEmpty() || !m_session->authenticated() || !m_app->prepareOtaVerification()) {
        finish(RttOtaResult::Failed, QStringLiteral("认证型号/SN或 App3 权限不匹配，未开始升级。")); return;
    }
    if (m_session->receiptDrain() != MLINK_OK) {
        if (m_wait.elapsed() > 10000) finish(RttOtaResult::Failed, QStringLiteral("认证后发送队列未能排空，未开始升级。"));
        return;
    }
    if (!m_image.unchanged()) { finish(RttOtaResult::Failed, QStringLiteral("排队期间固件文件已变化，未开始升级。")); return; }
    m_drain.stop(); m_deadline.stop(); m_otaStarted = true;
    preferThroughput(m_image.preferThroughput);
    emit logLine(QStringLiteral("[ble-perf] OTA throughput_preferred=%1 compact_log=%2; single outstanding ATT write")
        .arg(m_image.preferThroughput).arg(m_image.compactLog));
    if (!m_session->startOta(m_image.path, m_model, m_image.version) && !m_done)
        finish(RttOtaResult::Failed, QStringLiteral("原 OTA 校验未通过或升级未能开始；详见通信日志。"));
}

void RttDeviceOtaTask::cancel()
{
    if (m_done) return;
    m_cancelled = true; m_drain.stop(); m_deadline.stop();
    if (m_session->otaBusy() || m_session->otaVerifying()) {
        emit stage(QStringLiteral("正在取消；已进入重启提交时需等待原流程收尾"));
        m_session->abortOta();
        // The original job may refuse cancellation after reboot commit. Keep
        // its buffers/session and scheduler slot until its final callback.
        return;
    }
    if (m_otaStarted) {
        // A native terminal signal may already be queued. Let its verified
        // verdict win over a click that arrives between signal and delivery.
        emit stage(QStringLiteral("正在接收本轮最终结果"));
        return;
    }
    finish(RttOtaResult::Cancelled, QStringLiteral("已取消并断开本机连接。"));
}

void RttDeviceOtaTask::finish(RttOtaResult result, const QString &detail)
{
    if (m_done) return;
    m_done = true; m_deadline.stop(); m_drain.stop();
    emit transferStopped(); preferThroughput(false);
    m_app->cancel();
    emit finished(result, detail);
}
