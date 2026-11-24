"""Two independent J-Link sessions for the exact UART1 diagnostic images."""

from contextlib import contextmanager

import pylink


ADDRESSES = {'H': 0x2000707C, 'S': 0x200085E0}


def new_client():
    # Keep independent DLL state even on SDK versions where the driver uses
    # process-wide selection. Each session closes before leaving the worker.
    try:
        return pylink.JLink(use_tmpcpy=True, open_tunnel=None)
    except (OSError, TypeError) as error:
        raise RuntimeError('无法加载 J-Link DLL，请安装 Windows x64 J-Link 软件包') from error


def serial_numbers():
    client = new_client()
    return sorted({int(info.SerialNumber) for info in client.connected_emulators() if info.SerialNumber})


class Link:
    def __init__(self, client):
        self.client = client

    def read(self):
        return bytes(self.client.rtt_read(0, 4096))

    def write(self, data):
        return self.client.rtt_write(0, data)


@contextmanager
def attach(role, serial):
    client = new_client()
    client.open(serial_no=serial)
    try:
        if client.serial_number != serial:
            raise RuntimeError('连接的探针序列号与选择不一致')
        client.exec_command('InhibitConnectRetries = 1')
        client.set_tif(pylink.enums.JLinkInterfaces.SWD)
        client.set_speed(1000)
        client.connect('Cortex-M4', speed=1000)
        if client.halted():
            raise RuntimeError('目标 CPU 已暂停，请退出其他调试会话后重试')
        address = ADDRESSES[role]
        if bytes(client.memory_read8(address, 10)) != b'SEGGER RTT':
            raise RuntimeError('未找到测试固件的 RTT 控制块，请核对探针接线及 UART1 固件')
        client.rtt_start(address)
        try:
            yield Link(client)
        finally:
            client.rtt_stop()
    finally:
        client.close()
