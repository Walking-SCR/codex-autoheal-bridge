# 故障排查与回滚

## 故障分类

- **无效 TOML**：恢复最新的 `config.toml.backup-*`，然后只修复 Provider 块。
- **非环回 Provider**：重新绑定前先停止；修改已有的共享端点可能影响其他客户端。
- **凭据助手失败**：确认助手文件权限为仅所有者可读写，且 CLIProxyAPI 包含客户端密钥。不要打印助手输出。
- **路由缺失**：先修复或授权上游 Provider，再动 Codex 目录。
- **`unknown provider for model gpt-5.6-sol`**：CLIProxyAPI 返回 HTTP 400，原因是 WorkBuddy Fast 别名替换了原生 Codex ID。保留 `gpt-5.6-sol-standard` / `gpt-5.6-sol-fast` 的 `oauth-model-alias` 条目并设置 `fork: true`，让原始 `gpt-5.6-sol` 路由保持可见。不要把 Codex 指向 `gpt-5.6-sol-standard`；App Thread 和 `create_thread` 仍需要原生 slug。
- **修复 Grok 期间报 `unknown provider for model grok-4.6`**：这通常是会话进行中 CLIProxyAPI 被重启，而不是缺少 Grok 别名。不要在 Grok 会话里再次重启代理；等 `/v1/models` 列出 `grok-4.6` 后继续。
- **目录冲突（Catalog collision）**：保留手动条目。只有在用户确认本 skill 应接管该确切 slug 后才使用 `--adopt`。
- **目录已列出但 `codex exec` 失败**：该路由尚未证明 Responses 兼容；检查代理错误，不要宣告成功。
- **Shell 失败，报空参数、`missing field cmd` 或 `incompatible payload`**：检查 rollout 中是否有 `{}` 函数参数，以及模型目录是否继承了 `tool_mode = "code_mode_only"`。兼容代理常把自由格式的 `exec` 工具翻译成非 OpenAI 模型的空 schema 函数。只在受影响的第三方清单中设置 `"tool_mode": null`，重新同步，并要求跑 `probe --desktop --shell`；不要改动 Shell 权限或全局禁用 code mode。
- **Provider 操作后历史消失**：对比 `state_5.sqlite` 的 Provider 计数与根配置的 `model_provider`。用 `restore-default` 恢复占主导的 Provider；不要改写 thread 行。
- **原生 GPT 标签显示 Custom**：说明 `model_provider = "cli_proxy"` 全局生效了。先恢复 OpenAI Provider 身份，再启用 Desktop 透明模式。
- **第三方模型不在正常桌面选择器中**：根配置的 `model_catalog_json` 或 Desktop 透明模式缺失/过期。如果 `openai_base_url` 指向 `127.0.0.1:4202` 的 Codex Router，修 Router 的目录而不是跑 `configure-desktop`。否则运行 `configure-desktop`，验证 `127.0.0.1:8318`，并完全重启 Codex。`codex --profile cli-proxy` 只作为兜底。
- **需要保留 GPT 历史但 GLM-5.3 缺失**：按 [glm-coding-plan.md](glm-coding-plan.md) 处理。不要用 `ZAI` 覆盖 `model_provider`。Coding Plan 密钥在 `/api/paas/v4` 上会报 `1113`；要使用编码专用的 `/paas/v4` URL。
- **`bin/refresh-catalog` 把 Desktop 留在了原生目录**：该命令会禁用路由，且 `--help` 也会触发它。删除指向 `native-catalog-pre-router.json` 的非托管残留 `model_catalog_json` 后，用 `src/config-manager.mjs enable` 恢复。
- **透明路由返回 401**：Codex 被直接指向了带鉴权的 `8317` 端口，或头改写代理挂了。保持 `openai_base_url` 指向 `127.0.0.1:8318`；不要把 `auth.json` 从 ChatGPT 改成 API-key 模式。macOS 检查 LaunchAgent；Windows 检查 `node transparent_proxy.mjs` 是否还在运行。
- **ChatGPT 插件/账号功能消失**：根凭据被切换成了 API-key 鉴权。先恢复 ChatGPT 的 `auth.json` 再继续；绝不要对共享的 Codex 主目录用 `forced_login_method = "api"` 做探测。
- **第三方模型上 WebSocket 不断重试**：把托管目录条目的 `prefer_websockets` 设为 `false` 并重新同步。HTTP Responses 是兼容性基线。
- **Antigravity Gemini/Claude 只在完整 Codex 提示词下返回 `429 RESOURCE_EXHAUSTED`**：上游匹配到了固定的 Codex 身份提示词指纹。8318 路由仅对 Gemini 和 Claude 的 Antigravity 路由改写该句，并强制其 WebSocket 尝试回落到 HTTP；确认日志里有 `antigravity_prompt_rewrite` 事件，然后在厂商冷却结束后重试。
- **Antigravity 返回 `503 No capacity available for model ...`**：上游模型当时没有服务容量。等待 `Retry-After`/厂商冷却后重试；不要当成 local OAuth 或提示词改写故障。
- **8318 路由返回 `antigravity_account_action_required`，或原始 `503 auth_unavailable` 且 `last upstream error` 含 `403 VALIDATION_REQUIRED`**：Google 对账号发起了验证拦截。2026-09-26 实测确认：OAuth 重新登录（`login-validation`）**无法**解除该拦截；正确做法是打开 403 响应体中每个账号专属的 `validation_url`（`accounts.google.com/signin/continue?...`），用对应的 Google 账号在浏览器完成验证，然后删除该账号的 `.cds` 文件**并重启 CLIProxyAPI**（冷却状态在内存中——只删文件的话代理会继续返回缓存的 403）。运行自动化流程 `antigravity_pool.py validation-fix --model <model> --apply --restart`（详见 [antigravity-validation.md](antigravity-validation.md)）。不要先重试模型、不要在账号间互换验证链接、不要静默把任务发给其他厂商。如果修复后的探测返回 429，说明验证已通过、只剩配额问题——等重置即可。
- **长对话切换模型后报 `invalid compaction capsule: unrecognized compaction capsule format`**：这是 Provider 边界错误，不是 OAuth、配额或重试问题。GPT/OpenAI 任务可能携带 GPT 的压缩胶囊，而目标 Gemini/Claude Antigravity 路由只接受 `cpa-ag-compact-v1:`。路由器默认 `fail_closed`，在请求到达 8317 前返回 `provider_switch_compaction_conflict`，并标记 `handoff_required=true` 和 `choice_required=true`。用户在原生选择器选定目标模型后，运行 `bridge.py provider-switch --thread-id <id> --target-model <model> --output <path>`，让其选择 `handoff`（推荐；用生成的交接包新建任务）或 `cancel`（保持原任务不变）。不要静默重试或擅自选择降级路径。仅在受控测试中，可显式设置 `CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE=drop_foreign` 移除不兼容的压缩胶囊和 `previous_response_id`，但可能丢弃已压缩的上下文。不要删除 OAuth 文件或反复重试同一任务。
- **切换模型后报 `404 Item with id 'rs_...' not found` 或 `store=false` 相关错误**：任务携带了上一 Provider 的 Responses 作用域状态（`previous_response_id`、`item_reference`、`rs_`/`msg_`/`fc_`/`fco_` 项）。路由器记录任务的所属 Provider 并默认 `fail_closed`，在到达 8317 前返回 `provider_switch_state_conflict`；使用原生选择器的 `handoff`/`cancel` 选项。不要全局强制 `store=true`。受控维护测试可设 `CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE=drop_foreign`，它会丢弃推理/工具状态但保留普通消息。
- **子代理失败，HTTP 422 和 `ModelInput`**：Codex Multi-Agent v2 发送了第三方 Responses 端点无法反序列化的私有 `agent_message` 项。启用 CLIProxyAPI 官方的 `codex.optimize-multi-agent-v2` 兼容转换并用 `probe-multi-agent` 验证。8318 透明代理保持只做头改写。
- **Profile 列表过期**：目录有效后，新建一个基于 profile 的 CLI 任务。不要编辑 SQLite 或应用资源。
- **Fast 被拒**：移除 `service_tier = "fast"` 或使用默认 tier。不要通过改模型名来暗示 Fast。

## 回滚

备份与源文件放在同一目录：

- `~/.codex/config.toml.backup-<timestamp>`
- `~/.codex/cli-proxy.config.toml.backup-<timestamp>`
- `~/.codex/model-catalog-cli-proxy.json.backup-<timestamp>`

回滚时，把选定的备份复制覆盖源文件，保留 `0600` 权限，然后新建一个 Codex 任务。如果 Provider 和目录是一起修改的，两个文件要一起恢复。

`restore-default` 也会移除 `openai_base_url`。透明代理进程或 LaunchAgent 残留运行无害；只有在根配置恢复后、且用户明确要求清理时才停止它们。

Windows 上如果缺少 `python3`，改用 `py -3` 或 `python` 重试。如果 `cliproxyapi` 不在 PATH 上，传入 `--proxy-binary` 和 `--proxy-config`。隔离 profile 加 `codex --profile cli-proxy` 就足够了；不要卡在 LaunchAgents 或 Homebrew 上。

桥接状态文件只记录接管关系。删除它并不会恢复配置；请使用备份。
