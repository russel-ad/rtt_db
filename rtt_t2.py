import sys
import json
import logging as log
import threading
import time
import re
import datetime
import os
import bds.bds_jlink as bds_jk
import multiprocessing
import my_serial as bds_ser
import bds.bds_waveform as wv
import requests
import webbrowser
import keyboard
import config_manager
import traceback
from text_searcher import TextSearcher

# PyQt5 导入
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5 import QtCore

# 全局变量
global main_window
global download_window
global hw_obj
global log_remain_str
global real_time_save_file_name

thread_lock = threading.Lock()
download_thread_lock = threading.Lock()
download_thread_lock.acquire()

color_pat = re.compile(r'BDSCOL\((\d{1,8})\)', re.I)

# 颜色定义
COLOR_BLACK = '#000000'
COLOR_RED = '#FF3030'
COLOR_BLUE = '#00008B'
COLOR_GREEN = '#008B00'
COLOR_PURPLE = '#9932CC'


# 工具函数
def extract_and_convert_hex(string):
    pattern = r"(0x[0-9A-Fa-f]+)\s+(0x[0-9A-Fa-f]+)"
    match = re.search(pattern, string)
    if match:
        hex1, hex2 = match.groups()
        if int(hex1, 16) % 4 != 0:
            return None
        return hex1, hex2
    else:
        return None


def delete_str(pat, s, reverse=False, s_sub=''):
    matches = re.findall(pat, s)
    if reverse:
        matches = [match for match in matches if s_sub in match]
    for match in matches:
        s = s.replace(match, '')
    return s


def remove_line(patterns, line, only_exclude):
    if not patterns:
        return line
    if any(pattern in line for pattern in patterns) != only_exclude:
        return line
    else:
        return ''


def log_fileter(log_data, filter_pat, remain_str='', only_exclude=True):
    raw_s = remain_str + ''.join(log_data)
    log_lines = raw_s.split('\n')
    filtered_lines = []
    updated_remain_str = ''

    last_index = len(log_lines) - 1
    for idx, line in enumerate(log_lines):
        if idx < last_index:
            filtered_line = remove_line(filter_pat, line + '\n', only_exclude)
            filtered_lines.append(filtered_line)
        else:
            if line.endswith('\n'):
                filtered_line = remove_line(filter_pat, line + '\n', only_exclude)
                filtered_lines.append(filtered_line)
            else:
                updated_remain_str = line

    new_s = ''.join(filtered_lines)
    return new_s, updated_remain_str


def db_data_check_error(s):
    err_cnt = 0
    err_chr = chr(0)
    for v in s:
        if v > '~' or v == err_chr:
            err_cnt += 1
            if err_cnt > 100:
                return True
    return False


def update_user_input_list(input_data, user_input_list):
    if not isinstance(input_data, str) or input_data.strip() == '':
        return user_input_list

    # 简单的去重和限制长度
    if input_data in user_input_list:
        user_input_list.remove(input_data)
    user_input_list.insert(0, input_data)

    # 保持列表长度不超过30
    if len(user_input_list) > 30:
        user_input_list = user_input_list[:30]

    return user_input_list

