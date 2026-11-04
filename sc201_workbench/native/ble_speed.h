#pragma once
#include <QtGlobal>
#include <QString>
#include <functional>

#ifdef Q_OS_WIN
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Devices.Bluetooth.h>

// Own the Windows request until transfer completion or this GATT generation
// closes. This does not create devices, change framing or open another ATT op.
class RttBleSpeedLease {
public:
    using Device = winrt::Windows::Devices::Bluetooth::BluetoothLEDevice;
    using Log = std::function<void(const QString &)>;
    ~RttBleSpeedLease() { reset(); }
    bool request(const Device &, const Log &);
    void reset() noexcept;
    static QString describe(const Device &) noexcept;
private:
    winrt::Windows::Devices::Bluetooth::BluetoothLEPreferredConnectionParametersRequest m_request{nullptr};
};
#endif
