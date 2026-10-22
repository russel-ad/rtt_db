# 集成版验证记录

- `python -m unittest discover -s tests_ble_dfu -q`：51 项测试通过。
- 原 RTT 调试页与 BLE DFU 页均能创建；原资源发送控件保留。
- 已核对传输任务互斥、取消、最终提交保护、关闭清理和升级后版本确认等 UI 状态。
- `rtt_t2_ble_dfu.exe` 在 PATH 不含 Python、PYTHONPATH/PYTHONHOME 已清除的子进程中启动成功。
- EXE 内加载 `V001_004` 原厂 ZIP 成功；未连接设备时升级按钮禁用。
- EXE 内 WinRT BLE 客户端与扫描器依赖加载成功；本机适配器存在、支持 BLE 中央角色、蓝牙开启。
- 已查看原生 Windows Qt 渲染截图，检查中文、布局和控件显示。
- 原 `config.json`、`my_serial.py` 与集成前备份的 SHA-256 一致，既有修改未覆盖。
- 未扫描、连接或擦写产品；不代表完成真实 DFU 或原 RTT/串口硬件功能验收。

详细机器检查结果、界面截图、EXE 校验值和构建记录位于 `build/ble_dfu_checks/` 及 `dist/ble_dfu/`。
