# Windows 平台完整适配与使用指南 (Windows Native Guide)

本指南专门针对 **Windows 10 / 11** 环境下的 Codex 桌面客户端（Codex Desktop / ChatGPT.app）用户编写。通过原生 VBS 隐形启动与开机自启集成，Windows 用户可享有与 macOS 完全一致的 **0 黑框弹窗、开机静默常驻、原生模型下拉列表无缝共存与配额自动自愈** 体验。

---

## ⚡ 极速一键安装（双击即用）

为降低 Windows 命令行门槛，本项目专为 Windows 封装了一键配置批处理与 PowerShell 脚本：

### 方式一：双击批处理一键安装（推荐）
在文件资源管理器中打开本 Skill 目录，进入 `scripts\` 文件夹：
👉 **双击运行 `setup_windows.cmd`**

脚本会自动通过 PowerShell 完成：
1. 校验 Python 3.11+ 和 Node.js 运行环境；
2. 自动配置 Windows 8318 智能自愈路由网关；
3. 生成无窗口静默 VBS 启动脚本并写入用户开机启动目录（`Startup`）；
4. 自动同步多模型清单（Gemini 3.8/3.7/3.1/2.5、Claude Sonnet 4.6、DeepSeek V4 Pro、GLM 等）；
5. 验证本地 8318 回环服务健康度。

配置完成后，按 `Alt + F4` 彻底退出 Codex 客户端并重新打开，即可在原生下拉列表中使用所有模型！

### 方式二：PowerShell 终端一键安装
以普通用户权限打开 PowerShell（无需管理员权限）：
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

---

## 🔀 双模快速切换（官方 GPT ↔ 外部多模型）

当官方 5 小时 / 7 天额度用尽，或想要切换为完全由外部第三方模型承载时：

### 方式一：双击一键切换（零终端输入）
👉 **双击运行 `scripts\toggle_mode.cmd`**
每次双击执行，脚本都会在「官方原生 OpenAI 模式」与「外部多模型模式」之间自动来回交替切换，并自动平滑重启客户端生效。

### 方式二：命令行切换
```cmd
python scripts\quota_failover.py toggle --apply --restart
```

---

## 🛠️ Windows 环境与路径规划

| 角色 | 路径 / 位置 | 说明 |
| :--- | :--- | :--- |
| **Codex 根配置** | `%USERPROFILE%\.codex\config.toml` | 存放全局 provider 配置与模型目录指向 |
| **Codex 状态数据库** | `%USERPROFILE%\.codex\state_5.sqlite` | 官方历史会话数据库（只读保护，绝不修改） |
| **网关运行状态** | `%USERPROFILE%\.config\codex-cli-model-bridge\` | 存放 8318 路由运行时、状态 JSON 与缓存 |
| **开机自启目录** | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` | 存放隐形启动脚本 `codex-model-router.vbs` |
| **CLIProxyAPI 目录** | `%USERPROFILE%\.cli-proxy-api\` 或 `EasyCLIProxyAPI\cpa-core\` | 存放 Google OAuth 认证凭证与 `config.yaml` |

> ⚠️ **注意**：Windows 下请确保所有配置文件保存在当前用户个人主目录下，避免使用管理员权限在系统目录创建文件导致权限异常。

---

## 💻 命令行进阶工作流 (PowerShell / CMD)

如果你习惯在终端管理和排障，可直接使用以下标准命令：

```powershell
# 1. 环境健康审计与会话安全性校验
python scripts\bridge.py audit --platform windows

# 2. 配置并部署 Windows 桌面透明自愈网关
python scripts\bridge.py configure-desktop --platform windows --apply

# 3. 同步模型清单并挂载至原生下拉框
python scripts\bridge.py sync --apply

# 4. 连通性探针测试（测试 Claude 与 DeepSeek 连通性）
python scripts\bridge.py probe --desktop --models claude-sonnet-4-6,deepseek-v4-pro

# 5. 检查实时配额与路由状态
python scripts\quota_failover.py quota-status --live
```

---

## 🛡️ Windows 专属技术实现机制

1. **零黑框静默运行（Zero-Console-Window Daemon）**
   - Windows 下直接通过 Node 启动进程会常驻一个黑色 `cmd.exe` 窗口，极易被用户误关。
   - 本 Skill 在配置时会自动生成两层启动包装：
     - `run-router.cmd`：负责设置必要的环境变量（`CODEX_BRIDGE_LISTEN_PORT=8318` 等）并调用 Node 执行；
     - `run-router-hidden.vbs`：通过 Windows 原生 `WScript.Shell.Run ..., 0, False`（窗口样式 `0` 即 `SW_HIDE`）以 100% 隐藏窗体静默运行。
2. **开机自启动集成**
   - 自动生成快捷方式 VBS 放置在当前用户的开机启动文件夹中：
     `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs`
   - 每次开机或登录 Windows 时，8318 路由网关在后台隐形启动，随时响应客户端请求。
3. **原生任务管理与进程识别**
   - `quota_failover.py` 在 Windows 下自动通过 `tasklist` CSV 格式查询定位正在运行的 `Codex.exe` 或 `ChatGPT.exe`。
   - 重启时利用 `taskkill /F` 平滑释放本地 CDP 端口，并通过 `subprocess.Popen` 从安装路径安全拉起主程序，彻底杜绝孤儿进程或端口占用冲突。
4. **气泡通知集成**
   - 在配额耗尽切模式或配额恢复自动切回时，Windows 端调用内置 PowerShell Forms 模块弹出系统托盘气泡通知（BalloonTip），提示及时重启客户端。

---

## ❓ 常见问题排查 (Windows FAQ)

### Q1: 运行脚本提示 `python 不是内部或外部命令`？
- **原因**：安装 Python 时未勾选“Add Python to PATH”。
- **解决方式**：
  1. 重新运行 Python 安装包，选择 **Modify** 并勾选 **Add Python to environment variables**；
  2. 或者在终端尝试使用 `py scripts\bridge.py ...` 代替 `python`。

### Q2: PowerShell 提示 `无法加载文件 ... 因为在此系统上禁止运行脚本`？
- **原因**：Windows PowerShell 默认的执行策略（Execution Policy）为 Restricted。
- **解决方式**：
  直接双击 `scripts\setup_windows.cmd`（内部已自带 `-ExecutionPolicy Bypass` 参数绕过），或者在 PowerShell 中执行一次：
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```

### Q3: 如何确认 8318 路由是否已在后台成功运行？
在浏览器或终端访问回环健康检查接口：
```cmd
curl http://127.0.0.1:8318/__codex_bridge_health
```
如果返回 `{"ok":true,"mode":"openai",...}` 即表示隐形路由网关工作正常。

### Q4: 杀软或 Windows Defender 拦截本地回环？
8318 路由与 8317 代理均严格绑定在 `127.0.0.1` 本机回环网卡，不监听任何局域网或公网端口。如遇 Windows 防火墙弹窗询问，请允许“专用网络”访问。
