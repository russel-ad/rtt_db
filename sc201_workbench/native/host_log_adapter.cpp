// Reuse the original rotating/flush-on-write logger verbatim. A standalone
// process releases its file on exit; the embedded widget also needs that
// release when closing or changing its explicit workbench directory.
#include "HostLog.cpp"

void rttCloseHostLog()
{
    auto &log = logger();
    QMutexLocker lock(&log.mutex);
    log.file.close();
    log.directory.clear();
    log.stem.clear();
    log.part = 0;
}
