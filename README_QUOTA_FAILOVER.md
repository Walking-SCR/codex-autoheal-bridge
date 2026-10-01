# Codex Autoheal Bridge：GPT 配额耗尽自动切外部模型 / 恢复后自动切回

这是针对 `codex-autoheal-bridge` 的最小增量实现，不改现有 8317/8318 路由架构，也不伪造 Codex 用量、不 patch Desktop UI、不修改 thread/state DB。

维护中的操作说明见 [references/quota-failover.md](references/quota-failover.md)。默认采用手动进入、watcher 自动返回；若明确启用 `auto-mode`，则支持额度耗尽时自动进入外部模式、恢复后自动返回，并持续监控后续周期。启用需要用户指定已验证的备用模型并接受临时历史显示范围变化。

自动切换未触发或后台监控失效时，复制 [主 README 的手动切换命令](README.md#自动切换失败时手动切换命令macos)：先停监控，再预览/应用外部模式或原正常模式；默认不自动重启，另附明确会重启的替代命令。CLI、授权或额度查询错误不能靠手动切换绕过。

## 为什么要增加 External Mode

Codex Desktop 26.917 系列存在一个已公开报告的回归：当 ChatGPT/Codex workspace 用量达到 0 时，Desktop 会在请求发出前全局禁用 Send；即使自定义模型本身还有额度，请求也到不了 8318。

因此 8318 路由本身无法解除灰色发送按钮。可测试的临时绕行方式是：在 GPT 配额耗尽时，把 Desktop 的 active provider 暂时改为 `cli_proxy`，并明确 `requires_openai_auth=false`。外部 Provider 使用已有 8317 CLIProxyAPI，不依赖 ChatGPT included-usage。是否解除当前 Desktop 的 Send 限制仍需真机验证，不保证所有版本有效。

切换默认 Provider 可能让原 OpenAI 任务暂时不显示；不会删除任务，切回后恢复其历史范围。应先做只读审计和预览，再确认是否接受这一代价。

## 最终体验

```text
GPT 正常
  ↓
现有模式：
model_provider=openai
openai_base_url=http://127.0.0.1:8318/v1
  ↓
GPT 用量耗尽 / Send 置灰
  ↓
external-mode --apply --restart
  ↓
model_provider=cli_proxy
requires_openai_auth=false
只展示 Gemini / Claude / DeepSeek / GLM / MiniMax / Qwen 等外部模型
  ↓
继续用 Codex
  ↓
后台 watcher 读取 Codex 官方 account/rateLimits/read
记录 primary / secondary / resetsAt
  ↓
到预计 reset 时间以后每 45~120 秒验证
  ↓
只有 ordinaryUsageAllowed=true 且 Codex 窗口 usedPercent<100
  ↓
自动恢复进入 External Mode 前的 OpenAI 配置
  ↓
若 Codex 正在运行：不强杀任务，仅通知“完成当前任务后重启”
若 Codex 未运行：下次启动直接回 GPT 模式
```

## 安装

把文件放进现有 Skill：

```bash
cp scripts/quota_failover.py ~/.codex/skills/codex-autoheal-bridge/scripts/
chmod 700 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py
cp tests/test_quota_failover.py ~/.codex/skills/codex-autoheal-bridge/tests/
```

前提：原 Skill 已经执行过 profile 配置，存在：

```text
~/.codex/cli-proxy.config.toml
```

如没有，先执行：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/bridge.py configure --apply
```

## 用法

### 1. 看当前 GPT 配额（真实刷新）

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  quota-status --live
```

脚本会自动发现 CLI，覆盖 ChatGPT.app/Codex.app 的新旧打包目录和用户 Applications 目录，无需依赖 Terminal PATH。也可用 `CODEX_CLI_PATH` 或 `--codex /完整路径/codex` 指定。找不到 CLI 或查询失败时返回 `status=error` 和非零退出码；缓存数据会标记 `quota_source=cached`、`quota_stale=true`，不能当作实时额度。

### 2. GPT 用完以后，一键切外部模型

先预览（不写配置、状态、模型目录或服务文件）：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py external-mode
```

明确授权 Provider 切换、后台 watcher 和重启以后才执行：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  external-mode --apply --restart
```

也可以指定默认模型：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  external-mode --model gemini-3.8-flash-high --apply --restart
```

执行时会：

1. 通过 Codex 自己的 `app-server` 调用 `account/rateLimits/read`；
2. 记录 `ordinaryUsageAllowed`、5h/weekly 等窗口的 `usedPercent` 与 `resetsAt`；
3. 备份 `~/.codex/config.toml`；
4. 生成 `~/.codex/model-catalog-cli-proxy.external.json`，全量挂载 Gemini (3.8 Flash, 3.1 Pro, 3.7 Flash, 2.5 Pro)、Claude Sonnet 4.6、DeepSeek (V4 Pro, Flash)、GLM (5.3 Flash, 5.2)、MiniMax-M3 及自定义第三方模型；
5. 隐藏 GPT 原生模型，避免外部 Provider 误选 GPT；
6. 将 active provider 临时切成 `cli_proxy`，经由 8318 智能路由与 8317 网关；
7. `requires_openai_auth=false` 并注入 `x-codex-bridge-mode = "external"` 协议头；
8. macOS 自动安装一次性 quota watcher；
9. 可选重启 Codex，让发送按钮按外部 Provider 重新初始化。

### 3. 极速双向交替切换（Toggle 一键切换模式）

如果希望用**同一条命令**在两个模式之间来回极速切换，可以使用 `toggle`：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  toggle --apply --restart
```

- 当前处于 OpenAI 模式时执行：自动切换为外部模型模式（全量非 OpenAI 模型可见），并重启生效；
- 当前处于外部模型模式时执行：自动切回 OpenAI 官方模式（恢复 GPT 原生配置），并重启生效。

*(注：原命令增加 `--toggle` 参数也具有相同效果：`external-mode --apply --restart --toggle`)*

### 4. 手动切回 GPT

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  openai-mode --apply --restart
```

它恢复的是**进入 External Mode 前保存的四个根配置项**：

- `model_provider`
- `model`
- `openai_base_url`
- `model_catalog_json`

不会覆盖 MCP、Skills、personality 等无关配置。

### 4. 查看恢复时间

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py \
  quota-status
```

状态文件：

```text
~/.config/codex-cli-model-bridge/quota-state.json
```

权限为 `0600`，核心字段包括：

```json
{
  "mode": "external",
  "entered_external_at": "...",
  "blocking_window": [],
  "resets_at": 0,
  "resets_at_iso": "...",
  "last_checked_at": "...",
  "last_snapshot": {},
  "switch_back_status": "watching"
}
```

## 恢复判断为什么不能只看倒计时

Codex 最新 app-server 协议对 `ordinaryUsageAllowed` 有明确语义：它是后端针对当前账号确认的普通 included usage 是否允许；如果值为 null，客户端不应该仅根据百分比或 reset 时间推断恢复。

因此 watcher 的规则是：

```text
resetsAt 到点
≠
额度一定恢复
```

真正自动切回必须同时满足：

```text
ordinaryUsageAllowed == true
AND
primary.usedPercent < 100
AND
secondary.usedPercent < 100
AND
spendControlReached != true
```

这也解决了 5 小时窗口和 weekly 窗口同时耗尽的问题：`resetsAt` 记录当前所有真实阻塞窗口中最晚的恢复时间；到点后仍会再访问官方后端确认，不会只靠本地倒计时切回。

## 刷新频率

### 双向自动模式（需单独启用）

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py auto-mode --model <已验证的备用模型>
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py auto-mode --model <已验证的备用模型> --apply
```

第一条只预览，第二条获准后安装一个后台监控。GPT 模式默认每 120 秒查一次；只在后端明确 `ordinaryUsageAllowed=false` 时进入外部模式。额度恢复后切回，并继续监控下一周期。查询失败或数据未知时不切换，手动修改路由字段后会停止自动切换，避免覆盖设置。

两个方向都不会自动退出桌面应用；配置写好后会通知，运行中的应用需要在任务完成后手动重启才能确认生效。不会自动迁移已有任务的模型或上下文。

停止监控但保留当前模式：

```bash
python3 ~/.codex/skills/codex-autoheal-bridge/scripts/quota_failover.py stop-auto --apply
```

### 单向返回 watcher

External Mode 的 watcher 不做高频常驻请求：

- 距离预计恢复 >30 分钟：最多每 15 分钟醒一次；
- 10~30 分钟：约 5 分钟；
- 2~10 分钟：约 2 分钟；
- 2 分钟内：30~60 秒；
- 到点后尚未恢复：前 10 次约 45 秒，之后约 2 分钟；
- 后端无 reset 时间：15 分钟。

一旦确认恢复并恢复配置，watcher 自动退出。

## 安全边界

- 不修改 `state_5.sqlite`；
- 不修改 thread 的 `model_provider` 历史；
- 不伪造 usage；
- 不绕过服务端配额；
- 不 patch Codex Desktop 前端；
- 不自动强杀正在跑的外部模型任务；
- 每次改 `config.toml` 前都会创建 `0600` 备份；
- 外部 Provider 只允许 loopback base URL；
- GPT 恢复后只恢复进入 External Mode 前保存的配置，不覆盖期间用户新增的其它 TOML 配置。
- 若四个路由字段被用户手动修改，停止自动恢复，避免覆盖用户选择。
- `--no-watch` 可禁止安装后台 watcher；重启只针对 CLI 对应的桌面应用，无法识别时提示手动重启，不猜测应用。

## 测试

在仓库根目录：

```bash
python3 -m unittest tests.test_quota_failover
python3 -m unittest tests.test_bridge
node --test tests/test_router.mjs
```
