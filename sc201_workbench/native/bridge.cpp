// The bridge owns presentation/lifecycle only. All product operations are the
// original sc201_host implementation, including its frozen libmlink/TLS stack.
#include "MainWindow.h"
#include "SecurityPanel.h"
#include "MlinkSession.h"
#include "BleLink.h"
#include "HostLog.h"
#include "batch_ota_panel.h"

#include <QApplication>
#include <QDir>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLockFile>
#include <QPointer>
#include <QThread>
#include <QVariant>
#include <QVBoxLayout>
#include <memory>
#include <stdexcept>

#define RTT_EXPORT extern "C" __declspec(dllexport)
void rttCloseHostLog();
namespace {
QByteArray lastError;
QByteArray stateBytes;
class EmbeddedWorkbench;
QPointer<EmbeddedWorkbench> current;

class EmbeddedWorkbench final : public QWidget {
public:
    EmbeddedWorkbench(QWidget *parent, const QString &directory)
        : QWidget(parent), dataDirectory(QDir(directory).absolutePath()),
          lock(QDir(dataDirectory).filePath("workbench-gui.lock")) {
        setObjectName("sc201EmbeddedWorkbench");
        if (!QDir().mkpath(dataDirectory))
            throw std::runtime_error("Cannot create the selected workbench directory");
        lock.setStaleLockTime(0);
        if (!lock.tryLock(0))
            throw std::runtime_error("这份资料已有工作台在使用，请先正常关闭原工作台。");
        qApp->setProperty("data-dir", dataDirectory);
        // RTT uses config.json. Preserve the original SC201 QSettings identity
        // for workbench preferences and DPAPI login settings in this process.
        QCoreApplication::setOrganizationName(QStringLiteral("SC201"));
        QCoreApplication::setApplicationName(QStringLiteral("SC201"));
        if (!initializeHostLog(QDir(dataDirectory).filePath("logs")))
            throw std::runtime_error("Cannot open the workbench operation log");
        window = new MainWindow(this);
        window->setWindowFlags(Qt::Widget);
        auto *layout = new QVBoxLayout(this);
        layout->setContentsMargins(0, 0, 0, 0);
        layout->addWidget(window);
        session = window->findChild<MlinkSession *>();
        security = window->findChild<SecurityPanel *>();
        if (!session || !security)
            throw std::runtime_error("The original workbench object structure has changed");
        auto *firmware = window->findChild<QWidget *>("firmwarePage");
        auto *firmwareLayout = firmware ? qobject_cast<QVBoxLayout *>(firmware->layout()) : nullptr;
        if (!firmwareLayout) throw std::runtime_error("The original firmware page layout has changed");
        batch = new RttBatchOtaPanel(*window, *security, *session, firmware);
        firmwareLayout->insertWidget(firmwareLayout->count() - 1, batch, 1);
        connect(session->ble(), &BleLink::scanStarted, this, [this]() { scanning = true; });
        connect(session->ble(), &BleLink::scanFinished, this, [this]() { scanning = false; });
        window->show();
    }

    ~EmbeddedWorkbench() override {
        delete batch;
        delete window;
        rttCloseHostLog();
    }

    bool busy() const {
        return !stopped && (security->busy() || session->connectionOpening() ||
                           session->otaBusy() || session->otaVerifying());
    }

    QJsonObject state() const {
        const bool connected = !stopped && (session->authenticated() || session->ble()->isReady() ||
                                            session->connectionOpening());
        return {{"ready", !stopped}, {"busy", busy()}, {"connected", connected},
                {"scanning", !stopped && scanning},
                {"ota_active", !stopped && (session->otaBusy() || session->otaVerifying() || batch->active())},
                {"batch_ota", batch->state()},
                {"data_directory", dataDirectory}};
    }

    bool shutdown() {
        if (stopped) return true;
        if (busy()) {
            lastError = "工作台正在连接、执行流程或确认升级，请先在工作台停止操作或等待完成。";
            return false;
        }
        session->ble()->stopScan();
        security->cancel();
        session->disconnectBle();
        window->close(); // Original close saves its own preferences.
        stopped = true;
        lock.unlock();
        rttCloseHostLog();
        return true;
    }

    MainWindow *window = nullptr;
    MlinkSession *session = nullptr;
    SecurityPanel *security = nullptr;
    RttBatchOtaPanel *batch = nullptr;
    QString dataDirectory;
    QLockFile lock;
    bool scanning = false;
    bool stopped = false;
};

EmbeddedWorkbench *get(void *handle) {
    if (!current || current.data() != handle)
        throw std::runtime_error("Workbench handle is no longer valid");
    if (QThread::currentThread() != qApp->thread())
        throw std::runtime_error("Workbench calls must run on the Qt GUI thread");
    return current.data();
}
}

RTT_EXPORT const char *rtt_sc201_qt_version() { return QT_VERSION_STR; }
RTT_EXPORT const char *rtt_sc201_last_error() { return lastError.constData(); }

RTT_EXPORT void *rtt_sc201_create(void *parent, const wchar_t *directory) {
    lastError.clear();
    try {
        if (!qApp || QThread::currentThread() != qApp->thread())
            throw std::runtime_error("Create the Qt application on the GUI thread first");
        if (current) throw std::runtime_error("Only one embedded workbench is supported per process");
        if (!directory || !*directory) throw std::runtime_error("Select an explicit workbench directory");
        if (QString::fromLatin1(qVersion()) != QLatin1String(QT_VERSION_STR))
            throw std::runtime_error("Qt runtime does not match the workbench build");
        auto *widget = new EmbeddedWorkbench(static_cast<QWidget *>(parent), QString::fromWCharArray(directory));
        current = widget;
        return widget;
    } catch (const std::exception &error) {
        lastError = error.what();
        if (!current) rttCloseHostLog();
        return nullptr;
    }
}

RTT_EXPORT const char *rtt_sc201_state(void *handle) {
    try { stateBytes = QJsonDocument(get(handle)->state()).toJson(QJsonDocument::Compact); }
    catch (const std::exception &error) { lastError = error.what(); stateBytes = "{}"; }
    return stateBytes.constData();
}

RTT_EXPORT int rtt_sc201_shutdown(void *handle) {
    try { return get(handle)->shutdown() ? 1 : 0; }
    catch (const std::exception &error) { lastError = error.what(); return 0; }
}
