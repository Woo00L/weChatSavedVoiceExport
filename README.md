# 微信收藏语音工具

一个在 Windows 本机扫描微信收藏语音、导出可用 WAV，并合并现有 WAV 文件的桌面工具。图形界面为中文，也提供命令行入口。项目版本为 1.0.0，源代码采用 [MIT 许可证](LICENSE)。

本项目不是微信或 WeFlow 的官方产品，也不包含微信、WeFlow、用户数据库或音频。收藏导出依赖用户自行安装并解锁 WeFlow；合并现有 WAV 不依赖 WeFlow。

## 功能与范围

- 扫描当前账号的本地收藏，按原始消息 ID 匹配本地语音；逐条显示已导出、缺失或失败原因。
- 导出时校验 WAV，生成 CSV 和 JSON 结果清单。重复运行时按账号、收藏身份及文件哈希验证后跳过，避免覆盖来源不明的文件。
- 合并文件夹中现有的未压缩 WAV，可调整顺序与段间静音；输入音频须具有相同的采样率、位深和声道数。
- 所有处理在本机进行。本工具通过 `127.0.0.1` 连接 WeFlow，不要求用户把 WeFlow 应用密码交给本工具。

已验证环境为 Windows 11 64 位、Python 3.12 和 WeFlow 4.5.1。其他 Windows 或 WeFlow 版本尚未验证。未下载、已清理或仅存在于云端的语音无法保证导出。单个合并 WAV 不能超过 4 GB。

## 使用

运行打包后的 `微信收藏语音工具.exe`，按界面选择本机 WeFlow 程序、账号目录和导出目录；首次连接需要在 WeFlow 窗口自行解锁。扫描前建议正常退出微信，避免收藏数据库在复制时变化。完整步骤、取消与续导说明见[使用说明](使用说明.md)。

只需合并现有文件时，在“合并现有语音”选择 WAV 文件夹、顺序及输出位置即可。

## 从源码运行

在 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

运行测试、打包 Windows EXE、制作仅含公开源码的压缩包：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\build.ps1
.\package_source.ps1
```

`build.ps1` 会先执行测试和窗口自检，再生成 `dist\微信收藏语音工具.exe`。窗口自检需要 Windows 图形会话。`package_source.ps1` 默认在项目的上一级目录生成 `wechat-favorites-tool-source.zip`，只收入脚本列明的源码、测试和公开说明。上传前仍应检查压缩包内容及个人修改过的源文件。

命令行可用 `main.py --help` 查看。`--scan`、`--export` 分别需要 `--account`，导出还需要 `--output`；`--merge` 需要 `--folder` 和 `--output`。

## 数据与隐私

扫描时使用临时数据库副本，不修改微信原数据库。导出结果、音频、个人设置和本机验证目录属于私人数据，不应提交到公开仓库；本仓库的忽略规则覆盖常见文件类型，但无法替代发布前检查。JSON 结果清单用于核验断点续导，请勿在正常使用中随意删除。

## 许可与依赖

仓库内自有源码与文档按 [MIT 许可证](LICENSE)提供。`cryptography`、`websocket-client`、Python 和 PyInstaller 等第三方组件有各自的许可证；MIT 不自动覆盖它们。WeFlow 是外部程序，不随本项目分发。如另行发布 EXE，应核对并随发布物提供实际打包组件所需的许可证和声明。贡献方式见[贡献说明](CONTRIBUTING.md)。