# PyQt5 主窗口类
class MainWindow(QMainWindow):
    def __init__(self, js_cfg):
        super().__init__()
        self.js_cfg = js_cfg
        self.hw_obj = None
        self.log_remain_str = ''
        self.real_time_save_file_name = ''
        self.init_ui()

    def init_ui(self):
        # 设置窗口属性
        self.setWindowTitle('RTT调试工具 v2.7.0')
        self.setGeometry(100, 100, 1200, 800)

        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)

        # 第一行：按钮
        button_layout = QHBoxLayout()

        self.connect_btn = QPushButton('J_Link连接')
        self.connect_btn.clicked.connect(self.on_connect)

        self.timestamp_btn = QPushButton('打开时间戳')
        self.timestamp_btn.clicked.connect(self.on_timestamp)

        self.save_realtime_btn = QPushButton('实时保存数据')
        self.save_realtime_btn.clicked.connect(self.on_save_realtime)

        self.save_all_btn = QPushButton('保存全部数据')
        self.save_all_btn.clicked.connect(self.on_save_all)

        self.skip_btn = QPushButton('一键跳转')
        self.skip_btn.clicked.connect(self.on_skip)

        self.config_btn = QPushButton('配置')
        self.config_btn.clicked.connect(self.on_config)

        self.wave_btn = QPushButton('波形绘制')
        self.wave_btn.clicked.connect(self.on_wave)

        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.timestamp_btn)
        button_layout.addWidget(self.save_realtime_btn)
        button_layout.addWidget(self.save_all_btn)
        button_layout.addWidget(self.skip_btn)
        button_layout.addStretch()
        button_layout.addWidget(self.config_btn)
        button_layout.addWidget(self.wave_btn)

        # 过滤设置
        filter_group = QGroupBox("过滤设置")
        filter_layout = QHBoxLayout()

        filter_label = QLabel("过滤:")
        self.filter_input = QLineEdit()
        self.filter_input.setText(self.js_cfg.get('filter', ''))

        self.filter_enable = QCheckBox("打开过滤器")
        self.filter_enable.stateChanged.connect(self.on_filter_changed)

        self.filter_inverse = QCheckBox("取反过滤器")

        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.filter_input)
        filter_layout.addWidget(self.filter_enable)
        filter_layout.addWidget(self.filter_inverse)
        filter_group.setLayout(filter_layout)

        # 日志显示区域
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Courier New", 10))

        # 数据发送区域
        send_layout = QHBoxLayout()

        self.send_btn = QPushButton("发送数据")
        self.send_btn.clicked.connect(self.on_send_data)

        self.data_type_combo = QComboBox()
        self.data_type_combo.addItems(['ASC', 'HEX'])

        self.data_input = QTextEdit()
        self.data_input.setMaximumHeight(100)

        send_layout.addWidget(self.send_btn)
        send_layout.addWidget(self.data_type_combo)
        send_layout.addWidget(self.data_input, 1)

        # 历史数据
        history_layout = QHBoxLayout()
        history_label = QLabel("历史数据:")
        self.history_combo = QComboBox()

        # 确保user_input_data存在
        if 'user_input_data' not in self.js_cfg:
            self.js_cfg['user_input_data'] = []
        self.history_combo.addItems(self.js_cfg['user_input_data'])
        self.history_combo.currentTextChanged.connect(self.on_history_selected)

        history_layout.addWidget(history_label)
        history_layout.addWidget(self.history_combo, 1)

        # 添加到主布局
        main_layout.addLayout(button_layout)
        main_layout.addWidget(filter_group)
        main_layout.addWidget(self.log_text, 1)
        main_layout.addLayout(send_layout)
        main_layout.addLayout(history_layout)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # 菜单栏
        self.create_menu()

        # 启动硬件读取线程
        self.start_hw_thread()

    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('文件')

        clear_action = QAction('清除窗口数据', self)
        clear_action.triggered.connect(self.on_clear_data)
        file_menu.addAction(clear_action)

        scroll_action = QAction('滚动到最底端', self)
        scroll_action.triggered.connect(self.on_scroll_bottom)
        file_menu.addAction(scroll_action)

        exit_action = QAction('退出', self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def start_hw_thread(self):
        self.hw_thread = threading.Thread(target=self.hw_rx_thread, daemon=True)
        self.hw_thread.start()

    def hw_rx_thread(self):
        while True:
            try:
                thread_lock.acquire()
                if self.hw_obj:
                    self.hw_obj.hw_read()
                    run_interval_s = self.hw_obj.get_read_data_time_interval_s()
            finally:
                thread_lock.release()
            time.sleep(run_interval_s if 'run_interval_s' in locals() else 1)

    def log_process(self):
        if not self.hw_obj:
            return

        # 1. 从硬件读取原始数据
        raw_log = []
        self.hw_obj.read_data_queue(raw_log)
        if not raw_log:
            return

        # 2. HEX 模式直接输出
        if self.js_cfg.get('char_format', 'utf-8') == 'hex':
            for hex_line in raw_log:
                self.log_text.append(hex_line)
            return

        # 3. 合并本次数据，并与未完成的行拼接
        combined = self.log_remain_str + ''.join(raw_log)
        self.log_remain_str = ''  # 先清空，后续若有不完整行再重新赋值

        # 4. 清理可能的 b'...' 转义
        import re
        combined = re.sub(r"b'([^']*)'", lambda m: m.group(1), combined)
        combined = re.sub(r'b"([^"]*)"', lambda m: m.group(1), combined)
        combined = combined.replace('\\n', ' ').replace('\\r', ' ').replace('\\t', ' ')

        # 5. 分割行（保留空行，因为空行可能是有意义的）
        lines = combined.splitlines(keepends=False)

        # 6. 处理最后一行是否完整
        if not combined.endswith('\n'):
            # 最后一行不完整，保存到缓存
            if lines:
                self.log_remain_str = lines[-1]
                lines = lines[:-1]
            else:
                # 全部不完整（可能数据量小），直接作为一行输出（避免永久缓存）
                if len(combined) > 0:
                    # 强制输出这一条不完整行，并清空缓存
                    lines = [combined]
                    self.log_remain_str = ''
                else:
                    return

        # 7. 如果没有完整行且没有强制输出，返回
        if not lines:
            return

        # 8. 过滤器
        filter_pat = []
        only_exclude = True
        if self.filter_enable.isChecked():
            filter_pat = self.filter_input.text().split('&&')
            if self.filter_inverse.isChecked():
                only_exclude = False

        # 9. 逐行处理并显示
        last_time = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if filter_pat and (any(p in line for p in filter_pat) == only_exclude):
                continue

            # 精确到微秒的时间戳
            now = datetime.datetime.now()
            time_str = now.strftime("%H:%M:%S.%f")
            # 避免同一批次内微秒重复
            if time_str == last_time:
                base, frac = time_str.split('.')
                micro = int(frac.ljust(6, '0')[:6]) + 1
                if micro >= 1000000:
                    micro = 0
                time_str = f"{base}.{micro:06d}"
            last_time = time_str

            display_line = f"[{time_str}] [REV] {line}"
            self.log_text.append(display_line)

            # 实时保存
            if self.save_realtime_btn.text() == '关闭实时保存数据':
                self.save_log_to_file(display_line + '\n')

    def append_log_with_color(self, log_text):
        # 简单的颜色处理（可根据需要完善）
        self.log_text.append(log_text)

    def save_log_to_file(self, log_text):
        if self.save_realtime_btn.text() == '关闭实时保存数据':
            try:
                if not self.real_time_save_file_name:
                    self.real_time_save_file_name = self.get_log_filename()
                with open(self.real_time_save_file_name, 'a', encoding='utf-8') as f:
                    f.write(log_text)
            except Exception as e:
                log.info(f'Write real time data to file error: {e}')

    def get_log_filename(self):
        log_dir = config_manager.ensure_log_dir()
        return os.path.join(log_dir, f'real_time_log_{datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}.txt')

    def on_connect(self):
        """
        支持 J-Link/串口 连接/断开，自动切换硬件对象
        """
        print(f"hw_sel = {self.js_cfg.get('hw_sel', '1')}")
        print(f"hw_obj type = {type(self.hw_obj)}")
        try:
            # 波形复位
            try:
                wv.wave_cmd('wave reset')
            except:
                pass

            # ===== 根据配置决定使用哪种硬件对象 =====
            desired_hw = self.js_cfg.get('hw_sel', '1')  # '1'=J-Link, '2'=串口

            # 如果当前的硬件对象类型与期望的不符，则切换
            if desired_hw == '1':  # 期望 J-Link
                if not isinstance(self.hw_obj, bds_jk.BDS_Jlink):
                    if self.hw_obj and self.hw_obj.hw_is_open():
                        self.hw_obj.hw_close()
                    self.hw_obj = bds_jk.BDS_Jlink(
                        hw_error, hw_warn,
                        char_format=self.js_cfg.get('char_format', 'utf-8')
                    )
                    self.timestamp_btn.setText("打开时间戳")
                    self.timestamp_btn.setStyleSheet("")
            else:  # 期望串口
                if not isinstance(self.hw_obj, bds_ser.BDS_Serial):
                    if self.hw_obj and self.hw_obj.hw_is_open():
                        self.hw_obj.hw_close()
                    self.hw_obj = bds_ser.BDS_Serial(
                        hw_error, hw_warn,
                        char_format=self.js_cfg.get('char_format', 'utf-8')
                    )
                    self.timestamp_btn.setText("打开时间戳")
                    self.timestamp_btn.setStyleSheet("")

            # ===== 连接/断开操作 =====
            # J-Link 分支
            if desired_hw == '1':
                # 已连接则断开
                if self.hw_obj and self.hw_obj.hw_is_open():
                    self.hw_obj.hw_close()
                    self.connect_btn.setText("J_Link连接")
                    self.connect_btn.setStyleSheet("")
                    self.timestamp_btn.setText("打开时间戳")
                    self.timestamp_btn.setStyleSheet("")
                    self.status_bar.showMessage("J_Link已断开")
                    log.info("J_Link断开连接")
                    time.sleep(0.1)
                    return

                # 新连接
                jk_con_reset = self.js_cfg.get('jk_con_reset', False)
                jk_speed = int(self.js_cfg.get('jk_speed', 4000))
                chip = self.js_cfg['jk_chip'][0]
                start_address = None
                range_size = 0
                rtt_cfg = self.js_cfg.get('rtt_block_address', ['', ''])
                if len(rtt_cfg) >= 2 and rtt_cfg[0] and rtt_cfg[1]:
                    start_address = int(rtt_cfg[0], 16)
                    range_size = int(rtt_cfg[1], 16)

                result = self.hw_obj.hw_open(
                    speed=jk_speed,
                    chip=chip,
                    reset_flag=jk_con_reset,
                    start_address=start_address,
                    range_size=range_size
                )
                if result:
                    self.connect_btn.setText("J_Link断开")
                    self.connect_btn.setStyleSheet("background-color: green; color: white;")
                    self.status_bar.showMessage("J_Link连接成功")
                    self.log_text.append(f"[J_Link LOG] sn:{self.hw_obj.get_hw_serial_number()}")
                    self.log_text.append(f"[J_Link LOG] 芯片型号:{chip}")
                    if jk_con_reset:
                        self.log_text.append("[J_Link LOG] J_Link复位MCU")
                    else:
                        self.log_text.append("[J_Link LOG] J_Link未复位MCU")
                else:
                    QMessageBox.warning(self, "错误", "J_Link连接失败")

            # ===== 串口分支 =====
            else:
                # 若已连接，则断开
                if self.hw_obj and self.hw_obj.hw_is_open():
                    self.hw_obj.hw_close()
                    self.connect_btn.setText("串口连接")
                    self.connect_btn.setStyleSheet("")
                    self.timestamp_btn.setText("打开时间戳")
                    self.timestamp_btn.setStyleSheet("")
                    self.status_bar.showMessage("串口已断开")
                    log.info("串口断开连接")
                    time.sleep(0.1)
                    return

                # 新连接
                ser_port = self.js_cfg.get('ser_port', '').strip()
                if not ser_port:
                    QMessageBox.warning(self, "错误", "未配置串口端口，请先在配置中选择串口！")
                    return

                baud = int(self.js_cfg.get('ser_baud', [115200])[0])

                # 调用封装的串口打开
                result = self.hw_obj.hw_open(ser_port, baud)
                if result:
                    self.connect_btn.setText("串口断开")
                    self.connect_btn.setStyleSheet("background-color: green; color: white;")
                    self.status_bar.showMessage("串口连接成功")
                    self.log_text.append(f"[Serial LOG] 串口:{ser_port}")
                    self.log_text.append(f"[Serial LOG] 波特率:{baud}")
                else:
                    QMessageBox.warning(self, "错误", f"串口连接失败\n端口: {ser_port}\n波特率: {baud}")

        except Exception as e:
            try:
                if self.hw_obj:
                    self.hw_obj.hw_close()
            except:
                pass
            QMessageBox.critical(self, "连接异常", str(e))

    def on_timestamp(self):
        """
        RTT/串口日志时间戳开关
        对应原PySimpleGUI逻辑：
            打开时间戳 -> hw_obj.open_timestamp()
            关闭时间戳 -> hw_obj.close_timestamp()
        """

        try:
            # 硬件未连接
            if not self.hw_obj or not self.hw_obj.hw_is_open():
                QMessageBox.warning(self, "警告", "请先连接硬件！")
                return

            # 当前按钮状态判断
            if self.timestamp_btn.text() == "打开时间戳":

                # 打开底层时间戳
                if hasattr(self.hw_obj, "open_timestamp"):
                    self.hw_obj.open_timestamp()
                else:
                    QMessageBox.warning(self, "错误", "当前硬件对象不支持时间戳功能")
                    return

                # 更新UI
                self.timestamp_btn.setText("关闭时间戳")
                self.timestamp_btn.setStyleSheet(
                    "background-color: green; color: white;"
                )

                self.js_cfg['timestamp_en'] = True
                self.status_bar.showMessage("时间戳已开启")

            else:

                # 关闭底层时间戳
                if hasattr(self.hw_obj, "close_timestamp"):
                    self.hw_obj.close_timestamp()
                else:
                    QMessageBox.warning(self, "错误", "当前硬件对象不支持时间戳功能")
                    return

                # 更新UI
                self.timestamp_btn.setText("打开时间戳")
                self.timestamp_btn.setStyleSheet("")

                self.js_cfg['timestamp_en'] = False
                self.status_bar.showMessage("时间戳已关闭")

            # 保存配置
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(
                    self.js_cfg,
                    f,
                    indent=4,
                    ensure_ascii=False
                )

        except Exception as e:
            QMessageBox.critical(
                self,
                "时间戳异常",
                f"时间戳切换失败:\n{str(e)}"
            )

    def on_save_realtime(self):
        if self.save_realtime_btn.text() == '实时保存数据':
            self.save_realtime_btn.setText('关闭实时保存数据')
            self.real_time_save_file_name = self.get_log_filename()
            self.status_bar.showMessage("开始实时保存数据")
        else:
            self.save_realtime_btn.setText('实时保存数据')
            self.status_bar.showMessage("停止实时保存数据")

    def on_save_all(self):
        log_text = self.log_text.toPlainText()
        if log_text:
            filename = self.get_log_filename().replace('real_time', 'all')
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(log_text)
            self.status_bar.showMessage(f"数据已保存到: {filename}")
            # 打开文件所在目录
            os.startfile(os.path.dirname(filename))
        else:
            QMessageBox.warning(self, "警告", "无数据可保存！")

    def on_skip(self):
        try:
            current_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
            log_dir = os.path.join(current_dir, 'aaa_log')
            os.startfile(log_dir)
        except Exception as e:
            QMessageBox.warning(self, "错误", str(e))

    def on_config(self):
        # 打开配置对话框
        dialog = ConfigDialog(self.js_cfg, self)
        if dialog.exec_():
            # 保存配置
            with open('config.json', 'w') as f:
                json.dump(self.js_cfg, f, indent=4)
            self.status_bar.showMessage("配置已保存")

    def on_wave(self):
        if not self.hw_obj or not self.hw_obj.hw_is_open():
            QMessageBox.warning(self, "警告", "请先连接硬件！")
            return

        curves_name = self.js_cfg.get('curves_name', '')
        if not curves_name:
            QMessageBox.warning(self, "警告", "没有设置任何轴名称，请至少设置一个轴名称")
            return

        # 启动波形绘制
        try:
            y_range = self.js_cfg.get('y_range', [0, 100])
            y_label_text = self.js_cfg.get('y_label_text', '')
            curves_list = [v for v in curves_name.split('&&') if v != '']

            wv.wave_cmd('wave reset')
            wv.startup_wave(y_range, y_label_text, curves_list)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"启动波形失败: {str(e)}")

    def on_send_data(self):
        if not self.hw_obj or not self.hw_obj.hw_is_open():
            QMessageBox.warning(self, "警告", "请先连接硬件！")
            return

        input_data = self.data_input.toPlainText()
        if not input_data:
            return

        # 更新历史数据
        user_input_data = self.js_cfg.get('user_input_data', [])
        user_input_data = update_user_input_list(input_data, user_input_data)
        self.js_cfg['user_input_data'] = user_input_data
        self.history_combo.clear()
        self.history_combo.addItems(user_input_data)

        # 发送数据
        try:
            send_display = ""  # 用于在日志中显示的字符串
            if self.data_type_combo.currentText() == 'HEX':
                # HEX格式发送
                hex_list = [int(i, 16) for i in input_data.split() if i]
                send_bytes = bytes(hex_list)
                self.hw_obj.hw_write(hex_list)  # 原接口接受列表
                send_display = ' '.join(f'{b:02X}' for b in send_bytes)
            else:
                # ASC格式发送
                line_break = self.js_cfg.get('line_break', '')
                data_to_send = input_data + line_break
                # 根据当前字符编码转换为字节
                encoding = self.js_cfg.get('char_format', 'utf-8')
                if encoding == 'asc':
                    encoding = 'ascii'
                send_bytes = data_to_send.encode(encoding, errors='replace')
                self.hw_obj.hw_write(list(send_bytes))
                # 显示时去掉换行符，避免日志换行混乱
                send_display = input_data  # 不显示附加的换行

            # 在日志区域显示发送的数据
            now = datetime.datetime.now()
            timestamp = f"{now.strftime('%H:%M:%S')}.{int(now.microsecond / 1000):03d}"
            self.log_text.append(f"[{timestamp}] [SEND] {send_display}")
            self.status_bar.showMessage("数据发送成功")

            # 自动滚动到底部（可选）
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )
        except Exception as e:
            QMessageBox.warning(self, "错误", f"发送失败: {str(e)}")

    def on_history_selected(self, text):
        if text:
            self.data_input.setPlainText(text)

    def on_filter_changed(self, state):
        filter_text = self.filter_input.text()
        self.js_cfg['filter'] = filter_text
        self.js_cfg['filter_en'] = (state == Qt.Checked)

        # 保存配置
        with open('config.json', 'w') as f:
            json.dump(self.js_cfg, f, indent=4)

    def on_clear_data(self):
        self.log_text.clear()
        self.status_bar.showMessage("日志已清空")

    def on_scroll_bottom(self):
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )
        self.status_bar.showMessage("已滚动到底部")

    def timerEvent(self, event):
        # 定期处理日志
        self.log_process()

    def closeEvent(self, event):
        # 清理资源
        if self.hw_obj:
            self.hw_obj.hw_close()
        # 保存配置
        with open('config.json', 'w') as f:
            json.dump(self.js_cfg, f, indent=4)
        event.accept()


