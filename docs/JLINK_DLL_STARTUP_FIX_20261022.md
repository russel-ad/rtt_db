# 其他电脑启动失败：J-Link DLL 分析与修复

用户截图中的调用链是 `rtt_ble_launcher.py → rtt_t2.main() → BDS_Jlink.__init__() → pylink.JLink()`，最终异常 `TypeError: Expected to be given a valid DLL.`。

已检查本机 PyLink 实现：`JLink.__init__()` 默认构造 `library.Library()`；如果 `lib.dll()` 为空就抛出上述异常。这表示没有取得有效原生 DLL，可能是未安装 J-Link 软件包、DLL 不在查找路径或不能加载。仅凭截图不能确认目标电脑具体是哪一种。

旧版默认配置 `hw_sel = "1"`，程序在显示正常应用界面前就创建 J-Link 对象。因此，即便只使用 BLE OTA，也会触发原生 DLL 依赖。Python 模块已打入 EXE 不代表目标电脑具有可用的 SEGGER 运行环境。

## 旧版临时解决

- 要使用 J-Link RTT：在目标电脑安装与工具位数一致的 SEGGER J-Link Software and Documentation Pack。本工程发布的 EXE 为 Windows x64，选择 Windows 64-bit Installer。
- 只使用 BLE OTA：可将 EXE 旁 `config.json` 的 `"hw_sel": "1"` 改为 `"hw_sel": "2"` 后重启工具。这会把原调试页切到串口模式，避开启动时的 J-Link 构造，不会写从机固件。
- 官方下载：<https://www.segger.com/downloads/jlink/>
- 官方 DLL 说明：<https://kb.segger.com/Update_the_J-Link_DLL>
- PyLink 源码：<https://github.com/square/pylink/blob/master/pylink/jlink.py>

## 当前源码修复

- `BDS_Jlink` 构造器只建立 Python 状态，不加载原生 DLL。
- 仅在明确请求 RTT 连接的 `hw_open()` 中创建 `pylink.JLink()`。
- 无 DLL 时，后台读取、关闭、状态检查及序列号查询均安全返回。
- DLL 加载失败返回可操作的安装提示；原调试页展示详细错误，应用继续运行。
- 修复后安装组件再点击连接可以重试，无需修改配置来启动 BLE OTA。
- 不修改从机固件，不改变 OTA 协议、分区或固件包。

## 验证与范围

- 77 项离线测试通过，新增构造/轮询/关闭不加载 DLL、缺失/位数错误异常提示、安装后重试与 RTT 生命周期、真实 `main()` 默认配置启动回归。
- EXE `--ui-self-test ... --without-jlink-dll` 将原生构造器替换为与截图相同的异常，检查启动构造不调用 DLL、主动 RTT 连接失败被处理、BLE 页仍能创建及加载 V001_004 ZIP；不连接 J-Link 或 BLE 产品。
- 缺失 DLL 为故障注入模拟，没有卸载本机 J-Link。目标电脑仍需实际运行新版验证。
- 首次排查只检查了 G: 目录，遗漏位于 `E:\onm\SC201_master_omn\build\dfu_20261022_c2\delivery` 的原主从机兼容 EXE。现已找到对应源码，并将主机 752 KiB 适配补入 G: 上位机，再与 DLL 延迟加载修复一起构建。最新交付及三个分区说明见 `HOST_SCALE_PORTABLE_20261022.md`。

## 构建

```powershell
.\build_ble_dfu.ps1 -HostScale -Suffix portable
```

最新输出：`dist/ble_dfu_host_scale_portable/rtt_t2_ble_dfu_host_scale_rssi_portable.exe`。
备份：`G:\rtt_db_jlink_startup_backup_20261022_181719`。
