#include "ble_speed.h"
#ifdef Q_OS_WIN
using namespace winrt::Windows::Devices::Bluetooth;

QString RttBleSpeedLease::describe(const Device &device) noexcept
{
    try {
        if (!device || !device.try_as<IBluetoothLEDevice6>()) return QStringLiteral("connection parameters unavailable on this OS");
        const auto parameters = device.GetConnectionParameters();
        if (!parameters) return QStringLiteral("connection parameters not available yet");
        return QStringLiteral("interval=%1ms latency=%2 timeout=%3ms")
            .arg(double(parameters.ConnectionInterval()) * 1.25, 0, 'f', 2)
            .arg(parameters.ConnectionLatency()).arg(quint32(parameters.LinkTimeout()) * 10);
    } catch (...) { return QStringLiteral("connection parameters unavailable"); }
}

bool RttBleSpeedLease::request(const Device &device, const Log &log)
{
    reset();
    try {
        if (!device || !device.try_as<IBluetoothLEDevice6>()) {
            log(QStringLiteral("[ble-perf] ThroughputOptimized unavailable; keeping current Windows connection settings"));
            return false;
        }
        const auto preferred = BluetoothLEPreferredConnectionParameters::ThroughputOptimized();
        m_request = device.RequestPreferredConnectionParameters(preferred);
        const auto status = m_request ? m_request.Status() : BluetoothLEPreferredConnectionParametersRequestStatus::Unspecified;
        QString statusName;
        switch (status) {
        case BluetoothLEPreferredConnectionParametersRequestStatus::Success: statusName = "Success"; break;
        case BluetoothLEPreferredConnectionParametersRequestStatus::DeviceNotAvailable: statusName = "DeviceNotAvailable"; break;
        case BluetoothLEPreferredConnectionParametersRequestStatus::AccessDenied: statusName = "AccessDenied"; break;
        default: statusName = "Unspecified"; break;
        }
        log(QStringLiteral("[ble-perf] ThroughputOptimized request=%1 preferred=%2..%3ms; actual %4")
            .arg(statusName).arg(double(preferred.MinConnectionInterval()) * 1.25, 0, 'f', 2)
            .arg(double(preferred.MaxConnectionInterval()) * 1.25, 0, 'f', 2).arg(describe(device)));
        return status == BluetoothLEPreferredConnectionParametersRequestStatus::Success;
    } catch (const winrt::hresult_error &error) {
        log(QStringLiteral("[ble-perf] preference not applied: %1; keeping current Windows settings")
            .arg(QString::fromWCharArray(error.message().c_str())));
    } catch (...) { log(QStringLiteral("[ble-perf] preference not applied; keeping current Windows settings")); }
    reset();
    return false;
}

void RttBleSpeedLease::reset() noexcept
{
    try { if (m_request) m_request.Close(); } catch (...) {}
    m_request = nullptr;
}
#endif
