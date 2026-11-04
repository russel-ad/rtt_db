#include "batch_ota.h"
#include "batch_ota_task.h"
#include "ota_tuning.h"
#include "ble_speed.h"
#include "ota_att_plan.h"
#include "ota_drive.h"
#include "MlinkSession.h"
#include "security/BackendService.h"
#include "security/DeviceSerial.h"
#include "simulation/SimulatedScale.h"
#include "workflows/AppController.h"
#include "workflows/FactoryController.h"
#include <QtTest>
#include <QFile>
#include <QTemporaryDir>
#include <QEventLoop>
#include <array>
#include <stdexcept>

namespace {
void writeImage(const QString &path, const QByteArray &bytes) {
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly) || file.write(bytes) != bytes.size()) throw std::runtime_error("Cannot write isolated image fixture");
}
QByteArray readFile(const QString &path) { QFile file(path); file.open(QIODevice::ReadOnly); return file.readAll(); }
RttOtaImage image(const QString &directory) {
    const auto path = directory + "/test.bin";
    writeImage(path, QByteArray(128, 'x') + security::firmwareModelMarker("SC201-S"));
    return RttOtaImage::inspect(path, "V009");
}
QVector<RttOtaDevice> devices(int count = 3) {
    QVector<RttOtaDevice> result;
    for (int i = 0; i < count; ++i) {
        QBluetoothDeviceInfo info(QBluetoothAddress(QStringLiteral("12:34:56:78:90:%1").arg(i+1, 2, 16, QLatin1Char('0'))), QStringLiteral("fixture %1").arg(i), 0);
        info.setCoreConfigurations(QBluetoothDeviceInfo::LowEnergyCoreConfiguration);
        result.append({info, false});
    }
    return result;
}
class FakeTask : public RttOtaTask {
public:
    using RttOtaTask::RttOtaTask;
    bool started = false, cancelled = false;
    bool detailed = false;
    void start() override { started = true; }
    void cancel() override { cancelled = true; emit stage("cancel pending"); }
    void complete(RttOtaResult result) { emit finished(result, "fixture final verdict"); }
    void setDetailedLogging(bool enabled) override { detailed = enabled; }
};
class PreparedTask : public FakeTask {
public:
    using FakeTask::FakeTask;
    int releases = 0;
    bool supportsPreparedTransfer() const override { return true; }
    void readyForTransfer() { emit prepared(); }
    void releasePreparedTransfer() override { ++releases; emit transferStarted(); }
};
struct IdentityPort : SessionTransport {
    QString model = "SC201-S", serial = "M22010000A01005";
    bool ready = false;
    int writes = 0, opens = 0;
    void connectTo(const QBluetoothDeviceInfo &) override {
        ++opens; ready = true; emit publicIdentityRead(model.toLatin1(), serial.toLatin1()); emit readyChanged(true);
    }
    void disconnectFromDevice() override { const bool was = ready; ready = false; if (was) emit readyChanged(false); }
    bool isReady() const override { return ready; }
    bool isOpening() const override { return false; }
    uint16_t attPayload() const override { return 20; }
    uint16_t pendingBytes() const override { return 0; }
    int write(const uint8_t *, uint16_t size) override { ++writes; return size; }
};
class ReportedAttScale : public SimulatedScale {
public:
    using SimulatedScale::SimulatedScale;
    uint16_t payload = 20;
    uint16_t attPayload() const override { return payload; }
};
// Isolated transport fault injection: copy each accepted slice and keep one
// asynchronous SDK operation pending. Hold encrypted TLV frames while still
// forwarding native ACK frames. No test protocol or crypto replacement.
class GatedAsyncScale : public SessionTransport {
public:
    ReportedAttScale board;
    QByteArray held;
    bool holdWritten = false, validFrames = true;
    int peakSdkOperations = 0;
    explicit GatedAsyncScale(const QString &directory)
        : board(directory, "M21010000A01000", true) {
        board.payload = 244; board.suppressWeightReports(true);
        mlink_reasm_init(&m_reasm, 0);
        connect(&board, &SessionTransport::publicIdentityRead, this, &SessionTransport::publicIdentityRead);
        connect(&board, &SessionTransport::readyChanged, this, [this](bool ready) {
            if (!ready) { ++m_generation; m_pending = 0; mlink_reasm_init(&m_reasm, 0); }
            emit readyChanged(ready);
        });
        connect(&board, &SessionTransport::disconnected, this, &SessionTransport::disconnected);
        connect(&board, &SessionTransport::bytesReceived, this, [this](const QByteArray &bytes) {
            for (int offset = 0; offset < bytes.size();) {
                uint16_t used = 0;
                const auto rc = mlink_reasm_push(&m_reasm, reinterpret_cast<const uint8_t *>(bytes.constData() + offset),
                    uint16_t(bytes.size() - offset), 1, &used);
                if (!used) { validFrames = false; return; }
                offset += used;
                if (rc != MLINK_OK) continue;
                const auto *pdu = mlink_reasm_pdu(&m_reasm); mlink_hdr_t header{};
                uint8_t encoded[MLINK_FRAME_MAX]; uint16_t size = 0;
                if (mlink_hdr_decode(&header, pdu) != MLINK_OK ||
                    mlink_frame_encode(encoded, sizeof(encoded), &header, pdu + MLINK_HDR_LEN, &size) != MLINK_OK) {
                    validFrames = false; return;
                }
                const QByteArray frame(reinterpret_cast<const char *>(encoded), size);
                if (holdWritten && board.otaBlocks() && mlink_flags_kind(header.flags) == MLINK_KIND_TLV) held += frame;
                else emit bytesReceived(frame);
            }
        });
    }
    void connectTo(const QBluetoothDeviceInfo &info) override { board.connectTo(info); }
    void disconnectFromDevice() override { ++m_generation; m_pending = 0; board.disconnectFromDevice(); }
    bool isReady() const override { return board.isReady(); }
    bool isOpening() const override { return board.isOpening(); }
    uint16_t attPayload() const override { return board.attPayload(); }
    uint16_t pendingBytes() const override { return qMax(m_pending, board.pendingBytes()); }
    int write(const uint8_t *data, uint16_t size) override {
        if (m_pending) return 0;
        const int accepted = board.write(data, size);
        if (accepted <= 0) return accepted;
        m_pending = uint16_t(accepted); peakSdkOperations = 1;
        const auto generation = m_generation;
        QTimer::singleShot(2, this, [this, generation]() {
            if (generation != m_generation) return;
            m_pending = 0; emit wrote();
        });
        return accepted;
    }
    void releaseWritten() { holdWritten = false; const auto bytes = held; held.clear(); if (!bytes.isEmpty()) emit bytesReceived(bytes); }
private:
    mlink_reasm_t m_reasm{};
    uint16_t m_pending = 0;
    quint64 m_generation = 0;
};
QVector<int> fullDataFrameSizes(const QByteArray &bytes, uint16_t chunk) {
    mlink_reasm_t reasm{}; mlink_reasm_init(&reasm, 0); QVector<int> lengths;
    for (int offset = 0; offset < bytes.size();) {
        const auto available = uint16_t(qMin(65535, bytes.size() - offset)); uint16_t used = 0;
        const auto rc = mlink_reasm_push(&reasm, reinterpret_cast<const uint8_t *>(bytes.constData()+offset), available, 1, &used);
        if (!used) throw std::runtime_error("Native reassembler made no progress");
        offset += used;
        if (rc != MLINK_OK) continue;
        const auto *pdu = mlink_reasm_pdu(&reasm); mlink_hdr_t header{};
        if (mlink_hdr_decode(&header, pdu) != MLINK_OK) throw std::runtime_error("Invalid captured native frame");
        if (mlink_flags_kind(header.flags) != MLINK_KIND_TLV || !mlink_flags_encrypted(header.flags) ||
            header.body_len != chunk + MLINK_XFER_BLOCK_OVERHEAD + MLINK_ENC_OVERHEAD) continue;
        uint8_t encoded[MLINK_FRAME_MAX]; uint16_t size = 0;
        // This is the documented decoded PDU returned by reasm, not an offset
        // into encrypted/COBS wire bytes or a replacement protocol encoder.
        if (mlink_frame_encode(encoded, sizeof(encoded), &header, pdu + MLINK_HDR_LEN, &size) != MLINK_OK)
            throw std::runtime_error("Native frame encoder rejected its own captured PDU");
        lengths.append(size);
    }
    return lengths;
}
class MissingGrantBackend : public security::AppBackend {
public:
    int fetches = 0, claims = 0;
    void fetchLocal(const security::Target &, const QString &, GrantCallback callback, uint8_t) override {
        ++fetches; security::BackendResult<security::Credential> result;
        result.error = security::BackendErrorKind::Business; result.code = 404; result.message = "synthetic no grant"; callback(result);
    }
    void createClaim(const security::Target &, const QString &, bool, ClaimCallback callback) override {
        ++claims; security::BackendResult<security::ClaimRequest> result;
        result.error = security::BackendErrorKind::Network; result.message = "synthetic stopped before claim transport"; callback(result);
    }
    void confirmClaimRequest(const QByteArray &, const QString &, const security::DeviceProof &, GrantCallback) override { QFAIL("Unexpected claim confirmation"); }
    void recoverClaimRequest(const QByteArray &, const QString &, GrantCallback) override { QFAIL("Unexpected claim recovery"); }
    QByteArray accountUid(const QString &) const override { return QByteArray(16, 'u'); }
};
template<class Controller, class Action> bool flow(Controller &controller, Action action, QString &reason) {
    QEventLoop loop; bool done = false, ok = false;
    const auto connection = QObject::connect(&controller, &Controller::finished, &loop, [&](bool passed, const QString &detail) { done = true; ok = passed; reason = detail; loop.quit(); });
    QTimer timer; timer.setSingleShot(true); QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit); timer.start(45000);
    action(); if (!done) loop.exec(); QObject::disconnect(connection); return done && ok;
}
}

