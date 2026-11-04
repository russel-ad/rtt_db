#include "batch_ota_panel.h"
#include "batch_ota_task.h"
#include "MainWindow.h"
#include "SecurityPanel.h"
#include "MlinkSession.h"
#include "BleLink.h"
#include <QCheckBox>
#include <QFileDialog>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QProgressBar>
#include <QPushButton>
#include <QSaveFile>
#include <QSettings>
#include <QSignalBlocker>
#include <QSpinBox>
#include <QTableWidget>
#include <QTextStream>
#include <QVBoxLayout>
#include <QVariant>
#include <stdexcept>

namespace {
struct ConnectionContext {
    security::ServerConfig config;
    QString cache;
    ~ConnectionContext() { security::wipe(config.authorization); }
};
QString csvCell(QString value) {
    if (!value.isEmpty() && QStringLiteral("=+-@\t\r").contains(value.front())) value.prepend('\'');
    value.replace('"', "\"\"");
    return '"' + value + '"';
}
}

RttBatchOtaPanel::RttBatchOtaPanel(MainWindow &window, SecurityPanel &security,
                                 MlinkSession &session, QWidget *parent)
    : QGroupBox(QStringLiteral("多设备并发加密升级"), parent), m_window(window),
      m_security(security), m_session(session), m_controller(this)
{
    setObjectName("batchOtaPanel");
    m_controller.setObjectName("batchOtaController");
    auto *layout = new QVBoxLayout(this);
    auto *hint = new QLabel(QStringLiteral("在左侧蓝牙列表勾选设备，使用上方同一型号的固件和期望版本。请先断开单设备连接。3～4 台先依次完成 App 认证，再同时传输；每台分别重启与验版。"), this);
    hint->setWordWrap(true); layout->addWidget(hint);
    auto *actions = new QHBoxLayout;
    m_all = new QPushButton(QStringLiteral("勾选全部设备"), this); m_all->setObjectName("batchOtaCheckAll");
    m_none = new QPushButton(QStringLiteral("清空勾选"), this);
    m_concurrency = new QSpinBox(this); m_concurrency->setObjectName("batchOtaConcurrency");
    m_concurrency->setRange(1, 4); m_concurrency->setValue(2); m_concurrency->setSuffix(QStringLiteral(" 台"));
    m_concurrency->setToolTip(QStringLiteral("1 为顺序升级，2～4 为并发。通道包含连接、传输和重启验版；实际能力取决于电脑蓝牙适配器。"));
    m_start = new QPushButton(QStringLiteral("开始批量升级"), this); m_start->setObjectName("startBatchOta"); m_start->setProperty("primary", true);
    m_cancelSelected = new QPushButton(QStringLiteral("取消选中任务"), this); m_cancelSelected->setObjectName("cancelSelectedBatchOta");
    m_cancel = new QPushButton(QStringLiteral("停止全部"), this); m_cancel->setObjectName("cancelBatchOta");
    actions->addWidget(m_all); actions->addWidget(m_none); actions->addWidget(new QLabel(QStringLiteral("同时升级"), this));
    actions->addWidget(m_concurrency); actions->addWidget(m_start); actions->addStretch();
    actions->addWidget(m_cancelSelected); actions->addWidget(m_cancel); layout->addLayout(actions);
    m_throughput = new QCheckBox(QStringLiteral("吞吐优先（Windows 11）"), this);
    m_throughput->setObjectName("batchOtaThroughput"); m_throughput->setChecked(false);
    m_throughput->setToolTip(QStringLiteral("升级期间请求 Windows 吞吐优先参数，完成或取消后释放。系统可能保持原连接间隔，默认关闭；请对照实测后选择。生效情况见 [ble-perf] 日志。"));
    m_fitAtt = new QCheckBox(QStringLiteral("按蓝牙 MTU 缩小固件块"), this);
    m_fitAtt->setObjectName("batchOtaFitAtt"); m_fitAtt->setChecked(false);
    m_fitAtt->setToolTip(QStringLiteral("缩小固件块可减少跨 ATT 分包，但会增加逐块存储确认。当前 SC200-D 双台实测更慢，默认关闭；其他组合请对照实测后选择。"));
    auto *tuning = new QHBoxLayout;
    m_eventDrive = new QCheckBox(QStringLiteral("确认后立即推进"), this);
    m_eventDrive->setObjectName("batchOtaEventDrive"); m_eventDrive->setChecked(true);
    m_eventDrive->setToolTip(QStringLiteral("在同一通信线程继续推进已就绪状态，减少等待下一次 10 ms 轮询的空档。仍等待匹配的 ACK 和存储确认；关闭可对照原推进方式。"));
    tuning->addWidget(m_eventDrive); tuning->addWidget(m_fitAtt); tuning->addWidget(m_throughput); tuning->addStretch(); layout->addLayout(tuning);
    m_summary = new QLabel(this); m_summary->setObjectName("batchOtaSummary"); layout->addWidget(m_summary);
    m_table = new QTableWidget(0, 8, this); m_table->setObjectName("batchOtaResults");
    m_table->setHorizontalHeaderLabels({QStringLiteral("设备 / 地址"), QStringLiteral("型号 / SN"), QStringLiteral("阶段"), QStringLiteral("进度"), QStringLiteral("传输速度"), QStringLiteral("传输 / 总耗时"), QStringLiteral("当前 → 目标版本"), QStringLiteral("结果详情")});
    m_table->setSelectionBehavior(QAbstractItemView::SelectRows); m_table->setSelectionMode(QAbstractItemView::ExtendedSelection);
    m_table->setEditTriggers(QAbstractItemView::NoEditTriggers); m_table->setMinimumHeight(150);
    m_table->horizontalHeader()->setSectionResizeMode(QHeaderView::Interactive);
    const int widths[] = {180, 180, 275, 120, 90, 130, 190};
    for (int column = 0; column < 7; ++column) m_table->setColumnWidth(column, widths[column]);
    m_table->horizontalHeader()->setSectionResizeMode(7, QHeaderView::Stretch);
    m_table->horizontalHeaderItem(5)->setToolTip(QStringLiteral("传输耗时包含查询升级目标至发送重启；总耗时还包含连接、认证、重连和版本确认。"));
    m_table->verticalHeader()->setDefaultSectionSize(50);
    layout->addWidget(m_table, 1);
    auto *results = new QHBoxLayout;
    m_retry = new QPushButton(QStringLiteral("重试未成功设备"), this); m_retry->setObjectName("retryBatchOta");
    m_bind = new QPushButton(QStringLiteral("绑定窗口已打开，继续选中设备"), this); m_bind->setObjectName("continueBatchOtaBinding");
    m_export = new QPushButton(QStringLiteral("导出结果 CSV"), this); m_export->setObjectName("exportBatchOta");
    results->addWidget(m_retry); results->addWidget(m_bind); results->addStretch(); results->addWidget(m_export); layout->addLayout(results);
    m_notice = new QLabel(QStringLiteral("默认同时升级 2 台，使用原固件块并在确认后立即推进；两个蓝牙调优选项关闭。日常升级请关闭“详细日志”；100% 后仍需等待重启和版本确认。"), this);
    m_notice->setObjectName("batchOtaNotice"); m_notice->setWordWrap(true); layout->addWidget(m_notice);
    connect(m_all, &QPushButton::clicked, this, [this]() { checkAll(true); });
    connect(m_none, &QPushButton::clicked, this, [this]() { checkAll(false); });
    connect(m_start, &QPushButton::clicked, this, &RttBatchOtaPanel::startChecked);
    connect(m_cancel, &QPushButton::clicked, this, &RttBatchOtaPanel::cancelAll);
    connect(m_cancelSelected, &QPushButton::clicked, this, [this]() { m_controller.cancelRows(selectedRows()); });
    connect(m_retry, &QPushButton::clicked, this, &RttBatchOtaPanel::retryFailed);
    connect(m_bind, &QPushButton::clicked, this, &RttBatchOtaPanel::continueBinding);
    connect(m_export, &QPushButton::clicked, this, &RttBatchOtaPanel::exportResults);
    connect(&m_controller, &RttBatchOtaController::activeChanged, this, &RttBatchOtaPanel::setActive);
    // Block acknowledgements update the model immediately. Paint the table
    // only on the 250 ms timer, not once per block and per row.
    connect(window.m_rawFrames, &QCheckBox::toggled, &m_controller, &RttBatchOtaController::setDetailedLogging);
    connect(&m_controller, &RttBatchOtaController::logLine, &security, &SecurityPanel::logLine);
    connect(session.ble(), &BleLink::deviceFound, this, [this](const QBluetoothDeviceInfo &) { applyRssiFilter(); });
    connect(window.m_dev, &QListWidget::itemChanged, this, [this](QListWidgetItem *item) {
        if (m_syncing) return;
        const int row = m_window.m_dev->row(item);
        if (row < 0 || row >= m_window.m_devs.size()) return;
        const auto address = m_window.m_devs[row].address().toString();
        if (item->isHidden()) {
            m_checked.remove(address);
            const QSignalBlocker block(m_window.m_dev);
            item->setCheckState(Qt::Unchecked);
            refresh(); return;
        }
        if (item->checkState() == Qt::Checked) m_checked.insert(address); else m_checked.remove(address);
        refresh();
    });
    connect(window.m_disc, &QPushButton::clicked, this, &RttBatchOtaPanel::cancelAll);
    connect(security.findChild<QPushButton *>("cancelBenchFlow"), &QPushButton::clicked, this, &RttBatchOtaPanel::cancelAll);
    m_tick.setInterval(250); connect(&m_tick, &QTimer::timeout, this, &RttBatchOtaPanel::refresh); m_tick.start();
    auto *browser = window.findChild<QWidget *>("deviceBrowser");
    auto *browserLayout = browser ? qobject_cast<QVBoxLayout *>(browser->layout()) : nullptr;
    if (!browserLayout) throw std::runtime_error("Device browser layout changed");
    auto *filter = new QWidget(browser); filter->setObjectName("workbenchRssiFilter");
    auto *filterLayout = new QVBoxLayout(filter); filterLayout->setContentsMargins(0, 0, 0, 0); filterLayout->setSpacing(3);
    auto *filterRow = new QHBoxLayout; filterRow->setContentsMargins(0, 0, 0, 0);
    m_rssiEnabled = new QCheckBox(QStringLiteral("RSSI 过滤"), filter); m_rssiEnabled->setObjectName("workbenchRssiEnabled");
    m_rssiMinimum = new QSpinBox(filter); m_rssiMinimum->setObjectName("workbenchRssiMinimum");
    m_rssiMinimum->setRange(-127, -1); m_rssiMinimum->setPrefix(QStringLiteral("≥ ")); m_rssiMinimum->setSuffix(QStringLiteral(" dBm"));
    m_rssiMinimum->setMinimumWidth(105);
    m_rssiMinimum->setToolTip(QStringLiteral("仅显示信号达到阈值的设备；数值越接近 0 越强。开启后隐藏尚无有效 RSSI 的设备。"));
    QSettings settings;
    m_rssiEnabled->setChecked(settings.value("rttWorkbench/rssiEnabled", true).toBool());
    m_rssiMinimum->setValue(qBound(-127, settings.value("rttWorkbench/rssiMinimum", -60).toInt(), -1));
    filterRow->addWidget(m_rssiEnabled); filterRow->addWidget(m_rssiMinimum); filterRow->addStretch(); filterLayout->addLayout(filterRow);
    m_rssiSummary = new QLabel(filter); m_rssiSummary->setObjectName("workbenchRssiSummary"); filterLayout->addWidget(m_rssiSummary);
    browserLayout->insertWidget(browserLayout->indexOf(window.m_dev), filter);
    const auto changed = [this]() {
        QSettings saved; saved.setValue("rttWorkbench/rssiEnabled", m_rssiEnabled->isChecked());
        saved.setValue("rttWorkbench/rssiMinimum", m_rssiMinimum->value()); applyRssiFilter();
    };
    connect(m_rssiEnabled, &QCheckBox::toggled, this, changed);
    connect(m_rssiMinimum, QOverload<int>::of(&QSpinBox::valueChanged), this, changed);
    connect(session.ble(), &BleLink::scanStarted, this, [this]() { applyRssiFilter(); });
    applyRssiFilter(); refresh();
}