# 配置对话框
class ConfigDialog(QDialog):
    def __init__(self, js_cfg, parent=None):
        super().__init__(parent)
        self.js_cfg = js_cfg
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("硬件接口配置")
        self.setModal(True)
        self.setMinimumSize(600, 500)

        # 创建滚动区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 创建内容部件
        content_widget = QWidget()
        main_layout = QVBoxLayout(content_widget)

        # 接口选择
        interface_group = QGroupBox("接口选择")
        interface_layout = QHBoxLayout()

        self.jk_radio = QRadioButton("J_Link")
        self.ser_radio = QRadioButton("串口")

        if self.js_cfg.get('hw_sel', '1') == '1':
            self.jk_radio.setChecked(True)
        else:
            self.ser_radio.setChecked(True)

        self.jk_radio.toggled.connect(self.on_interface_changed)

        interface_layout.addWidget(self.jk_radio)
        interface_layout.addWidget(self.ser_radio)
        interface_layout.addStretch()
        interface_group.setLayout(interface_layout)

        # J-Link配置
        self.jk_group = QGroupBox("J_Link配置")
        jk_layout = QFormLayout()

        # 芯片选择
        chip_list = self.js_cfg.get('jk_chip', ['STM32F103C8', 'STM32F407VE', 'STM32F103ZE', 'STM32F407ZG'])
        self.chip_combo = QComboBox()
        self.chip_combo.addItems(chip_list)

        # 速度
        self.jk_speed = QLineEdit(str(self.js_cfg.get('jk_speed', 4000)))
        self.jk_speed.setValidator(QIntValidator(100, 20000))

        # SN号（可选）
        self.jk_sn_label = QLabel("SN:")
        self.jk_sn_input = QLineEdit()
        self.jk_sn_input.setPlaceholderText("可选，留空自动检测")

        # 复位选项
        self.jk_reset = QCheckBox("连接时复位")
        self.jk_reset.setChecked(self.js_cfg.get('jk_con_reset', False))

        # RTT地址搜索范围
        self.rtt_address = QLineEdit()
        rtt_addr = self.js_cfg.get('rtt_block_address', ['', ''])
        if rtt_addr[0] and rtt_addr[1]:
            self.rtt_address.setText(f"{rtt_addr[0]} {rtt_addr[1]}")
        else:
            self.rtt_address.setPlaceholderText("例如: 0x20000000 0x4000")

        jk_layout.addRow("芯片型号:", self.chip_combo)
        jk_layout.addRow("速度(kHz):", self.jk_speed)
        jk_layout.addRow(self.jk_sn_label, self.jk_sn_input)
        jk_layout.addRow(self.jk_reset)
        jk_layout.addRow("RTT地址搜索范围:", self.rtt_address)
        self.jk_group.setLayout(jk_layout)

        # ========== 串口配置（修复版本）==========
        self.ser_group = QGroupBox("串口配置")
        ser_layout = QFormLayout()

        # 获取串口列表（描述列表和名称列表）
        com_des_list, com_name_list = bds_ser.serial_find()
        self.com_name_list = com_name_list
        if not com_des_list:
            com_des_list = ['未找到串口']

        # 创建下拉框
        self.com_combo = QComboBox()
        self.com_combo.addItems(com_des_list)

        # 尝试恢复之前保存的端口
        saved_port = self.js_cfg.get('ser_port', '')
        if saved_port and saved_port in self.com_name_list:
            idx = self.com_name_list.index(saved_port)
            if idx < self.com_combo.count():
                self.com_combo.setCurrentIndex(idx)

        # 波特率
        baud_list = self.js_cfg.get('ser_baud',
                                    ['115200', '9600', '19200', '38400', '57600', '115200', '230400', '460800',
                                     '921600', '2000000'])
        self.baud_combo = QComboBox()
        self.baud_combo.addItems([str(b) for b in baud_list])

        # 刷新按钮
        refresh_btn = QPushButton("刷新串口列表")
        refresh_btn.clicked.connect(self.refresh_ports)

        ser_layout.addRow("串口:", self.com_combo)
        ser_layout.addRow("波特率:", self.baud_combo)
        ser_layout.addRow(refresh_btn)

        self.ser_group.setLayout(ser_layout)
        # ======================================

        # 波形图配置
        wave_group = QGroupBox("波形图配置")
        wave_layout = QFormLayout()

        self.y_min_input = QLineEdit(str(self.js_cfg.get('y_range', [-100, 100])[0]))
        self.y_max_input = QLineEdit(str(self.js_cfg.get('y_range', [-100, 100])[1]))

        self.y_label_input = QLineEdit(self.js_cfg.get('y_label_text', '数值'))
        self.curves_name_input = QLineEdit(self.js_cfg.get('curves_name', ''))
        self.curves_name_input.setPlaceholderText("多个曲线用 && 分隔")

        wave_layout.addRow("Y轴最小值:", self.y_min_input)
        wave_layout.addRow("Y轴最大值:", self.y_max_input)
        wave_layout.addRow("Y轴名称:", self.y_label_input)
        wave_layout.addRow("曲线名称:", self.curves_name_input)
        wave_group.setLayout(wave_layout)

        # 字符编码格式
        char_group = QGroupBox("字符编码格式")
        char_layout = QHBoxLayout()

        self.utf8_check = QCheckBox("UTF-8")
        self.asc_check = QCheckBox("ASC")
        self.hex_check = QCheckBox("HEX")
        self.gb2312_check = QCheckBox("GB2312")

        char_format = self.js_cfg.get('char_format', 'utf-8')
        if char_format == 'utf-8':
            self.utf8_check.setChecked(True)
        elif char_format == 'asc':
            self.asc_check.setChecked(True)
        elif char_format == 'hex':
            self.hex_check.setChecked(True)
        elif char_format == 'gb2312':
            self.gb2312_check.setChecked(True)

        # 互斥选择
        self.utf8_check.toggled.connect(lambda checked: self.on_char_format_changed('utf-8', checked))
        self.asc_check.toggled.connect(lambda checked: self.on_char_format_changed('asc', checked))
        self.hex_check.toggled.connect(lambda checked: self.on_char_format_changed('hex', checked))
        self.gb2312_check.toggled.connect(lambda checked: self.on_char_format_changed('gb2312', checked))

        char_layout.addWidget(self.utf8_check)
        char_layout.addWidget(self.asc_check)
        char_layout.addWidget(self.hex_check)
        char_layout.addWidget(self.gb2312_check)
        char_layout.addStretch()
        char_group.setLayout(char_layout)

        # 换行符设置
        line_break_group = QGroupBox("发送ASC追加换行符")
        line_break_layout = QHBoxLayout()

        self.lb_rn_check = QRadioButton("\\r\\n")
        self.lb_n_check = QRadioButton("\\n")
        self.lb_none_check = QRadioButton("none")

        line_break = self.js_cfg.get('line_break', '\r\n')
        if line_break == '\r\n':
            self.lb_rn_check.setChecked(True)
        elif line_break == '\n':
            self.lb_n_check.setChecked(True)
        else:
            self.lb_none_check.setChecked(True)

        line_break_layout.addWidget(self.lb_rn_check)
        line_break_layout.addWidget(self.lb_n_check)
        line_break_layout.addWidget(self.lb_none_check)
        line_break_layout.addStretch()
        line_break_group.setLayout(line_break_layout)

        # 按钮
        button_layout = QHBoxLayout()
        save_btn = QPushButton("保存")
        cancel_btn = QPushButton("取消")

        save_btn.clicked.connect(self.on_save)
        cancel_btn.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)

        # 添加到主布局
        main_layout.addWidget(interface_group)
        main_layout.addWidget(self.jk_group)
        main_layout.addWidget(self.ser_group)
        main_layout.addWidget(wave_group)
        main_layout.addWidget(char_group)
        main_layout.addWidget(line_break_group)
        main_layout.addLayout(button_layout)
        main_layout.addStretch()

        # 设置内容部件到滚动区域
        scroll_area.setWidget(content_widget)

        # 设置对话框主布局
        dialog_layout = QVBoxLayout(self)
        dialog_layout.addWidget(scroll_area)

        # 根据接口选择显示/隐藏对应的配置组
        self.on_interface_changed(self.jk_radio.isChecked())

    def refresh_ports(self):
        """刷新串口列表（仅更新下拉框选项，不重建控件）"""
        try:
            com_des_list, com_name_list = bds_ser.serial_find()
            if not com_des_list:
                com_des_list = ['未找到串口']
                com_name_list = []
            self.com_name_list = com_name_list

            # 保存当前选中的端口名（如果有）
            current_index = self.com_combo.currentIndex()
            current_port = ''
            if 0 <= current_index < len(self.com_name_list_old) if hasattr(self, 'com_name_list_old') else False:
                current_port = self.com_name_list_old[current_index]
            else:
                # 尝试从显示文本提取
                current_text = self.com_combo.currentText()
                match = re.search(r'\(([^)]+)\)', current_text)
                if match:
                    current_port = match.group(1)

            # 更新下拉框
            self.com_combo.clear()
            self.com_combo.addItems(com_des_list)

            # 恢复选中项
            if current_port and current_port in com_name_list:
                idx = com_name_list.index(current_port)
                if idx < self.com_combo.count():
                    self.com_combo.setCurrentIndex(idx)

            # 保存旧的名称列表供下次刷新使用
            self.com_name_list_old = com_name_list[:]

        except Exception as e:
            QMessageBox.warning(self, "刷新串口失败", f"无法刷新串口列表：{str(e)}")

    def on_interface_changed(self, checked):
        if self.jk_radio.isChecked():
            self.jk_group.show()
            self.ser_group.hide()
        else:
            self.jk_group.hide()
            self.ser_group.show()

    def on_char_format_changed(self, format_type, checked):
        if checked:
            if format_type == 'utf-8':
                self.asc_check.setChecked(False)
                self.hex_check.setChecked(False)
                self.gb2312_check.setChecked(False)
            elif format_type == 'asc':
                self.utf8_check.setChecked(False)
                self.hex_check.setChecked(False)
                self.gb2312_check.setChecked(False)
            elif format_type == 'hex':
                self.utf8_check.setChecked(False)
                self.asc_check.setChecked(False)
                self.gb2312_check.setChecked(False)
            elif format_type == 'gb2312':
                self.utf8_check.setChecked(False)
                self.asc_check.setChecked(False)
                self.hex_check.setChecked(False)

    def on_save(self):
        try:
            # 保存接口选择
            if self.jk_radio.isChecked():
                self.js_cfg['hw_sel'] = '1'
            else:
                self.js_cfg['hw_sel'] = '2'

            # 保存J-Link配置
            chip_name = self.chip_combo.currentText()
            if chip_name in self.js_cfg['jk_chip']:
                self.js_cfg['jk_chip'].remove(chip_name)
            self.js_cfg['jk_chip'].insert(0, chip_name)

            self.js_cfg['jk_speed'] = int(self.jk_speed.text())
            self.js_cfg['jk_con_reset'] = self.jk_reset.isChecked()

            if self.jk_sn_input.text():
                self.js_cfg['jk_sn'] = self.jk_sn_input.text()

            # RTT地址
            rtt_text = self.rtt_address.text()
            if rtt_text:
                rtt_addr = extract_and_convert_hex(rtt_text)
                if rtt_addr:
                    self.js_cfg['rtt_block_address'] = list(rtt_addr)
                else:
                    QMessageBox.warning(self, "警告", "RTT地址格式错误！请输入如 '0x20000000 0x4000' 的格式")
                    return

            # 保存串口配置
            current_index = self.com_combo.currentIndex()
            if 0 <= current_index < len(self.com_name_list):
                self.js_cfg['ser_port'] = self.com_name_list[current_index]
            else:
                # 尝试从显示文本提取端口名
                current_text = self.com_combo.currentText()
                match = re.search(r'\(([^)]+)\)', current_text)
                if match:
                    self.js_cfg['ser_port'] = match.group(1)
                else:
                    self.js_cfg['ser_port'] = ''

            baud = int(self.baud_combo.currentText())
            self.js_cfg['ser_baud'] = bds_ser.ser_buad_list_adjust(
                baud,
                self.js_cfg['ser_baud']
            )
            if len(self.js_cfg['ser_baud']) >= 10:
                self.js_cfg['ser_baud'].pop()

            # 保存波形配置
            try:
                y_min = int(self.y_min_input.text())
                y_max = int(self.y_max_input.text())
                self.js_cfg['y_range'] = [y_min, y_max]
                self.js_cfg['y_label_text'] = self.y_label_input.text()
                self.js_cfg['curves_name'] = self.curves_name_input.text()
            except ValueError:
                QMessageBox.warning(self, "警告", "Y轴范围必须是整数！")
                return

            # 保存字符编码
            if self.utf8_check.isChecked():
                self.js_cfg['char_format'] = 'utf-8'
            elif self.asc_check.isChecked():
                self.js_cfg['char_format'] = 'asc'
            elif self.hex_check.isChecked():
                self.js_cfg['char_format'] = 'hex'
            elif self.gb2312_check.isChecked():
                self.js_cfg['char_format'] = 'gb2312'

            # 保存换行符
            if self.lb_rn_check.isChecked():
                self.js_cfg['line_break'] = '\r\n'
            elif self.lb_n_check.isChecked():
                self.js_cfg['line_break'] = '\n'
            else:
                self.js_cfg['line_break'] = ''

            self.accept()

        except Exception as e:
            QMessageBox.warning(self, "错误", f"保存失败: {str(e)}")


