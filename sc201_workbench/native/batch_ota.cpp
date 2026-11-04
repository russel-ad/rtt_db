#include "batch_ota.h"
#include "security/Credential.h"
#include "security/DeviceSerial.h"
#include <QCryptographicHash>
#include <QFile>
#include <QSet>
#include <QTimer>
#include <QVariant>
#include <stdexcept>

RttOtaImage RttOtaImage::inspect(const QString &path, const QString &version)
{
    if (version.trimmed().isEmpty()) throw std::runtime_error("请填写升级后应报告的固件版本。");
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() < 1 || file.size() > 2 * 1024 * 1024)
        throw std::runtime_error("固件不可读或长度不在 1 B～2 MiB 范围内。");
    const auto bytes = file.readAll();
    if (bytes.size() != file.size()) throw std::runtime_error("固件读取不完整。");
    QStringList models;
    for (const auto &candidate : {QStringLiteral("SC200-D"), QStringLiteral("SC201-S"), QStringLiteral("SC202-S")}) {
        if (!bytes.contains(security::firmwareModelMarker(candidate))) continue;
        models.append(candidate);
    }
    if (models.isEmpty()) throw std::runtime_error("固件缺少准确的 SC200-D / SC201-S / SC202-S 型号标记。");
    return {file.fileName(), version.trimmed(), models.join(" / "), QCryptographicHash::hash(bytes, QCryptographicHash::Sha256), models};
}

bool RttOtaImage::unchanged() const
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() < 1 || file.size() > 2 * 1024 * 1024) return false;
    return QCryptographicHash::hash(file.readAll(), QCryptographicHash::Sha256) == sha256;
}

QString rttOtaResultText(RttOtaResult result)
{
    switch (result) {
    case RttOtaResult::Queued: return QStringLiteral("排队");
    case RttOtaResult::Running: return QStringLiteral("进行中");
    case RttOtaResult::Success: return QStringLiteral("成功（已验版）");
    case RttOtaResult::Failed: return QStringLiteral("失败");
    case RttOtaResult::Unknown: return QStringLiteral("未确认");
    case RttOtaResult::Cancelled: return QStringLiteral("已取消");
    case RttOtaResult::NeedsBinding: return QStringLiteral("需要绑定");
    }
    return {};
}

RttBatchOtaController::RttBatchOtaController(QObject *parent) : QObject(parent) { m_clock.start(); }

RttBatchOtaController::~RttBatchOtaController()
{
    ++m_generation; m_active = false;
    // Destroy tasks while controller fields and callback guards still exist,
    // before QObject's child cleanup runs after member destruction.
    const auto tasks = findChildren<RttOtaTask *>(QString(), Qt::FindDirectChildrenOnly);
    for (auto *task : tasks) { task->disconnect(this); delete task; }
    m_jobs.clear(); m_factory = {};
}

bool RttBatchOtaController::begin(const QVector<RttOtaDevice> &devices, const RttOtaImage &image,
                                int concurrency, Factory factory)
{
    if (m_active || devices.isEmpty() || concurrency < 1 || concurrency > 4 || !factory || !image.unchanged()) return false;
    QSet<QString> addresses;
    for (const auto &device : devices) {
        const auto address = device.info.address().toString();
        if (device.info.address().isNull() || addresses.contains(address)) return false;
        addresses.insert(address);
    }
    ++m_generation;
    m_rows.clear(); m_jobs.clear(); m_prepared.clear(); m_preparing = -1; m_waveTransferring = false;
    m_image = image; m_limit = concurrency; m_factory = std::move(factory);
    for (const auto &device : devices) {
        RttOtaRow row; row.device = device; row.stage = QStringLiteral("等待空闲升级通道");
        m_rows.append(row);
    }
    m_active = true;
    emit activeChanged(true); emit changed();
    QTimer::singleShot(0, this, &RttBatchOtaController::pump);
    return true;
}

