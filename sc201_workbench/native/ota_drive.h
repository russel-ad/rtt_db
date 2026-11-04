#pragma once
#include "mlink/ota.h"
#include <QObject>
#include <QElapsedTimer>
#include <QVariantMap>

// Read-only observations of the caller-owned job. All state transitions,
// packet matching and acknowledgements remain in the frozen library.
struct RttOtaPoint {
    int job = -1, transfer = -1;
    uint32_t id = 0, tag = 0;
    uint16_t block = 0;
};
RttOtaPoint rttOtaPoint(const mlink_ota_job_t *job);
constexpr unsigned RttOtaPollBudget = 8;
bool rttOtaContinue(const RttOtaPoint &before, const RttOtaPoint &after, unsigned passes);

class RttOtaTrace final : public QObject {
    Q_OBJECT
public:
    explicit RttOtaTrace(QObject *parent);
    static RttOtaTrace &forSession(QObject *session);
    static RttOtaTrace *existing(QObject *session);
    qint64 nowUs() const { return m_clock.nsecsElapsed() / 1000; }
    void begin(uint32_t transferId, bool eventDriven);
    void pass(unsigned number);
    void transition(const RttOtaPoint &before, const RttOtaPoint &after, qint64 atUs);
    void queued(uint32_t transferId, uint16_t block, uint32_t tag, qint64 atUs);
    void ack(uint32_t tag, int result, qint64 atUs);
    void stored(uint32_t transferId, uint16_t block, qint64 atUs);
    QString progress(unsigned blocks, unsigned total);
    QString finish();
    QVariantMap statistics() const;
private:
    struct Duration {
        quint64 count = 0, total = 0, maximum = 0;
        void add(qint64 value);
        double averageMs() const;
    };
    struct Block {
        uint16_t index = 0;
        uint32_t tag = 0;
        qint64 queued = -1, ack = -1, stored = -1;
        bool counted = false;
    };
    void countConfirmed();
    QString summary(const QString &stage) const;
    QElapsedTimer m_clock;
    bool m_active = false, m_eventDriven = false;
    uint32_t m_transferId = 0;
    quint64 m_submissions = 0, m_retries = 0, m_incomplete = 0, m_storeFirst = 0;
    quint64 m_acks = 0, m_receipts = 0;
    quint64 m_passes = 0, m_extraPasses = 0;
    unsigned m_maxPasses = 0, m_lastProgress = 0;
    Block m_block;
    Duration m_ack, m_store, m_next, m_cycle;
};