RttBatchOtaPanel::~RttBatchOtaPanel() { m_tick.stop(); }

bool RttBatchOtaPanel::visibleDevice(int index) const
{
    return index >= 0 && index < m_window.m_devs.size() && index < m_window.m_dev->count() &&
           !m_window.m_dev->item(index)->isHidden();
}

void RttBatchOtaPanel::applyRssiFilter()
{
    int visible = 0, first = -1;
    for (int i = 0; i < m_window.m_dev->count() && i < m_window.m_devs.size(); ++i) {
        const auto &device = m_window.m_devs[i]; const int rssi = device.rssi();
        const bool show = !m_rssiEnabled->isChecked() || (rssi >= m_rssiMinimum->value() && rssi < 0);
        m_window.m_dev->item(i)->setHidden(!show);
        if (show) { ++visible; if (first < 0) first = i; }
        else m_checked.remove(device.address().toString());
    }
    if (!visibleDevice(m_window.m_dev->currentRow())) {
        m_window.m_dev->setCurrentRow(first);
        if (first < 0) m_window.m_dev->clearSelection();
    }
    m_rssiSummary->setText(QStringLiteral("显示 %1 / %2 台").arg(visible).arg(m_window.m_devs.size()));
    syncChecks(); m_window.refreshUi();
}

