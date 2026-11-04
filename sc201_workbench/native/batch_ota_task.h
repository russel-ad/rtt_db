#pragma once
#include "batch_ota.h"
#include "security/ServerBackend.h"
#include <QTimer>
#include <memory>
class MlinkSession;
class AppController;

class RttDeviceOtaTask final : public RttOtaTask {
    Q_OBJECT
public:
    RttDeviceOtaTask(const RttOtaDevice &, const RttOtaImage &, security::ServerConfig,
                     const QString &cache, QObject *parent = nullptr);
    // Dependency injection for isolated native tests. Normal UI always uses
    // the ServerBackend + real BLE constructor above.
    RttDeviceOtaTask(const RttOtaDevice &, const RttOtaImage &,
                     std::unique_ptr<security::AppBackend>, std::unique_ptr<MlinkSession>,
                     const QString &account, const QString &cache, bool allowServer = true, QObject *parent = nullptr);
    ~RttDeviceOtaTask() override;
    void start() override;
    void cancel() override;
    void setDetailedLogging(bool enabled) override;
    bool supportsPreparedTransfer() const override { return true; }
    void releasePreparedTransfer() override;
private:
    void preferThroughput(bool enabled);
    void tryOta();
    void finish(RttOtaResult, const QString &);
    std::unique_ptr<security::AppBackend> m_backend;
    std::unique_ptr<MlinkSession> m_session;
    std::unique_ptr<AppController> m_app;
    RttOtaDevice m_device;
    RttOtaImage m_image;
    QString m_account, m_model, m_serial, m_version;
    QTimer m_deadline, m_drain;
    QElapsedTimer m_wait;
    bool m_started = false, m_done = false, m_cancelled = false, m_otaStarted = false, m_rebooted = false;
    bool m_allowServer = true;
    bool m_prepared = false;
};
