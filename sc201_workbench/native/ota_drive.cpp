#include "ota_drive.h"
#include <algorithm>

RttOtaPoint rttOtaPoint(const mlink_ota_job_t *job)
{
    if (!job) return {};
    return {mlink_ota_job_state(job), mlink_xfer_tx_state(&job->transfer),
        job->transfer.transfer_id, job->transfer.pending_tag, job->transfer.idx};
}

bool rttOtaContinue(const RttOtaPoint &before, const RttOtaPoint &after, unsigned passes)
{
    return passes < RttOtaPollBudget && before.job == MLINK_OTA_JOB_TRANSFER &&
        after.job == MLINK_OTA_JOB_TRANSFER && before.id && before.id == after.id &&
        before.transfer >= 0 && after.transfer >= 0 && before.transfer != after.transfer;
}

RttOtaTrace::RttOtaTrace(QObject *parent) : QObject(parent)
{
    setObjectName(QStringLiteral("_rttOtaTrace"));
    m_clock.start();
}

RttOtaTrace *RttOtaTrace::existing(QObject *session)
{
    return session->findChild<RttOtaTrace *>(QStringLiteral("_rttOtaTrace"), Qt::FindDirectChildrenOnly);
}

RttOtaTrace &RttOtaTrace::forSession(QObject *session)
{
    auto *trace = existing(session);
    return *(trace ? trace : new RttOtaTrace(session));
}

void RttOtaTrace::Duration::add(qint64 value)
{
    if (value < 0) return;
    ++count; total += quint64(value); maximum = std::max(maximum, quint64(value));
}

double RttOtaTrace::Duration::averageMs() const
{
    return count ? double(total) / double(count) / 1000. : 0.;
}

void RttOtaTrace::begin(uint32_t transferId, bool eventDriven)
{
    m_active = true; m_eventDriven = eventDriven; m_transferId = transferId;
    m_submissions = m_retries = m_incomplete = m_storeFirst = m_passes = m_extraPasses = 0;
    m_acks = m_receipts = 0;
    m_maxPasses = m_lastProgress = 0;
    m_block = {}; m_ack = {}; m_store = {}; m_next = {}; m_cycle = {};
}

void RttOtaTrace::pass(unsigned number)
{
    if (!m_active) return;
    ++m_passes;
    if (number > 1) ++m_extraPasses;
    m_maxPasses = std::max(m_maxPasses, number);
}

void RttOtaTrace::transition(const RttOtaPoint &before, const RttOtaPoint &after, qint64 atUs)
{
    if (before.job == MLINK_OTA_JOB_TRANSFER && after.job == MLINK_OTA_JOB_TRANSFER &&
        before.id == after.id && before.transfer == MLINK_XFER_TX_BLOCK &&
        after.transfer == MLINK_XFER_TX_WAIT_BLOCK_ACK)
        queued(after.id, after.block, after.tag, atUs);
}

void RttOtaTrace::queued(uint32_t id, uint16_t block, uint32_t tag, qint64 atUs)
{
    if (!m_active || id != m_transferId || !tag) return;
    if (m_block.queued >= 0) {
        if (block == m_block.index) ++m_retries;
        else if (m_block.counted) {
            m_next.add(atUs - std::max(m_block.ack, m_block.stored));
            m_cycle.add(atUs - m_block.queued);
        }
        if (!m_block.counted) ++m_incomplete;
    }
    ++m_submissions;
    m_block = {block, tag, atUs, -1, -1, false};
}

void RttOtaTrace::ack(uint32_t tag, int result, qint64 atUs)
{
    if (!m_active || m_block.queued < 0 || tag != m_block.tag || result != MLINK_DONE_OK ||
        m_block.ack >= 0 || atUs < m_block.queued) return;
    m_block.ack = atUs;
    ++m_acks;
    countConfirmed();
}

void RttOtaTrace::stored(uint32_t id, uint16_t block, qint64 atUs)
{
    if (!m_active || id != m_transferId || m_block.queued < 0 || block != m_block.index ||
        m_block.stored >= 0 || atUs < m_block.queued) return;
    m_block.stored = atUs;
    ++m_receipts;
    countConfirmed();
}

void RttOtaTrace::countConfirmed()
{
    if (m_block.counted || m_block.ack < 0 || m_block.stored < 0) return;
    m_ack.add(m_block.ack - m_block.queued);
    m_store.add(m_block.stored - m_block.queued);
    m_storeFirst += m_block.stored < m_block.ack;
    m_block.counted = true;
}

QVariantMap RttOtaTrace::statistics() const
{
    return {{"active", m_active}, {"event_driven", m_eventDriven}, {"submissions", qulonglong(m_submissions)},
        {"confirmed", qulonglong(m_ack.count)}, {"ack_received", qulonglong(m_acks)},
        {"stored_received", qulonglong(m_receipts)},
        {"retries", qulonglong(m_retries)}, {"incomplete_attempts", qulonglong(m_incomplete)},
        {"stored_before_ack", qulonglong(m_storeFirst)}, {"next_samples", qulonglong(m_next.count)},
        {"queue_to_ack_ms", m_ack.averageMs()}, {"queue_to_store_ms", m_store.averageMs()},
        {"confirmed_to_next_queue_ms", m_next.averageMs()}, {"cycle_ms", m_cycle.averageMs()},
        {"confirmed_to_next_queue_max_ms", double(m_next.maximum) / 1000.},
        {"poll_passes", qulonglong(m_passes)}, {"extra_passes", qulonglong(m_extraPasses)},
        {"max_passes_per_turn", m_maxPasses}};
}

QString RttOtaTrace::summary(const QString &stage) const
{
    return QStringLiteral("[ota-timing] %1 drive=%2 submitted=%3 confirmed=%4 retries=%5 incomplete=%6 "
        "queue_ack_avg_ms=%7 queue_store_avg_ms=%8 confirmed_next_avg_ms=%9 confirmed_next_max_ms=%10 "
        "next_samples=%11 cycle_avg_ms=%12 store_before_ack=%13 extra_passes=%14 max_passes=%15")
        .arg(stage).arg(m_eventDriven).arg(m_submissions).arg(m_ack.count).arg(m_retries).arg(m_incomplete)
        .arg(m_ack.averageMs(), 0, 'f', 3).arg(m_store.averageMs(), 0, 'f', 3)
        .arg(m_next.averageMs(), 0, 'f', 3).arg(double(m_next.maximum) / 1000., 0, 'f', 3)
        .arg(m_next.count).arg(m_cycle.averageMs(), 0, 'f', 3).arg(m_storeFirst).arg(m_extraPasses).arg(m_maxPasses);
}

QString RttOtaTrace::progress(unsigned blocks, unsigned total)
{
    if (!m_active || !blocks || blocks == m_lastProgress || (blocks != total && blocks % 128)) return {};
    m_lastProgress = blocks;
    return summary(QStringLiteral("progress"));
}

QString RttOtaTrace::finish()
{
    if (!m_active) return {};
    if (m_block.queued >= 0 && !m_block.counted) ++m_incomplete;
    m_active = false;
    return summary(QStringLiteral("summary"));
}
