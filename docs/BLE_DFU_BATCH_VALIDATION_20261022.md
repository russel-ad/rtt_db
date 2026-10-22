# 多设备 BLE DFU 验证记录（2026-10-22）

- `python -m unittest discover -s tests_ble_dfu -q`：72 项离线测试通过，覆盖原有 51 项及新增 21 项。
- 新增验证包括：并发 1/2 的连接生命周期上限；每台设备独立协议状态；3 个模拟接收端各完成一次最终 Execute；连接/CRC 故障隔离；同地址去重；任务忙和输入校验；固件被替换时拒绝写入；丢失最终回执与提交后版本错误不能判成功。
- 停止检查：取消排队及提交前任务；保留提交后任务；重复停止不打断清理；准备期间可取消；关闭时等待子任务结束。
- UI 检查：筛选保留勾选、无须手动逐台连接、每台进度独立、传输 100% 不直接判成功、CSV 导出、原 RTT 页保留、资源任务互斥。
- `build_ble_dfu.ps1 -Batch` 成功生成独立批量版 EXE，原单设备 EXE 未覆盖。
- 新 EXE 在 PATH 只有 Windows 系统目录、无 PYTHONPATH/PYTHONHOME 的环境中退出码为 0，加载真实 V001_004 ZIP 成功；无勾选设备时禁止开始升级。
- EXE 加载 Bleak 2.1.1、BleakClientWinRT、BleakScannerWinRT 成功；本机适配器存在、BLE / 中央角色支持、蓝牙开启，检查状态为 ok。
- 原 `config.json`、`my_serial.py` SHA-256 与先前备份一致。当前用户原有修改保留；本次没有修改 `E:\onm\sc201` 固件工程。
- UI 预览中的设备、进度、速度全部为模拟数据；没有扫描、连接、擦写真实设备。尚未验证电脑适配器的实机并发上限、OTA 成功率、吞吐和 RTT 硬件功能。

机器报告及构建日志：`build/ble_dfu_checks/batch_*.json`、`batch_build_exe.log`。
源码备份：`G:\rtt_db_ble_batch_backup_20261022_154443`。
