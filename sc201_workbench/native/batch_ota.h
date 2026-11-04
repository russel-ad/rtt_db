#pragma once
#include <QObject>
#include <QBluetoothDeviceInfo>
#include <QBluetoothAddress>
#include <QElapsedTimer>
#include <QHash>
#include <QPointer>
#include <QSet>
#include <QVector>
#include <QStringList>
#include <functional>

struct RttOtaImage {
    QString path, version, model;
    QByteArray sha256;
    QStringList models;
    bool preferThroughput = false, compactLog = true;
    bool fitAtt = false;
    bool eventDriven = true;
    static RttOtaImage inspect(const QString &path, const QString &version);
    bool unchanged() const;
};

struct RttOtaDevice {
    QBluetoothDeviceInfo info;
    bool bindingApproved = false;
};

enum class RttOtaResult { Queued, Running, Success, Failed, Unknown, Cancelled, NeedsBinding };
QString rttOtaResultText(RttOtaResult result);

class RttOtaTask : public QObject {
    Q_OBJECT
public:
    using QObject::QObject;
    virtual void start() = 0;
    virtual void cancel() = 0;
    virtual void setDetailedLogging(bool) {}
    virtual bool supportsPreparedTransfer() const { return false; }
    virtual void releasePreparedTransfer() {}
signals:
    void prepared();
    void stage(const QString &);
    void identity(const QString &model, const QString &serial, const QString &version);
    void progress(quint32 done, quint32 total);
    void transferStarted();
    void transferStopped();
    void finished(RttOtaResult result, const QString &detail);
    void logLine(const QString &);
};

struct RttOtaRow {
    RttOtaDevice device;
    RttOtaResult result = RttOtaResult::Queued;
    QString stage, model, serial, currentVersion, detail;
    quint32 done = 0, total = 0;
    qint64 startedMs = -1, finishedMs = -1;
    qint64 transferStartedMs = -1, transferFinishedMs = -1;
};

// Slots include authentication, reboot and verification, until a final verdict.
// Each task owns its own session and backend request cancellation scope.
class RttBatchOtaController : public QObject {
    Q_OBJECT
public:
    using Factory = std::function<RttOtaTask *(const RttOtaDevice &, const RttOtaImage &, QObject *)>;
    explicit RttBatchOtaController(QObject *parent = nullptr);
    ~RttBatchOtaController() override;
    bool begin(const QVector<RttOtaDevice> &, const RttOtaImage &, int concurrency, Factory);
    void cancelRows(const QVector<int> &rows);
    void cancelAll();
    void setDetailedLogging(bool enabled);
    bool active() const { return m_active; }
    int running() const { return m_jobs.size(); }
    int concurrency() const { return m_limit; }
    const QVector<RttOtaRow> &rows() const { return m_rows; }
    const RttOtaImage &image() const { return m_image; }
    qint64 elapsedMs(const RttOtaRow &) const;
    qint64 transferElapsedMs(const RttOtaRow &) const;
signals:
    void changed();
    void activeChanged(bool);
    void logLine(const QString &);
private:
    void pump();
    void settle();
    QVector<RttOtaRow> m_rows;
    QHash<int, QPointer<RttOtaTask>> m_jobs;
    QSet<int> m_prepared;
    RttOtaImage m_image;
    Factory m_factory;
    QElapsedTimer m_clock;
    quint64 m_generation = 0;
    int m_limit = 2;
    int m_preparing = -1;
    bool m_waveTransferring = false;
    bool m_active = false, m_pumping = false;
};
