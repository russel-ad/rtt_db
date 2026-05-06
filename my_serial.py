# my_serial.py
import serial
import serial.tools.list_ports
import threading

def serial_find():
    """获取系统可用串口列表，返回 (描述列表, 端口名列表)"""
    ports = serial.tools.list_ports.comports()
    desc_list = []
    name_list = []
    for p in ports:
        desc_list.append(f"{p.description} ({p.device})")
        name_list.append(p.device)
    return desc_list, name_list

def ser_buad_list_adjust(baud, baud_list):
    """调整波特率列表：将当前使用的波特率移到最前面，并去重"""
    baud = int(baud)
    if baud in baud_list:
        baud_list.remove(baud)
    baud_list.insert(0, baud)
    seen = set()
    new_list = []
    for b in baud_list:
        if b not in seen:
            seen.add(b)
            new_list.append(b)
    return new_list

class SimpleSerial:
    def __init__(self, error_callback, warn_callback, char_format='utf-8'):
        self.error_callback = error_callback
        self.warn_callback = warn_callback
        self.char_format = char_format
        self.ser = None
        self.read_buffer = bytearray()
        self.lock = threading.Lock()
        self.timestamp_enabled = False
        self.wave_callback = None

    def reg_dlog_M_callback(self, callback):
        self.wave_callback = callback

    def hw_open(self, port, baudrate, **kwargs):
        try:
            self.ser = serial.Serial(port, baudrate, timeout=0.1, **kwargs)
            return True
        except Exception as e:
            self.error_callback(str(e))
            return False

    def hw_close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None

    def hw_is_open(self):
        return self.ser is not None and self.ser.is_open

    def hw_read(self):
        if not self.hw_is_open():
            return
        try:
            data = self.ser.read(4096)
            if data:
                if len(data) > 0:
                    print(f"[DEBUG] Received {len(data)} bytes")  # 调试输出，证明收到了数据
                with self.lock:
                    self.read_buffer.extend(data)
        except Exception as e:
            self.error_callback(str(e))

    def read_data_queue(self, output_list):
        with self.lock:
            if not self.read_buffer:
                return
            data = bytes(self.read_buffer)
            self.read_buffer.clear()

        if self.char_format == 'hex':
            hex_str = ' '.join(f'{b:02X}' for b in data)
            output_list.append(hex_str)
        else:
            try:
                text = data.decode(self.char_format, errors='replace')
                output_list.append(text)
            except:
                output_list.append(str(data))

        if self.wave_callback is not None:
            self.wave_callback(data)

    def hw_write(self, data):
        if not self.hw_is_open():
            return
        try:
            if isinstance(data, list):
                data = bytes(data)
            self.ser.write(data)
        except Exception as e:
            self.error_callback(str(e))

    def get_read_data_time_interval_s(self):
        return 0.01

    def open_timestamp(self):
        self.timestamp_enabled = True

    def close_timestamp(self):
        self.timestamp_enabled = False

    def get_hw_serial_number(self):
        return None

# 保持与原 bds_serial 接口一致的类名
BDS_Serial = SimpleSerial