# 上位机自动更新

适用版本：2.10.0 起的自动更新便携包。解压整个目录，启动顶层 `RTTTool.exe`。
首次需要分发这份基础包；旧版 EXE 没有更新客户端，不能自行获得这项功能。

启动后后台检查；运行中每五分钟重试检查，包括联网恢复后的重试。新版完整下载并通过
Ed25519 签名、大小和 SHA256 校验后，等待所有设备任务结束及界面连续空闲60秒再重启。
也可从“更新 → 重启并更新”主动切换，或正常关闭后下次启动切换。
批量 OTA、设备重启验版、蓝牙连接、RTT/串口连接、资源传输和模态对话框均阻止自动重启。
下载/网络失败继续使用当前版本。未运行软件时没有后台服务或定时任务。

从2.10.2起，底部显示上位机下载进度条、实时百分比、已下载/总大小及下载速度。
数字按实际接收的字节刷新；下载完成后继续显示校验和空闲重启倒计时。
网络连接短暂中断会有限重试；失败原因可在“更新 → 查看更新详情”查看，
并保存在更新目录的 `last-check-error.json`，不再只显示统一的“更新暂不可用”。
旧版2.10.0第一次下载2.10.2时仍使用旧界面，安装新版后才能显示这些新增界面。

从2.10.3起，传输中途断线或超时会保留 `.part` 文件并有限重试；下次检查可用 HTTP Range
从已下载位置继续。服务器忽略 Range 时安全地重新下载，不拼接重复数据；最终仍必须通过完整
大小和 SHA256 校验。持续断网会显示“已暂停”，保留数字进度，待恢复联网后自动续传。
旧版下载器无法完成首次升级时，可使用[独立下载修复工具](UPDATE_DOWNLOAD_REPAIR.md)，
选择原顶层 `RTTTool.exe`，准备更新后由原启动器完成安装，保留既有配置。

启动器独立于 Qt 程序，串行持有当前用户的会话锁；先等待 GUI 退出再切换完整版本目录。
新版首次启动有45秒健康检查，完成原生组件加载和GUI事件循环检查后才开放试用版界面。
启动失败自动返回上一版，并记录失败版本，避免重复安装同一坏版本。
此检查是启动验证，不替代蓝牙设备实测。启动器API不兼容的将来版本需要另发基础包。

## 数据位置

- 上位机配置和日志：`%LOCALAPPDATA%/RTTTool/data/`。
- 更新缓存、版本及回退记录：`%LOCALAPPDATA%/RTTTool/updates/`。
- 所选工作台资料目录继续沿用，不复制或清理设备台账、账号缓存和未完成事务。
- 目标电脑无原工作台目录时，默认资料位于上述数据目录的 `private/workbench`。
- 首次可将旧 `config.json` 放在顶层启动器旁，首次运行时导入到稳定数据目录；之后以
  稳定目录为准。原资料和SC201账户的Windows用户设置不随软件版本切换。

## 使用当前GitHub仓库发布

仓库：`https://github.com/russel-ad/rtt_db`，已通过匿名API确认公开。
固定清单：`https://github.com/russel-ad/rtt_db/releases/latest/download/latest.json`。
完整应用ZIP使用版本固定的 `/releases/download/v<版本>/RTTTool-<版本>-windows-x64.zip`。

仓库中的 `updates/stable.json` 保留同一份已签名正式清单，可通过
`https://raw.githubusercontent.com/russel-ad/rtt_db/main/updates/stable.json` 获取。
它是备用清单副本，发布时须与已公开的正式更新包保持相同版本、大小和摘要；不得手工修改签名内容。

正式版本身支持自动更新。GitHub标记为Pre-release的测试版通过单独测试地址分发，
不会成为正式版Latest。正式用户使用上述固定清单和 `RTTTool.exe`；
此前的 `RTTTool_UpdateTest.exe` 使用独立测试清单和独立资料目录。

发布在 Release 附件，不将大型EXE、使用者资料或发布私钥提交进Git。
公开Release支持匿名下载。私有仓库应使用单独公开发布仓库或其他HTTPS分发服务；
客户端不内置GitHub token。GitHub可达性取决于客户端网络，不保证所有网络均可访问；
需要时可将同样的签名清单和ZIP部署在其他HTTPS服务上。

v2.10.0 已发布到[公开正式 Release](https://github.com/russel-ad/rtt_db/releases/tag/v2.10.0)。
首次使用请下载该 Release 的 `RTTTool_AutoUpdate_2.10.0_r3.zip`，完整解压后运行顶层
`RTTTool.exe`。后续版本沿用同一签名密钥，在三个附件上传校验完成后设为 Latest，
客户端即可通过上述固定清单检查更新。基础包必须和清单中信任的发布公钥对应。

## 发布工具

Windows发布账号首次生成签名密钥（私钥用该用户的DPAPI保护，存放在仓库外）：

```powershell
python -m app_update.publish keygen --private-key "$env:LOCALAPPDATA/RTTToolPublisher/release.key" --settings app_update/release_config.json --feed-url https://github.com/russel-ad/rtt_db/releases/latest/download/latest.json
```

`release_config.json`只有公开下载地址和公钥，可以纳入版本管理。私钥需持续保留，
后续发布使用同一密钥；密钥文件仅该Windows账号可解密，不随便携包分发。

修改 `app_update/__init__.py` 的版本号，完成回归，再构建：

```powershell
python tools/build_auto_update.py --private-key "$env:LOCALAPPDATA/RTTToolPublisher/release.key"
```

生成基础便携ZIP，以及 `dist/RTTTool_Release_<版本>/` 中的 `latest.json` 和应用ZIP。
在GitHub创建相应版本的正式Release，上传这两个文件，上传完成后再将该Release设为Latest。
基础便携ZIP可同时作为首次安装附件。应用更新ZIP不包含启动器或私钥。
旧Release及版本包保留；客户端拒绝已见过版本的倒退和同版本替换包。
清单有效期90天，长期无新版本时应使用同一包摘要重新签发有效清单。

签名工具及完整构建命令均不执行上传，也不操作BLE设备或固件。
