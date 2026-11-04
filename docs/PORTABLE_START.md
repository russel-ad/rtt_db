# RTT / SC201 免安装文件夹版

1. 将 ZIP 完整解压到本机有写入权限的目录，例如桌面或 `D:\RTT_SC201`。
2. 打开解压后的文件夹，双击其中的 `rtt_t2_sc201_workbench_portable_*.exe`。
3. 保留 EXE 同目录的 `_internal` 文件夹和其他随包文件。复制到另一台电脑时，应复制整个文件夹。

不要在压缩包预览窗口中直接运行 EXE，也不要只复制 EXE。此版本适用于 Windows 64 位，无需安装 Python。

## “Could not create temporary directory!”

这是原单文件 EXE 启动解压阶段的报错：启动器未能建立运行所需的临时目录，界面代码尚未执行。仅凭此提示不能确定具体原因，可能包括系统 TEMP/TMP 路径无效、权限不足、磁盘空间不足或安全软件拦截。

文件夹版将程序依赖随包展开，启动器无需再向系统临时目录解压 `_MEI...`。无需修改系统环境变量或使用管理员权限来完成这一步。

## 使用与资料

原有 RTT / 串口调试、蓝牙 DFU、出厂配对和 SC201 设备工作台都保留。工作台的日常操作、固件升级、工厂生产、连接测试、资料与调试入口在同一窗口内。

配置、日志和工作台资料仍需保存在可写目录中。携带原有资料时，在工作台中明确选择资料目录；本包不含用户账号缓存、生产台账或固件。完整功能说明见同目录 `SC201_WORKBENCH.md`。

## 再次打包

在工程目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./build_ble_dfu.ps1 -Workbench -OneDir -Suffix 20261102
```

输出包含可运行文件夹及其 ZIP。原单文件打包方式继续保留。

启动机制参考：[PyInstaller 官方说明](https://pyinstaller.org/en/v6.18.0/operating-mode.html#how-the-one-file-program-works)。