void RttBatchOtaPanel::syncChecks()
{
    m_syncing = true;
    for (int i = 0; i < m_window.m_dev->count() && i < m_window.m_devs.size(); ++i) {
        auto *item = m_window.m_dev->item(i);
        item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
        item->setCheckState(m_checked.contains(m_window.m_devs[i].address().toString()) ? Qt::Checked : Qt::Unchecked);
    }
    m_syncing = false; refresh();
}

void RttBatchOtaPanel::checkAll(bool checked)
{
    if (active()) return;
    m_checked.clear();
    if (checked) for (int i = 0; i < m_window.m_devs.size(); ++i)
        if (visibleDevice(i)) m_checked.insert(m_window.m_devs[i].address().toString());
    syncChecks();
}

void RttBatchOtaPanel::startChecked()
{
    QVector<RttOtaDevice> devices;
    for (int i = 0; i < m_window.m_devs.size(); ++i)
        if (visibleDevice(i) && m_checked.contains(m_window.m_devs[i].address().toString())) devices.append({m_window.m_devs[i], false});
    begin(devices);
}

void RttBatchOtaPanel::begin(QVector<RttOtaDevice> devices)
{
    if (active()) return;
    try {
        security::require(!devices.isEmpty(), "请先在左侧蓝牙列表勾选待升级设备。");
        if (m_rssiEnabled->isChecked()) for (const auto &device : devices) {
            int index = -1;
            for (int i = 0; i < m_window.m_devs.size(); ++i)
                if (m_window.m_devs[i].address() == device.info.address()) { index = i; break; }
            security::require(visibleDevice(index), "所选设备未达到当前 RSSI 过滤条件，请重新扫描或调整阈值后再开始。");
        }
        security::require(!m_security.busy() && !m_session.authenticated() && !m_session.connectionOpening() && !m_session.ble()->isReady(), "请先停止单设备操作并断开连接，再开始批量升级。");
        security::require(m_security.m_serverMode && !m_security.simulated(), "批量升级使用服务器账号和真实 BLE 设备。");
        auto image = RttOtaImage::inspect(m_window.m_path->text().trimmed(), m_window.m_ver->text());
        image.preferThroughput = m_throughput->isChecked();
        image.fitAtt = m_fitAtt->isChecked();
        image.eventDriven = m_eventDrive->isChecked();
        image.compactLog = !m_window.m_rawFrames->isChecked();
        auto context = std::make_shared<ConnectionContext>();
        context->config = m_security.m_serverConfig;
        if (context->config.authorization.isEmpty()) {
            const auto uid = m_security.rememberedServerUid();
            security::require(!uid.isEmpty(), "请先登录服务器，或使用该服务器和账号已有的 App 缓存。");
            context->config = security::ServerConfig();
            context->config.gateway = QUrl(m_security.m_systemUrl->text());
            context->config.allowLanHttp = m_security.m_allowLanHttp->isChecked(); context->config.uid = uid;
        } else security::require(!m_security.m_serverDirty, "服务器设置已变化，请重新登录后再开始。");
        context->config.validate();
        context->cache = context->config.cacheDirectory(m_security.m_directory->text());
        m_session.ble()->stopScan();
        const bool started = m_controller.begin(devices, image, m_concurrency->value(),
            [context](const RttOtaDevice &device, const RttOtaImage &firmware, QObject *parent) -> RttOtaTask * {
                return new RttDeviceOtaTask(device, firmware, context->config, context->cache, parent);
            });
        security::require(started, "批量任务未能开始，请检查重复地址、固件文件和并发数。");
        m_notice->setText(QStringLiteral("本批次：%1 · 目标版本 %2 · %3 台设备；每台独立认证和验版。").arg(image.model, image.version).arg(devices.size()));
    } catch (const std::exception &error) { m_notice->setText(QString::fromUtf8(error.what())); }
}