class BatchOtaTests : public QObject {
    Q_OBJECT
private slots:
    void immediateDriveStopsWhenWaitingAtBudgetOrAtReboot() {
        const RttOtaPoint ack{MLINK_OTA_JOB_TRANSFER, MLINK_XFER_TX_WAIT_BLOCK_ACK, 73, 12, 0};
        auto stored = ack; stored.transfer = MLINK_XFER_TX_WAIT_WRITTEN;
        QVERIFY(rttOtaContinue(ack, stored, 1));
        QVERIFY(!rttOtaContinue(ack, ack, 1));
        QVERIFY(!rttOtaContinue(ack, stored, RttOtaPollBudget));
        stored.id++; QVERIFY(!rttOtaContinue(ack, stored, 1)); stored.id--;
        stored.job = MLINK_OTA_JOB_REBOOT_SEND; QVERIFY(!rttOtaContinue(ack, stored, 1));
        QVERIFY(!rttOtaContinue({}, {}, 1));
    }
    void timingMatchesAttemptsAndHandlesStoredBeforeAckWithoutNegativeDurations() {
        QObject owner; auto &trace = RttOtaTrace::forSession(&owner);
        QCOMPARE(RttOtaTrace::existing(&owner), &trace);
        trace.begin(17, true); trace.queued(17, 0, 100, 1000);
        trace.ack(999, MLINK_DONE_OK, 2000); trace.stored(99, 0, 3000); trace.stored(17, 5, 4000);
        QCOMPARE(trace.statistics()["confirmed"].toInt(), 0);
        trace.stored(17, 0, 5000); trace.ack(100, MLINK_DONE_LOST, 6000);
        QCOMPARE(trace.statistics()["confirmed"].toInt(), 0);
        trace.ack(100, MLINK_DONE_OK, 7000); trace.ack(100, MLINK_DONE_OK, 8000);
        trace.queued(17, 1, 101, 10000);
        auto stats = trace.statistics();
        QCOMPARE(stats["confirmed"].toInt(), 1); QCOMPARE(stats["stored_before_ack"].toInt(), 1);
        QCOMPARE(stats["queue_to_ack_ms"].toDouble(), 6.); QCOMPARE(stats["queue_to_store_ms"].toDouble(), 4.);
        QCOMPARE(stats["confirmed_to_next_queue_ms"].toDouble(), 3.);
        QCOMPARE(stats["next_samples"].toInt(), 1);
        trace.ack(101, MLINK_DONE_OK, 11000);
        trace.queued(17, 1, 102, 12000); // Actual retry, not another completed block.
        trace.ack(101, MLINK_DONE_OK, 13000); trace.stored(17, 0, 14000);
        QCOMPARE(trace.statistics()["confirmed"].toInt(), 1);
        QVERIFY(!trace.finish().isEmpty()); QVERIFY(trace.finish().isEmpty());
        QCOMPARE(trace.statistics()["incomplete_attempts"].toInt(), 2);
        QCOMPARE(trace.statistics()["retries"].toInt(), 1);
        trace.begin(18, false); trace.queued(18, 0, 200, 20000);
        trace.ack(102, MLINK_DONE_OK, 21000); trace.stored(17, 1, 22000);
        QCOMPARE(trace.statistics()["confirmed"].toInt(), 0);
        QVERIFY(!trace.statistics()["event_driven"].toBool());
    }
    void attPlannerIncludesEncryptionFramingAndFlashAlignment() {
        QCOMPARE(rttOtaChunk(236, 244, false), uint16_t(216));
        QCOMPARE(rttOtaChunk(236, 244, true), uint16_t(176));
        QCOMPARE(rttOtaFrameBound(216), uint32_t(284));
        QCOMPARE(rttOtaFrameBound(176), uint32_t(243));
        QCOMPARE(rttOtaChunk(236, 20, true), uint16_t(216));
        QCOMPARE(rttOtaChunk(236, 185, true), uint16_t(216));
        QCOMPARE(rttOtaChunk(236, 512, true), uint16_t(216));
        QCOMPARE(rttOtaChunk(236, 0, true), uint16_t(216));
        QCOMPARE(rttOtaChunk(0, 244, true), uint16_t(0));
        for (uint16_t room = 64; room <= 256; ++room) for (uint16_t att = 20; att <= 300; ++att) {
            const auto chunk = rttOtaChunk(room, att, true), original = rttOtaChunk(room, att, false);
            QVERIFY(chunk <= original); QCOMPARE(chunk % 4, 0);
            if (chunk != original) QVERIFY(rttOtaFrameBound(chunk) <= att);
        }
    }
    void compactLoggingSamplesSuccessfulBlocksAndKeepsFailures() {
        int kept = 0;
        for (int block = 0; block <= 10000; ++block)
            kept += rttKeepOtaLog(QStringLiteral("[ota] wrote %1/10000 blocks t=123ms").arg(block), true, true);
        QCOMPARE(kept, 80);
        for (const auto &line : {"[tx] n=244 fixture", "[rx] n=53 fixture", "[rx] ver=1 fixture"}) {
            QVERIFY(!rttKeepOtaLog(line, true, true));
            QVERIFY(rttKeepOtaLog(line, false, true));
            QVERIFY(rttKeepOtaLog(line, true, false));
        }
        for (const auto &line : {"[done] LOST tag=1", "[done] REFUSED tag=1", "[tr] retx dst=1 seq=7 try=2",
                                "[ota] phase=10 code=-1", "[ota] wrote malformed", "[secure] failure", "UNKNOWN version"})
            QVERIFY2(rttKeepOtaLog(line, true, true), line);
        QCOMPARE(rttUnprefixedLog("[batch 12:34:56:78:90:01] [rx] n=53 fixture"), QString("[rx] n=53 fixture"));
        QCOMPARE(rttUnprefixedLog("[batch malformed"), QString("[batch malformed"));
    }
    void unsupportedThroughputRequestDoesNotOpenADevice() {
#ifdef Q_OS_WIN
        RttBleSpeedLease lease; QStringList logs;
        QVERIFY(!lease.request(nullptr, [&](const QString &line) { logs.append(line); }));
        QVERIFY(logs.join('\n').contains("unavailable"));
        QVERIFY(RttBleSpeedLease::describe(nullptr).contains("unavailable"));
        lease.reset(); lease.reset();
#endif
    }
    void transferClockStopsWhileVerificationKeepsSlotAndTotalClock() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs;
        queue.begin(devices(2), image(dir.path()), 1, [&](const auto &, const auto &, QObject *p) {
            auto *j = new FakeTask(p); jobs.append(j); return j;
        });
        QTRY_COMPARE(jobs.size(), 1);
        QCOMPARE(queue.transferElapsedMs(queue.rows()[0]), qint64(0));
        emit jobs[0]->transferStarted(); QTest::qWait(30);
        emit jobs[0]->progress(1000, 1000); emit jobs[0]->transferStopped();
        const auto transfer = queue.transferElapsedMs(queue.rows()[0]);
        QVERIFY(transfer >= 20); const auto total = queue.elapsedMs(queue.rows()[0]);
        QTest::qWait(35); emit jobs[0]->transferStopped(); emit jobs[0]->transferStarted();
        QCOMPARE(queue.transferElapsedMs(queue.rows()[0]), transfer);
        QVERIFY(queue.elapsedMs(queue.rows()[0]) >= total + 20);
        QCOMPARE(queue.running(), 1); QCOMPARE(jobs.size(), 1);
        jobs[0]->complete(RttOtaResult::Success); QTRY_COMPARE(jobs.size(), 2);
        emit jobs[1]->transferStarted(); QTest::qWait(20); jobs[1]->complete(RttOtaResult::Failed);
        QTRY_VERIFY(!queue.active());
        const auto failedTransfer = queue.transferElapsedMs(queue.rows()[1]);
        QTest::qWait(20); QCOMPARE(queue.transferElapsedMs(queue.rows()[1]), failedTransfer);
    }
    void liveDetailedLoggingAlsoAppliesToQueuedTasks() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs; QVector<bool> compact;
        queue.begin(devices(2), image(dir.path()), 1, [&](const auto &, const RttOtaImage &firmware, QObject *p) {
            compact.append(firmware.compactLog); auto *j = new FakeTask(p); jobs.append(j); return j;
        });
        QTRY_COMPARE(jobs.size(), 1); QVERIFY(compact[0]);
        queue.setDetailedLogging(true); QVERIFY(jobs[0]->detailed); QVERIFY(!queue.image().compactLog);
        jobs[0]->complete(RttOtaResult::Success); QTRY_COMPARE(jobs.size(), 2); QVERIFY(!compact[1]);
        queue.setDetailedLogging(false); QVERIFY(!jobs[1]->detailed);
        jobs[1]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
    }
    void validatesImageAndVersion() {
        QTemporaryDir dir; auto firmware = image(dir.path());
        QCOMPARE(firmware.model, QString("SC201-S")); QVERIFY(firmware.unchanged());
        QVERIFY_EXCEPTION_THROWN(RttOtaImage::inspect(firmware.path, ""), std::exception);
        writeImage(firmware.path, security::firmwareModelMarker("SC201-S") + security::firmwareModelMarker("SC202-S"));
        QVERIFY(!firmware.unchanged());
        const auto shared = RttOtaImage::inspect(firmware.path, "V009");
        QCOMPARE(shared.models, QStringList({"SC201-S", "SC202-S"}));
        writeImage(firmware.path, QByteArray("missing exact model marker"));
        QVERIFY_EXCEPTION_THROWN(RttOtaImage::inspect(firmware.path, "V009"), std::exception);
    }
    void concurrencyAndFailureIsolation() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs;
        QVERIFY(queue.begin(devices(), image(dir.path()), 2, [&](const auto &, const auto &, QObject *parent) { auto *job = new FakeTask(parent); jobs.append(job); return job; }));
        QTRY_COMPARE(jobs.size(), 2); QCOMPARE(queue.running(), 2);
        jobs[0]->complete(RttOtaResult::Success);
        QTRY_COMPARE(jobs.size(), 3); QCOMPARE(queue.running(), 2);
        jobs[1]->complete(RttOtaResult::Failed); QVERIFY(queue.active());
        jobs[2]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
        QCOMPARE(int(queue.rows()[0].result), int(RttOtaResult::Success));
        QCOMPARE(int(queue.rows()[1].result), int(RttOtaResult::Failed));
        QCOMPARE(int(queue.rows()[2].result), int(RttOtaResult::Success));
    }
    void transferCompleteStillOccupiesSlotUntilVersionVerdict() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs;
        queue.begin(devices(), image(dir.path()), 2, [&](const auto &, const auto &, QObject *p) { auto *j = new FakeTask(p); jobs.append(j); return j; });
        QTRY_COMPARE(jobs.size(), 2);
        emit jobs[0]->progress(1000, 1000); emit jobs[0]->stage("reboot verification");
        QCoreApplication::processEvents(); QCOMPARE(jobs.size(), 2); QCOMPARE(queue.running(), 2);
        QCOMPARE(int(queue.rows()[0].result), int(RttOtaResult::Running));
        jobs[0]->complete(RttOtaResult::Unknown); QTRY_COMPARE(jobs.size(), 3);
        jobs[1]->complete(RttOtaResult::Success); jobs[2]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
        QCOMPARE(int(queue.rows()[0].result), int(RttOtaResult::Unknown));
    }
    void fourPreparedPeersConnectInOrderBeforeAnyTransferStarts() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<PreparedTask *> jobs;
        QVERIFY(queue.begin(devices(4), image(dir.path()), 4, [&](const auto &, const auto &, QObject *parent) {
            auto *job = new PreparedTask(parent); jobs.append(job); return job;
        }));
        for (int i = 0; i < 4; ++i) {
            QTRY_COMPARE(jobs.size(), i + 1);
            for (auto *job : jobs) QCOMPARE(job->releases, 0);
            jobs[i]->readyForTransfer();
            jobs[i]->readyForTransfer(); // Duplicate preparation cannot release or skip a peer.
        }
        QTRY_COMPARE(jobs[3]->releases, 1);
        for (auto *job : jobs) QCOMPARE(job->releases, 1);
        QCOMPARE(queue.running(), 4);
        for (auto *job : jobs) job->complete(RttOtaResult::Success);
        QTRY_VERIFY(!queue.active());
    }
    void nextPreparedWaveWaitsForAllPreviousVersionVerdicts() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<PreparedTask *> jobs;
        QVERIFY(queue.begin(devices(5), image(dir.path()), 3, [&](const auto &, const auto &, QObject *parent) {
            auto *job = new PreparedTask(parent); jobs.append(job); return job;
        }));
        for (int i = 0; i < 3; ++i) { QTRY_COMPARE(jobs.size(), i+1); jobs[i]->readyForTransfer(); }
        QTRY_COMPARE(jobs[2]->releases, 1);
        jobs[0]->complete(RttOtaResult::Success); QCoreApplication::processEvents(); QCOMPARE(jobs.size(), 3);
        jobs[1]->complete(RttOtaResult::Unknown); QCoreApplication::processEvents(); QCOMPARE(jobs.size(), 3);
        jobs[2]->complete(RttOtaResult::Success);
        QTRY_COMPARE(jobs.size(), 4); jobs[3]->readyForTransfer();
        QTRY_COMPARE(jobs.size(), 5); QCOMPARE(jobs[3]->releases, 0); jobs[4]->readyForTransfer();
        QTRY_COMPARE(jobs[4]->releases, 1);
        jobs[3]->complete(RttOtaResult::Success); jobs[4]->complete(RttOtaResult::Success);
        QTRY_VERIFY(!queue.active());
    }
    void preparationFailureAndCancellationDoNotDeadlockOrStartCancelledPeers() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<PreparedTask *> jobs;
        QVERIFY(queue.begin(devices(4), image(dir.path()), 4, [&](const auto &, const auto &, QObject *parent) {
            auto *job = new PreparedTask(parent); jobs.append(job); return job;
        }));
        QTRY_COMPARE(jobs.size(), 1); jobs[0]->complete(RttOtaResult::Failed);
        QTRY_COMPARE(jobs.size(), 2); jobs[1]->readyForTransfer();
        QTRY_COMPARE(jobs.size(), 3); queue.cancelRows({2,3});
        jobs[2]->complete(RttOtaResult::Cancelled);
        QTRY_COMPARE(jobs[1]->releases, 1); QCOMPARE(jobs.size(), 3);
        jobs[1]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
        QCOMPARE(int(queue.rows()[3].result), int(RttOtaResult::Cancelled));
    }
    void cancellationWaitsForAcknowledgementAndNeverStartsQueuedDevices() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs;
        queue.begin(devices(5), image(dir.path()), 2, [&](const auto &, const auto &, QObject *p) { auto *j = new FakeTask(p); jobs.append(j); return j; });
        QTRY_COMPARE(jobs.size(), 2); queue.cancelAll();
        QVERIFY(queue.active()); QCOMPARE(queue.running(), 2); QVERIFY(jobs[0]->cancelled && jobs[1]->cancelled);
        for (int i = 2; i < 5; ++i) QCOMPARE(int(queue.rows()[i].result), int(RttOtaResult::Cancelled));
        jobs[0]->complete(RttOtaResult::Cancelled); jobs[1]->complete(RttOtaResult::Unknown);
        QTRY_VERIFY(!queue.active()); QCOMPARE(jobs.size(), 2);
    }
    void selectedCancellationDoesNotCancelOtherTasks() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs;
        queue.begin(devices(4), image(dir.path()), 2, [&](const auto &, const auto &, QObject *p) { auto *j = new FakeTask(p); jobs.append(j); return j; });
        QTRY_COMPARE(jobs.size(), 2); queue.cancelRows({0, 2});
        QVERIFY(jobs[0]->cancelled); QVERIFY(!jobs[1]->cancelled);
        jobs[0]->complete(RttOtaResult::Cancelled); QTRY_COMPARE(jobs.size(), 3);
        QCOMPARE(int(queue.rows()[2].result), int(RttOtaResult::Cancelled));
        jobs[1]->complete(RttOtaResult::Success); jobs[2]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
    }
    void lateCallbacksCannotChangeFinalVerdictOrNewBatch() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs; auto firmware = image(dir.path());
        auto factory = [&](const auto &, const auto &, QObject *p) { auto *j = new FakeTask(p); jobs.append(j); return j; };
        queue.begin(devices(1), firmware, 1, factory); QTRY_COMPARE(jobs.size(), 1);
        jobs[0]->complete(RttOtaResult::Failed); emit jobs[0]->progress(900, 900); jobs[0]->complete(RttOtaResult::Success);
        QCOMPARE(int(queue.rows()[0].result), int(RttOtaResult::Failed)); QCOMPARE(queue.rows()[0].done, quint32(0));
        QTRY_VERIFY(!queue.active()); QVERIFY(queue.begin(devices(1), firmware, 1, factory));
        QTRY_COMPARE(jobs.size(), 2); jobs[1]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
    }
    void validatesConcurrencyAndRejectsDuplicateAddresses() {
        QTemporaryDir dir; RttBatchOtaController queue; auto firmware = image(dir.path());
        auto factory = [](const auto &, const auto &, QObject *p) -> RttOtaTask * { return new FakeTask(p); };
        QVERIFY(!queue.begin({}, firmware, 2, factory)); QVERIFY(!queue.begin(devices(), firmware, 0, factory));
        QVERIFY(!queue.begin(devices(), firmware, 5, factory));
        auto duplicate = devices(1); duplicate.append(duplicate.first()); QVERIFY(!queue.begin(duplicate, firmware, 2, factory));
        QVERIFY(queue.begin(devices(), firmware, 1, factory)); QVERIFY(!queue.begin(devices(), firmware, 2, factory));
        queue.cancelAll(); QTRY_VERIFY(!queue.active());
    }
    void sequentialAndConstructionFailureDoNotDeadlock() {
        QTemporaryDir dir; RttBatchOtaController queue; QVector<FakeTask *> jobs; int calls = 0;
        queue.begin(devices(), image(dir.path()), 1, [&](const auto &, const auto &, QObject *p) -> RttOtaTask * {
            if (++calls == 1) throw std::runtime_error("isolated construction failure");
            auto *j = new FakeTask(p); jobs.append(j); return j;
        });
        QTRY_COMPARE(jobs.size(), 1); QCOMPARE(queue.running(), 1); QCOMPARE(calls, 2);
        jobs[0]->complete(RttOtaResult::Success); QTRY_COMPARE(jobs.size(), 2);
        jobs[1]->complete(RttOtaResult::Success); QTRY_VERIFY(!queue.active());
        QCOMPARE(int(queue.rows()[0].result), int(RttOtaResult::Failed));
    }
    void realAppWorkflowNeverClaimsWithoutExplicitContinuation() {
        QTemporaryDir dir; auto backend = std::make_unique<MissingGrantBackend>(); auto *observed = backend.get();
        auto session = std::make_unique<MlinkSession>(); IdentityPort port; QVERIFY(session->setTransport(&port));
        RttDeviceOtaTask task(devices(1).first(), image(dir.path()), std::move(backend), std::move(session), "fixture-account", dir.path() + "/cache");
        bool done = false; RttOtaResult result = RttOtaResult::Running;
        connect(&task, &RttOtaTask::finished, this, [&](RttOtaResult value, const QString &) { done = true; result = value; });
        task.start(); QTRY_VERIFY(done);
        QCOMPARE(int(result), int(RttOtaResult::NeedsBinding)); QCOMPARE(observed->claims, 0); QCOMPARE(port.writes, 0); QVERIFY(!port.ready);
    }
    void explicitBindingContinuationUsesOriginalAppWorkflowOnce() {
        QTemporaryDir dir; auto backend = std::make_unique<MissingGrantBackend>(); auto *observed = backend.get();
        auto session = std::make_unique<MlinkSession>(); IdentityPort port; QVERIFY(session->setTransport(&port));
        auto device = devices(1).first(); device.bindingApproved = true;
        RttDeviceOtaTask task(device, image(dir.path()), std::move(backend), std::move(session), "fixture-account", dir.path() + "/cache");
        bool done = false; connect(&task, &RttOtaTask::finished, this, [&](RttOtaResult, const QString &) { done = true; });
        task.start(); QTRY_VERIFY(done); QCOMPARE(observed->claims, 1); QCOMPARE(port.writes, 0);
    }
    void wrongPublicModelFailsBeforeCredentialFetchOrWrites() {
        QTemporaryDir dir; auto backend = std::make_unique<MissingGrantBackend>(); auto *observed = backend.get();
        auto session = std::make_unique<MlinkSession>(); IdentityPort port; port.model = "SC200-D"; port.serial = "M21010000A01005"; QVERIFY(session->setTransport(&port));
        RttDeviceOtaTask task(devices(1).first(), image(dir.path()), std::move(backend), std::move(session), "fixture-account", dir.path() + "/cache");
        bool done = false; RttOtaResult result = RttOtaResult::Running;
        connect(&task, &RttOtaTask::finished, this, [&](RttOtaResult value, const QString &) { done = true; result = value; });
        task.start(); QTRY_VERIFY(done); QCOMPARE(int(result), int(RttOtaResult::Failed)); QCOMPARE(observed->fetches, 0); QCOMPARE(port.writes, 0);
    }
    void changedImageFailsBeforeOpeningTransport() {
        QTemporaryDir dir; auto firmware = image(dir.path()); auto backend = std::make_unique<MissingGrantBackend>();
        auto session = std::make_unique<MlinkSession>(); IdentityPort port; QVERIFY(session->setTransport(&port));
        RttDeviceOtaTask task(devices(1).first(), firmware, std::move(backend), std::move(session), "fixture-account", dir.path() + "/cache");
        writeImage(firmware.path, QByteArray("changed")); bool done = false; RttOtaResult result = RttOtaResult::Running;
        connect(&task, &RttOtaTask::finished, this, [&](RttOtaResult value, const QString &) { done = true; result = value; });
        task.start(); QTRY_VERIFY(done); QCOMPARE(int(result), int(RttOtaResult::Failed)); QCOMPARE(port.opens, 0); QCOMPARE(port.writes, 0);
    }
    void offlineMissingCacheDoesNotFetchOrClaim() {
        QTemporaryDir dir; auto backend = std::make_unique<MissingGrantBackend>(); auto *observed = backend.get();
        auto session = std::make_unique<MlinkSession>(); IdentityPort port; QVERIFY(session->setTransport(&port));
        RttDeviceOtaTask task(devices(1).first(), image(dir.path()), std::move(backend), std::move(session), "fixture-account", dir.path() + "/cache", false);
        bool done = false; connect(&task, &RttOtaTask::finished, this, [&](RttOtaResult, const QString &) { done = true; });
        task.start(); QTRY_VERIFY(done); QCOMPARE(observed->fetches, 0); QCOMPARE(observed->claims, 0); QCOMPARE(port.writes, 0);
    }
    void twoRealTlsSessionsTransferRebootAndVerifyIndependently_data() {
        QTest::addColumn<int>("att"); QTest::addColumn<bool>("fitted"); QTest::addColumn<bool>("drive");
        QTest::addColumn<int>("count");
        QTest::newRow("small-mtu-original-plan") << 20 << true << true << 2;
        QTest::newRow("mtu247-original-blocks-baseline-drive") << 244 << false << false << 2;
        QTest::newRow("mtu247-original-blocks-immediate-drive") << 244 << false << true << 2;
        QTest::newRow("mtu247-fitted-blocks") << 244 << true << true << 2;
        QTest::newRow("four-prepared-peers-original-blocks") << 244 << false << true << 4;
    }
    void actualAckWithoutStoredReportCannotAdvanceAndCancellationIsIsolated_data() {
        QTest::addColumn<bool>("cancelHeld");
        QTest::newRow("release-written-and-verify") << false;
        QTest::newRow("cancel-held-device-keep-peer-running") << true;
    }
    void actualAckWithoutStoredReportCannotAdvanceAndCancellationIsIsolated() {
        QFETCH(bool, cancelHeld);
        try {
        QTemporaryDir dir; QVERIFY(dir.isValid());
        const auto backendPath = dir.path() + "/backend", cache = dir.path() + "/cache";
        security::BackendService backend(backendPath); backend.importBuiltInFactory(); backend.createAccount("gated-fixture");
        const auto selected = devices(2);
        std::array<std::unique_ptr<GatedAsyncScale>, 2> ports;
        for (int i = 0; i < 2; ++i) {
            ports[i] = std::make_unique<GatedAsyncScale>(dir.path() + QStringLiteral("/board%1").arg(i));
            MlinkSession session; QVERIFY(session.setTransport(ports[i].get()));
            FactoryController factory(session, backend); AppController app(session, backend, cache);
            const security::Target target{"DFT-SC201-S", QStringLiteral("M22010000A0100%1").arg(i+5)};
            QString reason;
            QVERIFY2(flow(factory, [&]() { factory.start(FactoryController::Goal::Provision, target, "M21010000A01000", selected[i].info); }, reason), qPrintable(reason));
            ports[i]->board.localOpenClaim();
            QVERIFY2(flow(app, [&]() { app.claimDevice(target, "gated-fixture", selected[i].info); }, reason), qPrintable(reason));
            app.cancel(); QTest::qWait(30);
        }
        const auto cacheBefore = readFile(cache + "/credentials.json"); QVERIFY(!cacheBefore.isEmpty());
        const auto path = dir.path() + "/upgrade.bin";
        writeImage(path, SimulatedScale::firmwareImage("V009", "SC201-S"));
        const auto firmware = RttOtaImage::inspect(path, "V009");
        ports[0]->holdWritten = true;
        std::array<QPointer<MlinkSession>, 2> sessions;
        RttBatchOtaController queue; int created = 0;
        QVERIFY(queue.begin(selected, firmware, 2, [&](const RttOtaDevice &device, const RttOtaImage &spec, QObject *parent) -> RttOtaTask * {
            const int i = created++; auto session = std::make_unique<MlinkSession>();
            if (!session->setTransport(ports[i].get())) throw std::runtime_error("Cannot attach gated native transport");
            sessions[i] = session.get();
            return new RttDeviceOtaTask(device, spec, std::make_unique<security::BackendService>(backendPath),
                std::move(session), "gated-fixture", cache, true, parent);
        }));
        QTRY_VERIFY_WITH_TIMEOUT(!ports[0]->held.isEmpty(), 15000);
        QVERIFY(sessions[0]); auto *trace = RttOtaTrace::existing(sessions[0]); QVERIFY(trace);
        QTRY_VERIFY_WITH_TIMEOUT(trace->statistics()["ack_received"].toUInt() >= 1, 1000);
        QCOMPARE(trace->statistics()["stored_received"].toUInt(), 0u);
        QCOMPARE(trace->statistics()["confirmed"].toUInt(), 0u);
        QCOMPARE(trace->statistics()["submissions"].toUInt(), 1u);
        QTest::qWait(60);
        QCOMPARE(ports[0]->board.otaBlocks(), 1u);
        QCOMPARE(trace->statistics()["submissions"].toUInt(), 1u);
        QVERIFY(trace->statistics()["max_passes_per_turn"].toUInt() <= RttOtaPollBudget);
        QCOMPARE(queue.rows()[0].result, RttOtaResult::Running);
        if (cancelHeld) queue.cancelRows({0});
        ports[0]->releaseWritten();
        QTRY_VERIFY_WITH_TIMEOUT(!queue.active(), 45000);
        QVERIFY(ports[0]->validFrames && ports[1]->validFrames);
        QCOMPARE(ports[0]->peakSdkOperations, 1); QCOMPARE(ports[1]->peakSdkOperations, 1);
        QVERIFY2(queue.rows()[1].result == RttOtaResult::Success, qPrintable(queue.rows()[1].detail));
        QCOMPARE(ports[1]->board.firmware(), QString("V009")); QVERIFY(ports[1]->board.reboots() > 0);
        if (cancelHeld) {
            QCOMPARE(queue.rows()[0].result, RttOtaResult::Cancelled);
            QCOMPARE(ports[0]->board.reboots(), 0u);
        } else {
            QVERIFY2(queue.rows()[0].result == RttOtaResult::Success, qPrintable(queue.rows()[0].detail));
            QCOMPARE(ports[0]->board.firmware(), QString("V009")); QVERIFY(ports[0]->board.reboots() > 0);
        }
        QCOMPARE(readFile(cache + "/credentials.json"), cacheBefore);
        } catch (const std::exception &error) { QFAIL(error.what()); }
    }
    void twoRealTlsSessionsTransferRebootAndVerifyIndependently() {
        QFETCH(int, att); QFETCH(bool, fitted); QFETCH(bool, drive); QFETCH(int, count);
        try {
        QTemporaryDir dir; QVERIFY(dir.isValid()); const QString backendPath = dir.path() + "/backend", cache = dir.path() + "/app-cache";
        security::BackendService backend(backendPath); backend.importBuiltInFactory(); backend.createAccount("batch-fixture");
        auto selected = devices(count);
        std::array<std::unique_ptr<ReportedAttScale>, 4> scales;
        std::array<int, 4> captureOffsets{};
        for (int i = 0; i < count; ++i) {
            scales[i] = std::make_unique<ReportedAttScale>(dir.path() + QStringLiteral("/device%1").arg(i), "M21010000A01000", true);
            scales[i]->payload = uint16_t(att);
            scales[i]->suppressWeightReports(true);
            MlinkSession session; QVERIFY(session.setTransport(scales[i].get()));
            FactoryController factory(session, backend); AppController app(session, backend, cache);
            const security::Target target{"DFT-SC201-S", QStringLiteral("M22010000A0100%1").arg(i+5)};
            QString reason;
            QVERIFY2(flow(factory, [&]() { factory.start(FactoryController::Goal::Provision, target, "M21010000A01000", selected[i].info); }, reason), qPrintable(reason));
            scales[i]->localOpenClaim();
            QVERIFY2(flow(app, [&]() { app.claimDevice(target, "batch-fixture", selected[i].info); }, reason), qPrintable(reason));
            app.cancel(); QTest::qWait(30);
            captureOffsets[i] = scales[i]->capturedClientWire().size();
        }
        const auto cacheBefore = readFile(cache + "/credentials.json"); QVERIFY(!cacheBefore.isEmpty());
        const auto path = dir.path() + "/upgrade.bin"; writeImage(path, SimulatedScale::firmwareImage("V009", "SC201-S"));
        auto firmware = RttOtaImage::inspect(path, "V009"); firmware.fitAtt = fitted; firmware.eventDriven = drive;
        RttBatchOtaController queue; int created = 0, peak = 0;
        std::array<QVariantMap, 4> timing;
        connect(&queue, &RttBatchOtaController::changed, this, [&]() { peak = qMax(peak, queue.running()); });
        QVERIFY(queue.begin(selected, firmware, count, [&](const RttOtaDevice &device, const RttOtaImage &spec, QObject *parent) -> RttOtaTask * {
            const int index = created++; auto session = std::make_unique<MlinkSession>();
            if (!session->setTransport(scales[index].get())) throw std::runtime_error("Cannot attach isolated simulated transport");
            auto *observed = session.get();
            connect(observed, &MlinkSession::otaRebooted, this, [&, index, observed](const QString &) {
                if (auto *trace = RttOtaTrace::existing(observed)) timing[index] = trace->statistics();
            });
            return new RttDeviceOtaTask(device, spec, std::make_unique<security::BackendService>(backendPath), std::move(session), "batch-fixture", cache, true, parent);
        }));
        QTRY_VERIFY_WITH_TIMEOUT(!queue.active(), 45000);
        QCOMPARE(peak, count); QCOMPARE(created, count);
        for (int i = 0; i < count; ++i) {
            QVERIFY2(queue.rows()[i].result == RttOtaResult::Success, qPrintable(queue.rows()[i].detail));
            QCOMPARE(scales[i]->firmware(), QString("V009")); QVERIFY(scales[i]->reboots() > 0); QVERIFY(scales[i]->otaBlocks() > 0);
            QVERIFY(queue.rows()[i].transferStartedMs >= 0); QVERIFY(queue.rows()[i].transferFinishedMs >= 0);
            QVERIFY(queue.transferElapsedMs(queue.rows()[i]) <= queue.elapsedMs(queue.rows()[i]));
            QVERIFY(!timing[i].isEmpty()); QCOMPARE(timing[i]["event_driven"].toBool(), drive);
            QCOMPARE(timing[i]["confirmed"].toUInt(), scales[i]->otaBlocks());
            QVERIFY(timing[i]["max_passes_per_turn"].toUInt() <= (drive ? RttOtaPollBudget : 1u));
            QVERIFY(timing[i]["confirmed_to_next_queue_ms"].toDouble() >= 0.);
            if (drive) QVERIFY(timing[i]["extra_passes"].toUInt() > 0);
            else QCOMPARE(timing[i]["extra_passes"].toUInt(), 0u);
            qInfo("OTA owner timing: drive=%d confirmed=%u queue_ack=%.3fms queue_store=%.3fms confirmed_next=%.3fms max_passes=%u", drive,
                timing[i]["confirmed"].toUInt(), timing[i]["queue_to_ack_ms"].toDouble(), timing[i]["queue_to_store_ms"].toDouble(),
                timing[i]["confirmed_to_next_queue_ms"].toDouble(), timing[i]["max_passes_per_turn"].toUInt());
            const auto chunk = rttOtaChunk(236, uint16_t(att), fitted);
            const auto frames = fullDataFrameSizes(scales[i]->capturedClientWire().mid(captureOffsets[i]), chunk);
            QCOMPARE(frames.size(), int(readFile(path).size() / chunk));
            QVERIFY(!frames.isEmpty());
            for (int size : frames) {
                QVERIFY(uint32_t(size) <= rttOtaFrameBound(chunk));
                if (att == 244) QCOMPARE((size + att - 1) / att, fitted ? 1 : 2);
            }
            qInfo("Captured actual native encrypted data frames: ATT=%d chunk=%d full_frames=%d max_frame_bound=%u", att, chunk, frames.size(), rttOtaFrameBound(chunk));
        }
        QCOMPARE(readFile(cache + "/credentials.json"), cacheBefore);
        } catch (const std::exception &error) { QFAIL(error.what()); }
    }
};
QTEST_GUILESS_MAIN(BatchOtaTests)
#include "test_batch_ota.moc"