def main():
    multiprocessing.freeze_support()

    # 初始化应用环境
    log_dir = config_manager.initialize_app_environment()

    # 配置日志
    log.basicConfig(filename='rtt.log', filemode='w', level=log.INFO,
                    format='%(asctime)s %(message)s', datefmt='%Y/%m/%d %I:%M:%S')

    # 读取配置
    js_cfg = config_manager.load_config()

    # 确保必要的配置项存在
    if 'font' not in js_cfg:
        js_cfg['font'] = ['Microsoft YaHei']
    if 'font_size' not in js_cfg:
        js_cfg['font_size'] = 10
    if 'user_input_data' not in js_cfg:
        js_cfg['user_input_data'] = []

    # 如果串口端口为空且当前选择的是串口模式，自动选择第一个可用串口
    if js_cfg.get('hw_sel', '1') != '1' and not js_cfg.get('ser_port'):
        try:
            _, com_names = bds_ser.serial_find()
            if com_names:
                js_cfg['ser_port'] = com_names[0]
                log.info(f"自动选择串口: {com_names[0]}")
        except Exception as e:
            log.warning(f"自动选择串口失败: {e}")

    # 创建应用
    app = QApplication(sys.argv)

    # 设置字体 - 修复类型错误
    font_family = js_cfg['font'][0]
    font_size = int(js_cfg['font_size'])  # 确保是整数
    font = QFont(font_family, font_size)
    app.setFont(font)

    # 创建主窗口
    window = MainWindow(js_cfg)

    # 初始化硬件对象
    if js_cfg.get('hw_sel', '1') == '1':
        window.hw_obj = bds_jk.BDS_Jlink(hw_error, hw_warn,
                                         char_format=js_cfg.get('char_format', 'utf-8'))
    else:
        window.hw_obj = bds_ser.SimpleSerial(hw_error, hw_warn,
                                           char_format=js_cfg.get('char_format', 'utf-8'))

    # 初始化波形
    wv.wave_init()
    if window.hw_obj:
        window.hw_obj.reg_dlog_M_callback(wv.wave_data)

    # 启动定时器处理日志
    window.startTimer(5)  # 每100ms处理一次日志

    # 显示窗口
    window.show()

    # 启动版本检测线程
    threading.Thread(target=version_detect_thread, args=(window, 'v2.7.0'), daemon=True).start()

    sys.exit(app.exec_())


# 辅助函数（需要保留原逻辑）
def hw_error(err):
    # 处理硬件错误
    pass


def hw_warn(err):
    # 处理硬件警告
    pass


def version_detect_thread(win, rtt_cur_version):
    # 版本检测线程
    pass


if __name__ == '__main__':
    main()