void RttBatchOtaController::pump()
{
    if (!m_active || m_pumping) return;
    // Do not open another GATT connection while a peer is being discovered
    // or authenticated. Subsequent waves wait for all previous verdicts.
    if (m_preparing >= 0 || (m_waveTransferring && !m_jobs.isEmpty())) return;
    if (m_jobs.isEmpty()) m_waveTransferring = false;
    m_pumping = true;
    const auto generation = m_generation;
    for (int index = 0; index < m_rows.size() && m_jobs.size() < m_limit; ++index) {
        auto &row = m_rows[index];
        if (row.result != RttOtaResult::Queued) continue;
        row.result = RttOtaResult::Running;
        row.startedMs = m_clock.elapsed();
        row.stage = QStringLiteral("正在连接 / App 加密认证");
        RttOtaTask *task = nullptr;
        try { task = m_factory(row.device, m_image, this); }
        catch (const std::exception &error) { row.detail = QString::fromUtf8(error.what()); }
        if (!task) {
            row.result = RttOtaResult::Failed; row.stage = QStringLiteral("未能建立独立会话");
            row.finishedMs = m_clock.elapsed();
            continue;
        }
        m_jobs.insert(index, task);
        const bool held = m_limit >= 3 && task->supportsPreparedTransfer();
        if (held) { m_preparing = index; task->setProperty("rttHoldTransfer", true); }
        auto valid = [this, generation, index, task]() {
            return generation == m_generation && m_jobs.value(index) == task;
        };
        connect(task, &RttOtaTask::prepared, this, [this, index, valid]() {
            if (!valid() || m_preparing != index) return;
            m_preparing = -1; m_prepared.insert(index);
            m_rows[index].stage = QStringLiteral("App 已认证，等待本批设备连接完成");
            emit changed();
            QTimer::singleShot(0, this, &RttBatchOtaController::pump);
        });
        connect(task, &RttOtaTask::transferStarted, this, [this, index, valid]() {
            if (!valid() || m_rows[index].transferStartedMs >= 0) return;
            m_rows[index].transferStartedMs = m_clock.elapsed(); emit changed();
        });
        connect(task, &RttOtaTask::transferStopped, this, [this, index, valid]() {
            if (!valid() || m_rows[index].transferStartedMs < 0 || m_rows[index].transferFinishedMs >= 0) return;
            m_rows[index].transferFinishedMs = m_clock.elapsed(); emit changed();
        });
        connect(task, &RttOtaTask::stage, this, [this, index, valid](const QString &stage) {
            if (!valid()) return; m_rows[index].stage = stage; emit changed();
        });
        connect(task, &RttOtaTask::identity, this, [this, index, valid](const QString &model, const QString &serial, const QString &version) {
            if (!valid()) return;
            auto &row = m_rows[index]; row.model = model; row.serial = serial; row.currentVersion = version; emit changed();
        });
        connect(task, &RttOtaTask::progress, this, [this, index, valid](quint32 done, quint32 total) {
            if (!valid()) return;
            m_rows[index].done = qMin(done, total); m_rows[index].total = total; emit changed();
        });
        connect(task, &RttOtaTask::logLine, this, [this, index, valid](const QString &line) {
            if (valid()) emit logLine(QStringLiteral("[batch %1] %2").arg(m_rows[index].device.info.address().toString(), line));
        });
        connect(task, &RttOtaTask::finished, this, [this, index, task, valid](RttOtaResult result, const QString &detail) {
            if (!valid()) return;
            if (result == RttOtaResult::Queued || result == RttOtaResult::Running) result = RttOtaResult::Unknown;
            m_jobs.remove(index);
            m_prepared.remove(index);
            if (m_preparing == index) m_preparing = -1;
            auto &row = m_rows[index]; row.result = result; row.detail = detail;
            row.stage = rttOtaResultText(result); row.finishedMs = m_clock.elapsed();
            if (row.transferStartedMs >= 0 && row.transferFinishedMs < 0) row.transferFinishedMs = row.finishedMs;
            if (result == RttOtaResult::Success) { row.currentVersion = m_image.version; row.done = row.total; }
            task->deleteLater(); emit changed();
            QTimer::singleShot(0, this, &RttBatchOtaController::pump);
        });
        task->start();
        if (held && m_preparing >= 0) break;
    }
    bool queued = false;
    for (const auto &row : m_rows) if (row.result == RttOtaResult::Queued) { queued = true; break; }
    if (m_preparing < 0 && !m_prepared.isEmpty() && (m_jobs.size() >= m_limit || !queued)) {
        // All available peers in this wave have completed App3 authentication
        // before any receiver enters its high-throughput OTA connection mode.
        const auto ready = m_prepared;
        m_prepared.clear(); m_waveTransferring = true;
        emit logLine(QStringLiteral("[batch-open] prepared=%1; releasing concurrent transfers").arg(ready.size()));
        for (int index = 0; index < m_rows.size(); ++index) if (ready.contains(index)) {
            if (auto task = m_jobs.value(index)) task->releasePreparedTransfer();
        }
    }
    m_pumping = false;
    emit changed();
    settle();
}

void RttBatchOtaController::settle()
{
    if (!m_active || !m_jobs.isEmpty()) return;
    for (const auto &row : m_rows) if (row.result == RttOtaResult::Queued) return;
    m_active = false; m_factory = {};
    emit activeChanged(false); emit changed();
}

void RttBatchOtaController::cancelRows(const QVector<int> &indices)
{
    for (const int index : indices) {
        if (index < 0 || index >= m_rows.size()) continue;
        auto &row = m_rows[index];
        if (row.result == RttOtaResult::Queued) {
            row.result = RttOtaResult::Cancelled; row.stage = QStringLiteral("未连接，已移出队列");
            row.detail = QStringLiteral("尚未执行设备操作"); row.finishedMs = m_clock.elapsed();
        }
    }
    for (const int index : indices) if (auto task = m_jobs.value(index)) task->cancel();
    emit changed(); settle();
}

void RttBatchOtaController::cancelAll()
{
    QVector<int> indices;
    for (int i = 0; i < m_rows.size(); ++i) indices.append(i);
    cancelRows(indices);
}

void RttBatchOtaController::setDetailedLogging(bool enabled)
{
    m_image.compactLog = !enabled;
    for (auto task : m_jobs) if (task) task->setDetailedLogging(enabled);
}

qint64 RttBatchOtaController::elapsedMs(const RttOtaRow &row) const
{
    return row.startedMs < 0 ? 0 : qMax<qint64>(0, (row.finishedMs < 0 ? m_clock.elapsed() : row.finishedMs) - row.startedMs);
}

qint64 RttBatchOtaController::transferElapsedMs(const RttOtaRow &row) const
{
    return row.transferStartedMs < 0 ? 0 : qMax<qint64>(0,
        (row.transferFinishedMs < 0 ? m_clock.elapsed() : row.transferFinishedMs) - row.transferStartedMs);
}