void RttBatchOtaPanel::retryFailed()
{
    if (active()) return;
    QVector<RttOtaDevice> devices;
    for (const auto &row : m_controller.rows()) if (row.result != RttOtaResult::Success) devices.append({row.device.info, false});
    begin(devices);
}

QVector<int> RttBatchOtaPanel::selectedRows() const
{
    QVector<int> rows;
    for (const auto &index : m_table->selectionModel()->selectedRows()) rows.append(index.row());
    return rows;
}

void RttBatchOtaPanel::continueBinding()
{
    if (active()) return;
    QVector<RttOtaDevice> devices;
    for (const int index : selectedRows()) {
        const auto &row = m_controller.rows().at(index);
        if (row.result == RttOtaResult::NeedsBinding) devices.append({row.device.info, true});
    }
    if (devices.isEmpty()) { m_notice->setText(QStringLiteral("请先打开目标设备的绑定窗口，再选中结果为“需要绑定”的行。")); return; }
    begin(devices);
}

void RttBatchOtaPanel::cancelAll() { m_controller.cancelAll(); }

void RttBatchOtaPanel::setActive(bool active)
{
    m_security.setProperty("rttBatchOtaActive", active);
    if (auto *single = m_window.findChild<QGroupBox *>("otaPanel")) single->setEnabled(!active);
    m_security.refresh(); m_window.refreshUi();
    emit m_security.stateChanged();
    refresh();
}

