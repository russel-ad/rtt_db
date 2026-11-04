#pragma once
#include <QString>
#include <QRegularExpression>

// Keep failures, retries, target admission and reboot/version verdicts.
// Packet bytes and per-block successful progress are optional diagnostics.
inline bool rttKeepOtaLog(const QString &line, bool compact, bool transferring)
{
    if (!compact || !transferring) return true;
    if (line.startsWith("[tx] n=") || line.startsWith("[rx] n=") || line.startsWith("[rx] ver=")) return false;
    if (line.startsWith("[ota] wrote ")) {
        static const QRegularExpression progress(QStringLiteral("^\\[ota\\] wrote ([0-9]+)/([0-9]+) blocks"));
        const auto match = progress.match(line);
        if (match.hasMatch()) {
            const auto done = match.captured(1).toUInt(), total = match.captured(2).toUInt();
            return done == 0 || done >= total || done % 128 == 0;
        }
    }
    return true;
}

inline QString rttUnprefixedLog(const QString &line)
{
    if (line.startsWith("[batch ")) {
        const int end = line.indexOf("] ");
        if (end > 0) return line.mid(end + 2);
    }
    return line;
}
