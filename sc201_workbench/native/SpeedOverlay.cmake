# These are build-local adapter/diagnostic edits only. Product protocol and
# the frozen libmlink/crypto sources are never patched.
function(rtt_speed_replace file before after)
  file(READ "${file}" text)
  string(FIND "${text}" "${before}" location)
  if(location EQUAL -1)
    message(FATAL_ERROR "Speed integration marker changed in ${file}; review source before building")
  endif()
  string(REPLACE "${before}" "${after}" text "${text}")
  file(WRITE "${file}" "${text}")
endfunction()

# Selection indexes still refer to the original unfiltered device vector.
# Hidden rows cannot be connected through the ordinary connection entry.
set(window "${RTT_WORKBENCH_OVERLAY}/MainWindow.cpp")
rtt_speed_replace("${window}" "void MainWindow::onConnect()\n{" [=[void MainWindow::onConnect()
{
    if (!m_dev->currentItem() || m_dev->currentItem()->isHidden()) {
        m_state->setText(QStringLiteral("请先选择列表中可见的蓝牙设备"));
        return;
    }]=])
rtt_speed_replace("${window}" "m_conn->setEnabled(!flow && (!m_sessionOpen || (m_sess->authenticated() && production)));"
    "m_conn->setEnabled(!flow && m_dev->currentItem() && !m_dev->currentItem()->isHidden() && (!m_sessionOpen || (m_sess->authenticated() && production)));")

set(session "${RTT_WORKBENCH_OVERLAY}/MlinkSession.cpp")
rtt_speed_replace("${session}" "#include \"BleLink.h\"" "#include \"BleLink.h\"\n#include \"ota_tuning.h\"\n#include \"ota_drive.h\"\n#include <QVariant>")
rtt_speed_replace("${session}" "m_poll.setInterval(kPollMs);" "m_poll.setInterval(kPollMs);\n\tm_poll.setTimerType(Qt::PreciseTimer);")
rtt_speed_replace("${session}" "void MlinkSession::postLog(const QString &line)\n{" "void MlinkSession::postLog(const QString &line)\n{\n\tif (!rttKeepOtaLog(line, property(\"rttCompactOtaLog\").toBool(), otaBusy())) return;")
rtt_speed_replace("${session}" "if (n > 0 && buf) {" "if (n > 0 && buf && !(self->otaBusy() && self->property(\"rttCompactOtaLog\").toBool())) {")
rtt_speed_replace("${session}" "if (describeFrame(raw, static_cast<uint16_t>(chunk.size()), t, &desc, &quiet)) {" "if (otaBusy() && property(\"rttCompactOtaLog\").toBool()) {\n\t\t// Byte delivery and protocol polling below are unchanged.\n\t} else if (describeFrame(raw, static_cast<uint16_t>(chunk.size()), t, &desc, &quiet)) {")

# The owner still performs the complete guarded poll sequence. Repeat only
# after an actual transfer-state change, never recursively from a callback,
# and never across a connection/attempt change or into the reboot exchange.
rtt_speed_replace("${session}" [=[QScopedValueRollback<bool> polling(m_polling, true);
	const auto generation = m_linkGeneration;]=] [=[QScopedValueRollback<bool> polling(m_polling, true);
	const auto generation = m_linkGeneration;
    const bool rttImmediate = property("rttOtaEventDrive").toBool();
    const unsigned rttBudget = rttImmediate ? RttOtaPollBudget : 1u;
    for (unsigned rttPass = 0; rttPass < rttBudget; ++rttPass) {
    const auto rttBefore = rttOtaPoint(m_otaJobActive ? &m_tx : nullptr);
    if (auto *trace = RttOtaTrace::existing(this)) trace->pass(rttPass + 1u);]=])
rtt_speed_replace("${session}" [=[	pollOta();
	otaReport();
}

/* The library owns the transfer's reports]=] [=[	pollOta();
    const auto rttAfter = rttOtaPoint(m_otaJobActive ? &m_tx : nullptr);
    if (auto *trace = RttOtaTrace::existing(this))
        trace->transition(rttBefore, rttAfter, trace->nowUs());
	otaReport();
    if (!m_inited || generation != m_linkGeneration || !m_otaJobActive ||
        !rttImmediate || !rttOtaContinue(rttBefore, rttAfter, rttPass + 1u)) break;
    }
}

/* The library owns the transfer's reports]=])
rtt_speed_replace("${session}" [=[	if (self->m_otaJobActive)
		mlink_ota_job_on_done(&self->m_tx, idx, tag, result, reason);]=] [=[	if (self->m_otaJobActive) {
        mlink_ota_job_on_done(&self->m_tx, idx, tag, result, reason);
        if (auto *trace = RttOtaTrace::existing(self)) trace->ack(tag, result, trace->nowUs());
    }]=])
rtt_speed_replace("${session}" [=[	if (self->m_otaJobActive && mlink_ota_job_on_tlv(&self->m_tx, idx, h, body, len) != 0)
		return;]=] [=[	if (self->m_otaJobActive && mlink_ota_job_on_tlv(&self->m_tx, idx, h, body, len) != 0) {
        // Measure only a report already accepted by the original job. Decode
        // through the frozen public API, with no raw/encrypted byte offsets.
        mlink_cmd_xfer_wrote_t wrote{};
        if (mlink_xfer_wrote_decode(body, len, &wrote) == MLINK_OK && wrote.mt == MLINK_MT_RPT)
            if (auto *trace = RttOtaTrace::existing(self))
                trace->stored(wrote.transfer_id, wrote.idx, trace->nowUs());
        return;
    }]=])

set(ota "${RTT_WORKBENCH_OVERLAY}/MlinkSessionOta.cpp")
rtt_speed_replace("${ota}" "#include \"SessionTransport.h\"" "#include \"SessionTransport.h\"\n#include \"ota_att_plan.h\"\n#include \"ota_drive.h\"\n#include <QVariant>")
rtt_speed_replace("${ota}" "const uint16_t chunk = uint16_t(mlink_xfer_block_max(outMax) & ~uint16_t(3));" "const uint16_t chunk = rttOtaChunk(outMax, m_transport ? m_transport->attPayload() : 0, property(\"rttFitOtaToAtt\").toBool());")
rtt_speed_replace("${ota}" "m_scratch.resize(int(chunk));" [=[m_scratch.resize(int(chunk));
    const auto att = m_transport ? m_transport->attPayload() : 0;
    emit logLine(QStringLiteral("[ota-att] payload=%1 original_chunk=%2 selected_chunk=%3 max_wire=%4 data_write_parts=%5 optimized=%6")
        .arg(att).arg(rttOtaChunk(outMax, att, false)).arg(chunk).arg(rttOtaFrameBound(chunk))
        .arg(att ? (rttOtaFrameBound(chunk) + att - 1u) / att : 0u)
        .arg(property("rttFitOtaToAtt").toBool()));]=])
rtt_speed_replace("${ota}" [=[	emit logLine(QStringLiteral("[ota] start size=%1 crc=%2 chunk=%3 blocks=%4 id=%5")]=] [=[    RttOtaTrace::forSession(this).begin(m_tx.transfer.transfer_id, property("rttOtaEventDrive").toBool());
    emit logLine(QStringLiteral("[ota-drive] enabled=%1 poll_budget=%2")
        .arg(property("rttOtaEventDrive").toBool()).arg(RttOtaPollBudget));
	emit logLine(QStringLiteral("[ota] start size=%1 crc=%2 chunk=%3 blocks=%4 id=%5")]=])
rtt_speed_replace("${ota}" [=[			self->m_blockLog = idx;]=] [=[			self->m_blockLog = idx;
            if (auto *trace = RttOtaTrace::existing(self)) {
                const auto timing = trace->progress(idx, self->m_tx.transfer.n_blocks);
                if (!timing.isEmpty()) self->postLog(timing);
            }]=])
rtt_speed_replace("${ota}" [=[	m_reportedSt = st;
	if (st == uint8_t(MLINK_OTA_JOB_REBOOT_SENT))]=] [=[	m_reportedSt = st;
    if (st == MLINK_OTA_JOB_REBOOT_SENT || st == MLINK_OTA_JOB_FAILED || st == MLINK_OTA_JOB_UNKNOWN)
        if (auto *trace = RttOtaTrace::existing(this)) {
            const auto timing = trace->finish();
            if (!timing.isEmpty()) postLog(timing);
        }
	if (st == uint8_t(MLINK_OTA_JOB_REBOOT_SENT))]=])

set(window "${RTT_WORKBENCH_OVERLAY}/MainWindow.cpp")
rtt_speed_replace("${window}" "#include \"MainWindow.h\"" "#include \"MainWindow.h\"\n#include \"ota_tuning.h\"")
rtt_speed_replace("${window}" "bool isDetailedLog(const QString &line)\n{" "bool isDetailedLog(const QString &line)\n{\n\tconst auto unprefixed = rttUnprefixedLog(line);\n\tif (unprefixed != line) return isDetailedLog(unprefixed);")

set(header "${RTT_WORKBENCH_OVERLAY}/BleWinrtGatt.h")
rtt_speed_replace("${header}" "void start(const QBluetoothAddress &addr);" "void start(const QBluetoothAddress &addr);\n\tvoid setThroughputPreferred(bool enabled);")
set(gatt "${RTT_WORKBENCH_OVERLAY}/BleWinrtGatt.cpp")
rtt_speed_replace("${gatt}" "#include \"PublicIdentity.h\"" "#include \"PublicIdentity.h\"\n#include \"ble_speed.h\"\n#include <QVariant>\n#include <chrono>")
rtt_speed_replace("${gatt}" "bool sessTokSet = false;" "bool sessTokSet = false;\n\tevent_token performanceTok{};\n\tbool performanceTokSet = false;\n\tRttBleSpeedLease speedLease;\n\tvoid tuneSpeed();")
rtt_speed_replace("${gatt}" "void unhook()\n\t{" "void unhook()\n\t{\n\t\ttry { if (dev && performanceTokSet) dev.ConnectionParametersChanged(performanceTok); } catch (...) {}\n\t\tperformanceTokSet = false;\n\t\tspeedLease.reset();")
rtt_speed_replace("${gatt}" "self->sessionDone = true;" "self->sessionDone = true;\n\t\t\t\tself->tuneSpeed();")
# Measure actual SDK completion and the Qt dispatch delay separately. This
# adds counters, not another pending operation or a local write queue.
rtt_speed_replace("${gatt}" "RttBleSpeedLease speedLease;" "RttBleSpeedLease speedLease;\n\tquint64 sdkCount = 0, sdkBytes = 0, sdkUs = 0, dispatchUs = 0, sdkMaxUs = 0;")
rtt_speed_replace("${gatt}" "auto op = p->wr.WriteValueWithResultAsync(" "const auto submitted = std::chrono::steady_clock::now();\n\t\tauto op = p->wr.WriteValueWithResultAsync(")
rtt_speed_replace("${gatt}" "op.Completed([p, n](auto const &async, AsyncStatus st) {" "op.Completed([p, n, submitted](auto const &async, AsyncStatus st) {\n\t\t\tconst auto completed = std::chrono::steady_clock::now();\n\t\t\tconst auto sdkTime = std::chrono::duration_cast<std::chrono::microseconds>(completed - submitted).count();")
rtt_speed_replace("${gatt}" "p->qt([p, n, st, gs, proto, ok]() {" "p->qt([p, n, st, gs, proto, ok, completed, sdkTime]() {")
rtt_speed_replace("${gatt}" "p->q->m_wrBusy = false;" [=[++p->sdkCount; p->sdkBytes += n; p->sdkUs += quint64(sdkTime);
                p->sdkMaxUs = qMax(p->sdkMaxUs, quint64(sdkTime));
                p->dispatchUs += quint64(std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - completed).count());
                p->q->m_wrBusy = false;]=])
rtt_speed_replace("${gatt}" "performanceTokSet = false;\n\t\tspeedLease.reset();" [=[performanceTokSet = false;
        speedLease.reset();
        if (sdkCount && q) emit q->logLine(QStringLiteral("[ble-perf] write_summary count=%1 bytes=%2 sdk_avg_ms=%3 sdk_max_ms=%4 qt_dispatch_avg_ms=%5")
            .arg(sdkCount).arg(sdkBytes).arg(double(sdkUs) / double(sdkCount) / 1000., 0, 'f', 3)
            .arg(double(sdkMaxUs) / 1000., 0, 'f', 3).arg(double(dispatchUs) / double(sdkCount) / 1000., 0, 'f', 3));]=])
file(READ "${gatt}" text)
set(implementation [=[
void BleWinrtGatt::Impl::tuneSpeed()
{
    if (closed.load() || !dev || !q) return;
    std::weak_ptr<Impl> weak = shared_from_this();
    if (q->property("rttPreferThroughput").toBool())
        speedLease.request(dev, [weak](const QString &line) { if (auto self = weak.lock()) self->log(line); });
    else log(QStringLiteral("[ble-perf] current %1").arg(RttBleSpeedLease::describe(dev)));
    try {
        if (!performanceTokSet && dev.try_as<IBluetoothLEDevice6>()) {
            performanceTok = dev.ConnectionParametersChanged([weak](auto const &, auto const &) {
                if (auto self = weak.lock()) self->qt([self]() {
                    if (!self->closed.load()) self->log(QStringLiteral("[ble-perf] actual %1").arg(RttBleSpeedLease::describe(self->dev)));
                });
            });
            performanceTokSet = true;
        }
    } catch (...) { log(QStringLiteral("[ble-perf] parameter change notifications unavailable")); }
}

]=])
string(FIND "${text}" "BleWinrtGatt::BleWinrtGatt(QObject *parent)" location)
if(location EQUAL -1)
  message(FATAL_ERROR "BleWinrtGatt constructor marker changed")
endif()
string(SUBSTRING "${text}" 0 ${location} before)
string(SUBSTRING "${text}" ${location} -1 after)
set(text "${before}${implementation}${after}")
file(WRITE "${gatt}" "${text}")
file(APPEND "${gatt}" [=[

void BleWinrtGatt::setThroughputPreferred(bool enabled)
{
    const bool previous = property("rttPreferThroughput").toBool();
    setProperty("rttPreferThroughput", enabled);
#ifdef Q_OS_WIN
    if (!d || d->closed.load()) return;
    if (enabled && !previous) d->tuneSpeed();
    else if (!enabled && previous) { d->speedLease.reset(); d->log(QStringLiteral("[ble-perf] throughput preference released")); }
#endif
}
]=])