QJsonObject RttBatchOtaPanel::state() const
{
    int queued = 0, success = 0, failed = 0, unknown = 0, cancelled = 0, binding = 0;
    for (const auto &row : m_controller.rows()) {
        switch (row.result) {
        case RttOtaResult::Queued: ++queued; break;
        case RttOtaResult::Success: ++success; break;
        case RttOtaResult::Failed: ++failed; break;
        case RttOtaResult::Unknown: ++unknown; break;
        case RttOtaResult::Cancelled: ++cancelled; break;
        case RttOtaResult::NeedsBinding: ++binding; break;
        default: break;
        }
    }
    return {{"active", active()}, {"running", m_controller.running()}, {"concurrency", m_concurrency->value()},
            {"throughput_preferred", m_throughput->isChecked()}, {"compact_log", !m_window.m_rawFrames->isChecked()},
            {"refresh_interval_ms", m_tick.interval()},
            {"fit_ota_to_att", m_fitAtt->isChecked()},
            {"event_driven_ota", m_eventDrive->isChecked()},
            {"rssi_filter_enabled", m_rssiEnabled->isChecked()}, {"rssi_min_dbm", m_rssiMinimum->value()},
            {"total", m_controller.rows().size()}, {"queued", queued}, {"success", success},
            {"failed", failed}, {"unknown", unknown}, {"cancelled", cancelled}, {"needs_binding", binding}};
}

void RttBatchOtaPanel::refresh()
{
    setProperty("rttRefreshCount", QVariant::fromValue(++m_refreshCount));
    const auto status = state();
    int checked = 0;
    for (const auto &device : m_window.m_devs) if (m_checked.contains(device.address().toString())) ++checked;
    m_summary->setText(QStringLiteral("已勾选 %1 台 · 运行 %2 台 · 排队 %3 台 · 成功 %4 台 · 失败 %5 台 · 未确认 %6 台 · 需要绑定 %7 台")
        .arg(checked).arg(status["running"].toInt()).arg(status["queued"].toInt()).arg(status["success"].toInt())
        .arg(status["failed"].toInt()).arg(status["unknown"].toInt()).arg(status["needs_binding"].toInt()));
    m_start->setEnabled(!active()); m_all->setEnabled(!active()); m_none->setEnabled(!active());
    m_concurrency->setEnabled(!active()); m_cancel->setEnabled(active()); m_cancelSelected->setEnabled(active());
    m_throughput->setEnabled(!active());
    m_fitAtt->setEnabled(!active());
    m_eventDrive->setEnabled(!active());
    const bool filterLocked = active() || m_security.busy() || m_session.connectionOpening() || m_session.ble()->isReady();
    m_rssiEnabled->setEnabled(!filterLocked); m_rssiMinimum->setEnabled(!filterLocked && m_rssiEnabled->isChecked());
    m_retry->setEnabled(!active() && status["total"].toInt() > status["success"].toInt());
    m_bind->setEnabled(!active() && status["needs_binding"].toInt() > 0);
    m_export->setEnabled(!m_controller.rows().isEmpty());
    const auto &rows = m_controller.rows();
    if (m_table->rowCount() != rows.size()) m_table->setRowCount(rows.size());
    for (int index = 0; index < rows.size(); ++index) {
        const auto &row = rows[index];
        const auto elapsed = m_controller.elapsedMs(row);
        const auto transfer = m_controller.transferElapsedMs(row);
        const auto speed = transfer > 0 ? double(row.done) * 1000.0 / double(transfer) / 1024.0 : 0.0;
        const QStringList values{row.device.info.name() + '\n' + row.device.info.address().toString(),
            row.model + '\n' + row.serial, row.stage, {}, QStringLiteral("%1 KiB/s").arg(speed, 0, 'f', 1),
            QStringLiteral("%1 / %2 s").arg(transfer / 1000).arg(elapsed / 1000), row.currentVersion + QStringLiteral(" → ") + m_controller.image().version,
            row.detail};
        for (int column = 0; column < values.size(); ++column) {
            if (column == 3) continue;
            auto *item = m_table->item(index, column);
            if (!item) { item = new QTableWidgetItem; m_table->setItem(index, column, item); }
            if (item->text() != values[column]) { item->setText(values[column]); item->setToolTip(values[column]); }
        }
        auto *progress = qobject_cast<QProgressBar *>(m_table->cellWidget(index, 3));
        if (!progress) { progress = new QProgressBar(m_table); progress->setMinimumWidth(110); progress->setRange(0, 100); m_table->setCellWidget(index, 3, progress); }
        progress->setValue(row.result == RttOtaResult::Success ? 100 : row.total ? int(quint64(row.done) * 100 / row.total) : 0);
    }
}

void RttBatchOtaPanel::exportResults()
{
    const auto path = QFileDialog::getSaveFileName(this, QStringLiteral("导出批量升级结果"), "sc201-batch-ota.csv", "CSV (*.csv)");
    if (path.isEmpty()) return;
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) { m_notice->setText(QStringLiteral("无法写入所选结果文件。")); return; }
    QTextStream stream(&file); stream.setCodec("UTF-8"); stream.setGenerateByteOrderMark(true);
    stream << "设备名称,地址,型号,SN,结果,阶段,已传字节,总字节,当前版本,目标版本,总耗时秒,传输耗时秒,详情\r\n";
    for (const auto &row : m_controller.rows()) {
        QStringList values{row.device.info.name(), row.device.info.address().toString(), row.model, row.serial,
            rttOtaResultText(row.result), row.stage, QString::number(row.done), QString::number(row.total), row.currentVersion,
            m_controller.image().version, QString::number(m_controller.elapsedMs(row) / 1000),
            QString::number(m_controller.transferElapsedMs(row) / 1000), row.detail};
        for (auto &value : values) value = csvCell(value);
        stream << values.join(',') << "\r\n";
    }
    stream.flush();
    m_notice->setText(file.commit() ? QStringLiteral("已导出升级结果，不含登录凭据或密钥。") : QStringLiteral("结果文件保存失败。"));
}
