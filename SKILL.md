---
name: codex-autoheal-bridge
description: Install, audit, repair, and manage Codex multi-model coexistence with model-aware routing and OAuth token self-healing across macOS and Windows. Routes official GPT models directly to OpenAI Native and third-party models (Gemini, Claude, DeepSeek) to loopback CLIProxyAPI (127.0.0.1:8317). Automatically self-heals expired Antigravity OAuth tokens across sleep/wake cycles.
---

# Codex Autoheal Bridge (自愈型多模型桥)

Manage subscription-backed or local proxy models in Codex Desktop and CLI with robust model-aware isolation and automatic OAuth token renewal across macOS and Windows.

Codex selects one `model_provider` for a task. Model catalog entries do not carry per-model Provider routing. Preserve the Provider identity that owns the majority of indexed task history (normally `openai`). For normal Desktop use, the v2 bridge design keeps `model_provider = "openai"`, keeps ChatGPT subscription auth intact, and points the built-in Provider's `openai_base_url` at `http://127.0.0.1:8318/v1` (`codex-model-router.mjs`).

### Architecture

```text
Codex Desktop
    │ model_provider = "openai"
    │ openai_base_url = http://127.0.0.1:8318/v1
    ▼
8318 Model-aware Router (codex-model-router.mjs)
    ├─ GPT / Codex / o1-o4  → OpenAI Native (chatgpt.com/backend-api/codex)
    └─ Gemini / Claude / DeepSeek → 8317 CLIProxyAPI
                                      ├─ Antigravity OAuth (with on-demand self-healing)
                                      └─ DeepSeek OpenAI compatibility
```

### Key v2 Improvements

1. **Model-Aware Isolation & Dual-Plane Architecture**:
   Official GPT models (`gpt-*`, `codex-*`, `o1/3/4`) are forwarded directly to OpenAI Native servers. They never transit CLIProxyAPI, preventing third-party gateway downtime from affecting official models. Control plane (`quota_failover.py` & `bridge.py`) manages rate-limit monitoring, catalog synthesis, and atomic config transitions without touching UI code or state databases.

2. **Simultaneous Multi-Model Coexistence in External Mode**:
   When entering external mode, the generated catalog simultaneously presents all third-party models (`Visible Catalog ⊆ 8318 可路由模型 ∩ 8317 实际存在模型`), including Gemini (3.8 Flash, 3.1 Pro, 3.7 Flash, 2.5 Pro), Claude Sonnet 4.6, DeepSeek (V4 Pro, Flash), GLM (5.3 Flash, 5.2), and MiniMax-M3 in the Codex model picker. Users can freely switch models in the UI dropdown without triggering routing conflicts.

3. **Explicit Header Contract & Dynamic Compaction**:
   External provider configuration routes through `http://127.0.0.1:8318/v1` with `x-codex-bridge-mode = "external"`. The 8318 router intercepts this header, dynamically enables `drop_foreign` compaction mode across model switches (dropping foreign carrier tokens and previous response IDs while preserving conversation history), fast-fails any direct GPT calls with HTTP 409, and strips internal headers before forwarding upstream to 8317.

4. **4-State Lifecycle Machine & Safe Quota Failover**:
   Transitions follow `OPENAI_ACTIVE` -> `EXTERNAL_RESTART_REQUIRED` -> `EXTERNAL_ACTIVE` -> `OPENAI_RESTORE_PENDING` -> `OPENAI_ACTIVE`. Fails over immediately on 100% window exhaustion or spend limits, and restores only when fresh backend confirmation confirms complete recovery. Active external model turns are never killed during restore.

5. **On-Demand OAuth Token Self-Healing**:
   Sleep, hibernation, or weekend shutdown will cause Google OAuth access tokens (1-hour TTL) to expire. 8318 Router intercepts expired credentials, silently uses the long-lived `refresh_token` to fetch a fresh token from Google within 300ms, and pauses to allow 8317 to reload—completely eliminating `503 auth_unavailable` errors.

6. **Full Windows Native Support & Zero-Console-Window Daemon**:
   Fully adapts to Windows 10/11 with zero-console-window hidden VBS runner (`run-router-hidden.vbs`), automatic user logon startup registration (`%APPDATA%\...\Startup`), native process discovery via `tasklist`, and Windows-friendly PowerShell/Batch helpers (`setup_windows.cmd`, `toggle_mode.cmd`). See [references/windows.md](references/windows.md).

7. **Legacy Transparent Proxy Deprecation**:
   The legacy `transparent_proxy.mjs` (which blindly forwarded all models to 8317) and its LaunchAgent `com.zhijian.codex-cli-model-bridge-transparent-proxy` are permanently superseded by `codex-model-router.mjs` and `com.zhijian.codex-cli-model-bridge-router`.

### Codex included-usage exhaustion / disabled Send

When ChatGPT/Codex included usage is exhausted and Desktop disables Send before
the local router receives any request, read
[references/quota-failover.md](references/quota-failover.md). This is separate
from Antigravity OAuth, Google validation, and third-party quota failures.

Start with `scripts/quota_failover.py quota-status --live`. It discovers the CLI
from `CODEX_CLI_PATH`, PATH, current/legacy ChatGPT.app and Codex.app layouts
(system and user Applications), then common Homebrew locations. Never assume
an app version implies one fixed CLI path. Use `--codex` for an explicit verified
executable; tests use `bridge.tomllib` so Python <3.11 follows the existing
`tomli` fallback.

External Mode is an opt-in workaround, not automatic permission to switch
Provider. Preview `external-mode` first; it writes no state, catalog, config,
or LaunchAgent. A default-Provider switch can hide existing OpenAI tasks in
the UI. Explain this and obtain explicit acceptance before `--apply`; also
obtain approval before restarting the desktop app or installing the watcher.
This is a temporary, user-approved exception to the normal dominant-history
Provider invariant. Preserve task rows, ChatGPT auth, and unrelated TOML.

For manual quick toggling between OpenAI mode and External mode in a single command:

```bash
# Every execution alternates between OpenAI and External mode:
python3 <skill-dir>/scripts/quota_failover.py toggle --apply --restart
```

For explicit two-way automation, use `auto-mode --model <verified-external-model>`
to preview, then `--apply` only after the user approves background monitoring,
the fixed fallback model, and the history-scope tradeoff. It monitors GPT in
normal mode, enters External Mode only on fresh `ordinaryUsageAllowed=false`,
restores after backend-confirmed recovery, and keeps monitoring for later
cycles. Use `stop-auto` to preview disabling and `stop-auto --apply` to stop
without changing the current mode. Do not migrate existing tasks or switch
their model automatically; the change is to the default routing configuration.

The one-way return watcher and the two-way monitor restore saved fields only after a fresh
backend response confirms `ordinaryUsageAllowed=true` and no blocking windows,
individual limit, or spend control. Missing fields, query errors, stale cached
data, or a passed reset time do not confirm recovery. They refuse restoration
if the user has changed those routing fields, and never restart an active app.
CLI query success does not prove that this Desktop version's Send button is
fixed: require a real external-model Codex probe and a user/UI check before
claiming the workaround is usable. Updating this Skill alone must not activate
External Mode, deploy a watcher, or restart services.

### Windows Native Workflows & Quick Start

Windows 10/11 is fully supported out of the box with zero console window popups.
For complete details and troubleshooting, see [references/windows.md](references/windows.md).

#### 1. Instant Automated Setup on Windows
- **Option A (Double-click)**: Run `scripts\setup_windows.cmd` directly from File Explorer.
- **Option B (PowerShell)**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
  ```
- **Option C (Standard CLI)**:
  ```cmd
  python scripts\bridge.py configure-desktop --platform windows --apply
  python scripts\bridge.py sync --apply
  ```
The setup generates a hidden VBS runner (`run-router-hidden.vbs`) and registers `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs`, running the 8318 gateway completely invisibly at user logon.

#### 2. Fast Dual-Mode Toggle on Windows (OpenAI ↔ External)
- **Option A (Double-click)**: Double-click `scripts\toggle_mode.cmd`.
- **Option B (Command line)**:
  ```cmd
  python scripts\quota_failover.py toggle --apply --restart
  ```

### Antigravity multi-account quota failover

The bridge supports multiple Google AI Pro accounts through one CLIProxyAPI
instance. Keep all Antigravity OAuth files in the configured `auth-dir`; do not
start a second CLIProxyAPI process against the same directory. The helper below
only prints redacted account metadata and never copies or logs OAuth tokens:

```bash
python3 <skill-dir>/scripts/antigravity_pool.py audit
python3 <skill-dir>/scripts/antigravity_pool.py configure --apply
python3 <skill-dir>/scripts/antigravity_pool.py login
```

The recommended policy is `routing.strategy: "fill-first"` with explicit
credential priorities (`priority: 100` for the preferred account and `50` for
the backup), `disable-cooling: false`, and `save-cooldown-status: true`.
`request-retry: 0` prevents a second retry round while the first round still
tries every eligible credential; this limits 429 cascades. `session-affinity`
keeps an existing long conversation on its selected account until that account
is unavailable, then binds the conversation to the fallback account.

### Antigravity quota-proximity priority rebalance

Accounts in the Antigravity pool can be prioritized by quota reset timing and account tier:

```bash
python3 <skill-dir>/scripts/antigravity_pool.py rebalance
python3 <skill-dir>/scripts/antigravity_pool.py rebalance --apply
```

1. **Primary criterion — weekly reset**: Among accounts with remaining 7-day quota, the account with the least time until its weekly reset ranks first. The weekly reset time is the primary sort key; it always takes precedence over 5-hour urgency and tier.
2. **Secondary criterion — 5-hour reset**: When accounts have equal weekly reset times—or all compared weekly reset times are unknown—a usable account whose 5-hour window resets within the imminent window (default: <= 2 hours) ranks ahead, so expiring short-window capacity is used. Tier (`standard-tier` / `g1-pro-tier`) and existing priority are later tie-breakers only.
3. **Exhausted and blocked handling**: An account with 0% 5-hour remaining is marked `FIVE_HOUR_EXHAUSTED` and assigned a lower priority than every usable account, even if its weekly quota remains; new `fill-first` selections therefore move to the next usable account. An account with 0% weekly remaining is `WEEKLY_EXHAUSTED`. Both remain behind usable accounts and carry their reset time when available. 429 cooldown follows these unavailable quota windows by earliest recovery; disabled or `VALIDATION_REQUIRED` accounts are `BLOCKED` with priority 0.
4. **Applied priority ranges**: usable accounts receive descending priorities from 900; 5-hour exhausted and 429-cooling accounts from 200; weekly-exhausted accounts receive 10; blocked accounts receive 0. The generated `pool-status.json` names a primary only when a usable account exists.
5. **Automated Scheduling**: The 8318 Router runs rebalancing on startup, periodically every 15 minutes, and reactively debounced upon receiving an upstream 429 quota exhaustion signal. The plugin's “重排” button invokes the same `rebalance --apply` operation manually.

Quota inputs come from the plugin's local `quota-snapshot.json` (`gemini5hRemaining` / `gemini7dRemaining` and absolute reset timestamps). Older snapshots containing only relative countdowns remain supported by aging the countdown from `updatedAt`. A missing weekly reset time sorts after accounts with known weekly reset times; when weekly times are equally unknown, use the 5-hour tie-breaker. Without a 5-hour remaining value, do not infer exhaustion.

With CLIProxyAPI `session-affinity` enabled, priority changes select the next eligible account for new or unbound requests. An already-bound conversation remains sticky until CLIProxyAPI observes that credential as cooling/unavailable; rebalancing does not forcibly clear live session affinity.

When Google returns `403 VALIDATION_REQUIRED`, follow
[references/antigravity-validation.md](references/antigravity-validation.md).
Do not run the generic `login` command blindly: first enumerate the exact
affected accounts, obtain authorization for that account list, then launch
OAuth one account at a time and verify the saved account before proceeding.
Never print or paste the stored `validation_url`; it contains opaque
account-specific flow parameters.

When the 8318 router returns `antigravity_account_action_required`, it has
confirmed that every enabled Antigravity credential for the requested Gemini
model is in a current validation or quota cooldown. Treat it as a terminal
account action, not a transient 503: do not retry or launch another OAuth flow
automatically. Explain the blocked-account categories using the redacted
`validation-plan` and offer to wait/verify or explicitly start a new task on a
different model with a plain-text summary. Never silently switch the model,
reuse provider-scoped Responses state, or expose account emails or Google
validation URLs in the router response. A successful Codex-level probe is still
required before declaring the Gemini route recovered.

After adding an account, run `audit`, verify two distinct enabled Antigravity
records, restart only the managed 8317 service, and probe the affected model.
The expected failover is: preferred account receives a confirmed quota/cooldown
signal, the same request is retried once on the backup, and a new session uses
the preferred account again after its reset. Ambiguous short 429s must not be
treated as permanent quota exhaustion; investigate the upstream reset signal
before adding a supervisor quarantine.

### Antigravity VALIDATION_REQUIRED (account verification) self-healing

When Codex receives `503 auth_unavailable` whose last upstream error contains `403 VALIDATION_REQUIRED` / "Verify your account to continue", Google has flagged account-level verification. **OAuth re-login (`login-validation`) does NOT clear this wall** (verified 2026-09-26). The only verified fix is completing Google's own challenge at the per-account `validation_url` embedded in the 403 body, then clearing the `.cds` cooldown files AND restarting CLIProxyAPI (cooldown is in-memory; deleting files alone is not enough).

Run the automated flow — preview first, then apply:

```bash
python3 <skill-dir>/scripts/antigravity_pool.py validation-fix --model gemini-3.8-flash-high          # 预览
python3 <skill-dir>/scripts/antigravity_pool.py validation-fix --model gemini-3.8-flash-high --apply --restart
```

The helper opens each account's verification page serially (user completes the Google challenge), backs up and deletes the cds files, kickstarts the CLIProxyAPI launchd service, and probes `/v1/responses` to classify the result (recovered / still blocked / quota exhausted). Full runbook: [references/antigravity-validation.md](references/antigravity-validation.md). If the probe reports 429 `RESOURCE_EXHAUSTED`, verification succeeded — the remaining issue is quota and must wait for reset.

### Antigravity prompt-fingerprint compatibility

The Antigravity Gemini and Claude routes can return `429 RESOURCE_EXHAUSTED` when the request contains the exact Codex identity sentence `You are Codex, an agent based on GPT-5.`. The 8318 router rewrites that exact string to `You are a helpful AI coding assistant.` only when `route.provider` is `gemini` or `claude`; OpenAI, GLM, MiniMax, and other non-Antigravity routes are byte-for-byte unaffected.

Because the router cannot inspect prompt data inside a WebSocket tunnel, Gemini and Claude WebSocket upgrades are rejected with `426 Upgrade Required` so Codex falls back to the HTTP Responses path where the rewrite is applied. Keep the managed Antigravity catalog entries on `prefer_websockets = false` as the compatibility baseline. The router logs `antigravity_prompt_rewrite` without logging prompt contents.

This rewrite addresses the prompt-fingerprint 429 class only. An upstream `503 UNAVAILABLE` / `No capacity available for model ...` response is a separate Antigravity capacity condition and must be retried after the upstream cooldown; it is not fixed by changing local credentials or enabling Multi-Agent v2 compatibility.

### Cross-provider compaction guard

Codex can persist a `type=compaction` item in a long task. That item is provider-specific: OpenAI/GPT compaction capsules cannot be decoded by Antigravity Gemini/Claude, and Antigravity capsules cannot be decoded by the OpenAI route. The router inspects compaction items before forwarding JSON Responses requests.

The default `CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE=fail_closed` mode blocks an incompatible or missing capsule locally with `provider_switch_compaction_conflict`. The response includes `choice_required=true` and two stable choices: `handoff` (recommended) creates a new target-model task with a plain-text summary, while `cancel` keeps the source-model task unchanged. It does not send the known-invalid request upstream or retry it. Valid `cpa-ag-compact-v1:` capsules remain intact for Gemini/Claude routes.

When this conflict is shown during a model switch, the user-facing Skill must ask the user to choose one of those two actions. Do not silently select `drop_foreign`, silently retry, or claim that the model switch succeeded. If the user chooses `handoff`, generate the packet below and create/open the target-model task. If the user chooses `cancel`, tell them to continue in the original task with its source model. The experimental `drop_foreign` mode remains an explicit maintenance override only; it is not a default dialog choice because it can discard compressed context.

The native model picker remains the model-selection surface. After a picker-triggered conflict, use the Skill decision helper to make the choice explicit:

```bash
python3 <skill-dir>/scripts/bridge.py provider-switch \
  --thread-id <thread-id> \
  --target-model gemini-3.8-flash-high \
  --output /tmp/codex-provider-handoff.md
```

The helper asks for `handoff` or `cancel` in an interactive terminal; in a
non-interactive call it returns `choice_required` instead of guessing. On
`handoff`, it writes a `0600` plain-text packet and returns
`next_action=create_target_model_task`; the Codex task layer should create/open
the new target-model task and paste that packet. On `cancel`, it leaves the
source task untouched. This is a post-conflict handoff workflow; the HTTP
router itself cannot render a native picker dialog or create a Codex task.

For that handoff, generate a provider-neutral Markdown packet from the visible rollout messages:

```bash
python3 <skill-dir>/scripts/bridge.py handoff \
  --thread-id <thread-id> \
  --target-model gemini-3.8-flash-high \
  --output /tmp/codex-provider-handoff.md
```

Paste the generated packet into a new target-model task. The command deliberately excludes developer messages, encrypted reasoning, compaction capsules, and tool IDs; it transfers visible task meaning without pretending to translate private provider state.

For controlled recovery only, set `CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE=drop_foreign`. The router removes incompatible compaction items, clears `previous_response_id`, preserves ordinary messages, and logs the dropped format. This may lose compressed context or increase token usage, so it is an opt-in degraded mode rather than the default.

This guard is separate from prompt-fingerprint 429 handling, OAuth refresh, capacity 503s, and Multi-Agent v2 `agent_message` compatibility.

### Cross-provider Responses item/state guard

Compaction is not the only provider-boundary state. A long Responses task can
also contain `previous_response_id`, `item_reference`, or provider-generated
items such as `rs_*`, `resp_*`, `msg_*`, `fc_*`, and `fco_*`. With Codex's
`store=false` request mode, replaying one of those IDs after switching from
Gemini/Claude to GPT (or in the opposite direction) can produce `404 Item with
id ... not found`; this is a stale provider-state reference, not an OAuth quota
problem. The router deliberately does not solve it by globally forcing
`store=true`, because that changes persistence semantics and is unsafe for a
mixed-provider bridge.

The router records only the last provider observed for each bounded
`thread_id`/`session_id` (no prompt or credential data). When the next request
for that task carries provider-scoped response state for a different provider,
the default `CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE=fail_closed` returns a
local `409 provider_switch_state_conflict` before any upstream call. The
native model picker remains the selection surface; the response exposes the
same two stable choices as the compaction guard: `handoff` (recommended,
create a new target-model task and paste a plain-text summary) or `cancel`
(keep the source-model task). No invalid request is retried.

`CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE=drop_foreign` is an explicit degraded
maintenance override. It removes `previous_response_id`, `item_reference`,
reasoning items, and function-call items carrying provider-scoped IDs while
preserving ordinary user/assistant messages. This can lose tool/reasoning
context and must not be selected silently. The state map expires entries after
two hours and is capped at 4096 tasks; tune with
`CODEX_BRIDGE_PROVIDER_STATE_TTL_MS` and
`CODEX_BRIDGE_PROVIDER_STATE_MAX_ENTRIES` only when operating a controlled
local deployment.

When the user wants GLM-5.3 from a Coding Plan key, read [glm-coding-plan.md](references/glm-coding-plan.md). If Desktop already uses Codex Router on port 4202, add `zai-coding` there and keep the OpenAI Provider identity. Do not run `npx @z_ai/coding-helper`.

On Windows, start with the isolated profile. Read [windows.md](references/windows.md). Do not require Homebrew, LaunchAgents, or Codex Router.

## Resolve the Skill directory

Resolve this loaded Skill's directory as `<skill-dir>`. Resolve `<python>` as the first available of `python3`, `py -3`, and `python`. Use the deterministic entry point:

```bash
<python> <skill-dir>/scripts/bridge.py
```

Examples below use `python3`. Substitute `<python>` when that command is missing.

## Default workflow

### 0. 启动交互确认操作系统（Mandatory OS Confirmation Gate）

**在执行任何环境检测、审计、配置或后台服务部署之前，必须先明确向用户确认本地操作系统环境：**

- 主动向用户发起一次简明确认：
  > “在开始配置前，请确认您当前运行的本地系统环境：  
  > 1. **macOS**  
  > 2. **Windows**”
- **免二次确认例外**：如果用户在初始提示词中已明确声明了操作系统（如“我在 Windows 电脑上”、“我是 Mac 系统”），则视为已确认，无需重复询问。
- **未确认操作系统之前，严禁直接执行写入或系统配置命令。**
- 确认系统后，按对应平台的最佳方案执行：
  - **macOS**：执行 LaunchAgent 开机自启守护（调用 `/bin/launchctl` 管理，指定 `--platform darwin`）。
  - **Windows**：执行 Windows 最佳实践方案，生成无黑框静默运行脚本（`run-router-hidden.vbs`）并写入用户自启目录（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs`），彻底避免黑框闪烁与误关问题（指定 `--platform windows`）。

### 1. Audit before mutation

```bash
python3 <skill-dir>/scripts/bridge.py audit
```

The audit must redact secrets and verify:

- Codex CLI version, `~/.codex/config.toml`, file permissions, and TOML validity
- default `model_provider`, indexed task counts by Provider, SQLite integrity, and the dominant history Provider
- active bridge mode: Desktop-transparent or isolated-profile
- Desktop-transparent `openai_base_url`, ChatGPT auth continuity, loopback health, or the isolated profile's command-backed authentication
- loopback-only CLIProxyAPI reachability and live `/v1/models`
- the active catalog's validity, visible model IDs, and bridge ownership state
- stale catalog entries, missing live routes for managed models, listed native models, or the current default model, and models present in the proxy but absent from Codex

Codex officially supports only the Responses wire API for custom Providers. Do not register a Chat Completions-only route and call it Codex-compatible.

If the default Provider differs from the dominant indexed-history Provider,
treat history restoration as the first repair unless the quota-failover state
records a currently authorized temporary External Mode. Do not undo that
exception merely because an audit sees a minority Provider, and never edit
task rows to make the current Provider fit.

### 2. Restore the desktop default and history

Preview:

```bash
python3 <skill-dir>/scripts/bridge.py restore-default
```

The preview reports one finding ID, the current config SHA-256, the exact single-file diff, and the before-state thread inventory. Apply only after the repair is authorized and the SHA is still current:

```bash
python3 <skill-dir>/scripts/bridge.py restore-default \
  --expected-sha256 <approved-sha256> \
  --apply
```

The default target is the Provider with the largest indexed task count. The command refuses a minority Provider unless `--allow-minority-provider` is explicit, restores a native model, removes the custom root catalog override, preserves unrelated TOML, creates a `0600` backup, and proves the task inventory digest did not change.

### 3. Configure or repair the isolated CLIProxyAPI profile

This is the default path on Windows. Preview and apply:

```bash
python3 <skill-dir>/scripts/bridge.py configure
python3 <skill-dir>/scripts/bridge.py configure --apply
```

The command does not rewrite `~/.codex/config.toml`. It preserves unrelated profile sections, creates timestamped `0600` backups, installs an owner-only credential helper that reads the existing CLIProxyAPI client key without copying it, and configures `~/.codex/cli-proxy.config.toml` with:

- `model_provider = "cli_proxy"`
- `model_catalog_json = "~/.codex/model-catalog-cli-proxy.json"`
- `[model_providers.cli_proxy]` with a loopback URL and `wire_api = "responses"`
- `[model_providers.cli_proxy.auth]` using the local helper command

The helper is Python by default so Windows does not need Ruby. An existing `.rb` helper is left in place. Do not set or change the user's default model unless they explicitly ask. Do not overwrite built-in Provider IDs. After this profile exists, use `codex --profile cli-proxy`.

### 4. Synchronize model catalogs

For a missing **official** model in Desktop-transparent mode, use the active
catalog synchronizer. It parses the root TOML's `model_catalog_json`; it does
not assume the profile catalog is the Desktop catalog:

```bash
python3 <skill-dir>/scripts/auto_sync_official.py
python3 <skill-dir>/scripts/auto_sync_official.py --apply
python3 <skill-dir>/scripts/auto_sync_official.py --apply --force  # manual immediate check
```

The account-specific `https://api.openai.com/v1/models` list (only
`visibility = "list"`) is the first discovery signal; Codex's
`models_cache.json` is a second source of full runtime metadata. That cache
can lag a release, while Codex app-server `model/list` can be bundled/cached.
Neither a public model page nor a listed slug proves this ChatGPT account can
run it. If the account endpoint returns 403 or is unavailable, do not erase
entries or claim a negative entitlement result. A known exact slug can be
bootstrapped explicitly with `--candidate <slug>` only when this Skill has
reviewed metadata for it; `gpt-6.1-sol` uses the official low/medium/high/
xhigh/max efforts and defaults to medium, not inherited `ultra`.

Every new official entry must pass an ephemeral read-only `codex exec` turn
against a private temporary catalog **before** the active catalog is atomically
updated. Failed probes leave the picker unchanged. Unknown account-listed
models without trustworthy runtime metadata are reported as
`pending_metadata` rather than cloned speculatively. The script preserves
unmanaged/third-party entries and unrelated config, detects a concurrent
active-catalog change, and creates a `0600` backup. Its output never contains
OAuth credentials or upstream error bodies.

The deployed 8318 router calls this synchronizer on startup, when the native
cache or account file changes, and every six hours without blocking inference.
The synchronizer keeps only account/cache fingerprints and retry metadata in
an owner-only state file; it never persists tokens. Unchanged sources within
the retry window cause no network call or Codex probe. Consecutive 403/model
failures back off to 6, 12, then 24 hours; a changed cache/account bypasses
the window. `--force --apply` is for an explicit manual check. Source edits
alone do not update a running router process; install a tested router version
at a safe time, and do not restart it during a live third-party turn. A catalog
write may also require a new Codex task or Desktop reload to refresh the
picker. Report the Codex probe and UI observation separately.

The following `bridge.py sync` command is for the isolated CLIProxyAPI
**profile** catalog, not the active Desktop-transparent catalog. It verifies
8317's live routes and is not a substitute for official-model discovery:

Preview bundled models:

```bash
python3 <skill-dir>/scripts/bridge.py sync
```

Apply after live route verification:

```bash
python3 <skill-dir>/scripts/bridge.py sync --apply
```

The sync command starts with Codex's native model cache only to inherit required runtime metadata, overlays verified model manifests from `<skill-dir>/models/`, preserves unmanaged/manual profile entries, refuses to overwrite an unowned collision unless `--adopt` is explicit, backs up the target, writes atomically, and records managed IDs under `~/.config/codex-cli-model-bridge/state.json`.

The picker policy lives at `<skill-dir>/policies/catalog.json`. IDs in `hidden_native_model_ids` remain in the catalog with Codex's native `visibility = "hide"` semantics, so existing tasks and routes keep working while those entries disappear from the model picker. Always change this canonical policy instead of hand-editing the generated catalog; every later sync reapplies it after a Codex update refreshes `models_cache.json`.

IDs in `protected_native_model_ids` must also remain under their exact native slugs. Codex App `create_thread` validates those IDs independently of cosmetic catalog aliases, so a managed manifest must never `supersede` them. Represent Fast through the service tier; do not replace `gpt-5.6-sol` with a `*-standard` picker alias. If WorkBuddy needs extra Fast/standard aliases, CLIProxyAPI `oauth-model-alias` must set `fork: true` so the native slug stays in live `/v1/models`. Audit fails when a listed catalog model or the current default model is missing from that live list.

Native entries copied into the bridge catalog are metadata only. In isolated-profile mode they route through `cli_proxy`; in Desktop-transparent mode they route through the built-in `openai` Provider identity and its loopback `openai_base_url`. The catalog itself never chooses the Provider.

Use `--models <comma-separated-ids>` to select a subset. Use `--catalog-policy <path>` only for an explicit alternate policy or an isolated test. Use `--prune-managed` only when the user explicitly asked to remove stale bridge-managed models. Never prune native or manual entries; hide native picker entries through the policy.

When onboarding a new model, read [model-manifests.md](references/model-manifests.md). A manifest is metadata, not proof. Its route must appear in live `/v1/models`, and a real `codex exec` probe must pass before success is reported.

### 5. Enable transparent Desktop coexistence

支持 macOS 与 Windows 两种操作系统的原生无感后台自愈常驻：

- **macOS**：自动生成并加载 `~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-router.plist`
- **Windows**：自动在 `%USERPROFILE%\.config\codex-cli-model-bridge\` 生成 `run-router.cmd` 和 `run-router-hidden.vbs`，并静默注册进用户的自启目录（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs`），彻底解决黑框控制台闪烁与误关问题。

First preview the exact root config diff and history guard (带确认后的平台参数):

```bash
python3 <skill-dir>/scripts/bridge.py configure-desktop --platform <darwin|windows>
```

After the finding-level diff is authorized, apply with the reported SHA-256:

```bash
python3 <skill-dir>/scripts/bridge.py configure-desktop \
  --platform <darwin|windows> \
  --expected-sha256 <approved-sha256> \
  --apply
```

The command refuses to proceed unless `openai` owns the majority of indexed history, `auth.json` still contains healthy ChatGPT tokens, both endpoints are loopback-only, and the selected default model exists in the catalog. It installs:

- `~/.config/codex-cli-model-bridge/codex-model-router.mjs`, owner-executable
- on macOS, `~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-router.plist`
- on Windows, `run-router.cmd` and `run-router-hidden.vbs` with startup entry in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs`
- unloads legacy `com.zhijian.codex-cli-model-bridge-transparent-proxy` if present
- a listener on `127.0.0.1:8318` that routes GPT to OpenAI Native and third-party models to CLIProxyAPI on `127.0.0.1:8317` with built-in OAuth token self-healing

It then keeps `model_provider = "openai"`, sets `openai_base_url = "http://127.0.0.1:8318/v1"`, activates the verified catalog, preserves ChatGPT login and unrelated TOML, creates a `0600` backup, and proves the task inventory digest did not change. Do not run a second CLIProxyAPI instance against the same OAuth directory; concurrent token refresh can invalidate credentials.

### 6. Handle Fast mode correctly

Codex Fast mode is a service tier on a model, not normally a second model entry. For a model whose catalog advertises the Fast tier, use:

```toml
service_tier = "fast"
```

or launch a one-off run with:

```bash
codex -c 'service_tier="fast"'
```

Codex maps `fast` to the priority request value. Do not create `*-fast` as a cosmetic catalog alias. A separate route is acceptable only when the upstream truly requires it and a live Responses probe verifies the distinct routing semantics.

### 7. Probe through Codex itself

After catalog sync, probe affected models:

```bash
python3 <skill-dir>/scripts/bridge.py probe --models claude-sonnet-4-6,deepseek-v4-pro
```

The probe runs `codex exec --profile cli-proxy` in ephemeral, read-only mode for each model and verifies a successful final response. Use `--fast` only for a model that advertises Fast. Keep prompts non-sensitive and do not persist sessions.

For the normal Desktop-transparent path, probe without switching Provider identity:

```bash
python3 <skill-dir>/scripts/bridge.py probe \
  --desktop \
  --models claude-sonnet-4-6,deepseek-v4-pro,deepseek-flash,gpt-5.6-sol
```

With `--desktop`, the probe reads the active root `model_catalog_json` from
`~/.codex/config.toml`; use `--catalog` only as an explicit override.

Direct HTTP probes can diagnose the proxy, but they do not prove that Codex consumed the Provider and model catalog. Completion requires the Codex-level probe.

When a model can chat but Codex reports an empty or incompatible Shell payload, require an actual read-only command event:

```bash
python3 <skill-dir>/scripts/bridge.py probe \
  --desktop \
  --shell \
  --models claude-sonnet-4-6
```

This passes only when Codex records a successful `pwd` command execution; a model that merely prints or simulates a path does not pass. If the failing custom model inherited `tool_mode = "code_mode_only"` from an OpenAI template, set `"tool_mode": null` in that model manifest and resync. Do not remove code mode from native OpenAI models globally.

### 7.1 Repair Codex Multi-Agent input for third-party models

Codex Multi-Agent v2 uses a private Responses input item named `agent_message`. Native OpenAI/Codex routes accept it, while xAI and other third-party Responses endpoints may reject it with HTTP 422 and `ModelInput`. CLIProxyAPI 7.2.125+ contains the compatibility transform; do not duplicate this protocol rewrite in the transparent header proxy.

Preview and enable it in the canonical CLIProxyAPI config:

```bash
python3 <skill-dir>/scripts/bridge.py configure-multi-agent
python3 <skill-dir>/scripts/bridge.py configure-multi-agent \
  --expected-sha256 <approved-sha256> \
  --apply
```

This changes only `codex.optimize-multi-agent-v2` to `true`, creates a `0600` backup, restarts CLIProxyAPI when a macOS Homebrew service exists, and waits for the transparent route to recover. On Windows, tell the user to restart CLIProxyAPI locally if the live `/v1/models` check does not recover. The transform is gated to official Codex user agents. For xAI, it converts `agent_message` into a standard user `message`, normalizes its encrypted content wrapper, and leaves normal OpenAI history/provider identity untouched.

Verify the exact failing shape, then run the normal Codex probe:

```bash
python3 <skill-dir>/scripts/bridge.py probe-multi-agent --models grok-4.6
python3 <skill-dir>/scripts/bridge.py probe --desktop --tool-sequence --models grok-4.6
```

For Grok agentic use, require CLIProxyAPI `7.2.130` or newer plus both probes above. A plain text completion or one successful `pwd` does not qualify the model for multi-tool or Subagent work. Older proxy versions may mishandle Responses tool identity, incremental tool state, or Codex multi-agent namespaces.

### 8. Verify consumption

Run `audit` again and repeat `sync`; the second sync must be idempotent. Normal Desktop tasks must remain on the dominant history Provider. In Desktop-transparent mode all selected models route through the loopback bridge while task identity remains `openai`; do not describe this as independent per-model Provider selection. Use `codex --profile cli-proxy` for Windows and for fallback diagnosis.

Report:

- Codex and CLIProxyAPI versions and local endpoint
- default Provider, task counts by Provider, and the verified unchanged task-inventory digest
- profile Provider and catalog paths, with secrets omitted
- models added, updated, removed, preserved, or conflicted
- live route and `codex exec` probe results
- Fast semantics when requested
- backup paths, reload action, and rollback command

## Repair workflow

1. Audit and distinguish history-scope mismatch, invalid TOML, proxy-down, helper/auth failure, missing route, invalid profile catalog, stale task, and Provider protocol mismatch. If the upstream body contains `403 VALIDATION_REQUIRED`, enumerate affected Antigravity accounts with the validation workflow before attempting OAuth.
2. Restore the dominant history Provider before model work; do not rewrite task rows.
3. Repair the smallest failing layer; do not reinstall a healthy proxy.
4. Re-authorize upstream Providers only when CLIProxyAPI authentication is actually absent or rejected. For Google `VALIDATION_REQUIRED`, use the targeted sequential login workflow; never log in every configured account or silently accept whichever Google account the browser defaults to.
5. Re-run profile catalog sync and the affected Codex-level probes.
6. Verify normal desktop history remains visible under the default Provider.

For a Subagent failure whose HTTP 422 body mentions `ModelInput`, inspect the failed task for an `agent_message` input item. On CLIProxyAPI 7.2.125+, enable `codex.optimize-multi-agent-v2`, then run `probe-multi-agent`; do not flatten all requests indiscriminately in the transparent header proxy.

Read [troubleshooting.md](references/troubleshooting.md) for failure classification and rollback.

## Safety boundaries

- Keep CLIProxyAPI on explicit loopback and remote management disabled.
- Do not restart CLIProxyAPI or the 8318 transparent proxy while the current Desktop session is using a third-party model such as Grok. A restart drops live routes for a few seconds and can abort this session with `unknown provider`. Wait until after the Sol/Grok repair is verified, or tell the user first.
- Preserve unrelated `config.toml` sections, MCP servers, hooks, skills, permissions, and project trust settings.
- Never print API keys, bearer headers, OAuth files, one-time codes, raw credential-helper output, or credential-bearing TOML blocks.
- Keep `config.toml`, catalog/state files, proxy config, helper, and backups owner-only when they can reveal private infrastructure. Unix mode `0600` is the target; on Windows keep the files in the current user profile and do not share them.
- Use command-backed auth or the owner-only transparent header rewriter; do not embed `experimental_bearer_token` or duplicate the proxy client key.
- Treat native `models_cache.json` as upstream input, not a file this Skill owns.
- Do not directly edit Codex SQLite state or the desktop app bundle to force a model into the picker.
- Never switch the default Provider without first reading the indexed Provider distribution. Refuse a switch that would hide the majority of history unless the user explicitly accepts that result; use the quota-failover runbook for a temporary accepted exception.
- Do not advertise per-model Provider routing. Desktop coexistence works only because the built-in `openai` Provider identity transparently routes every selected catalog model through the same loopback bridge.
- Respect Provider subscription terms, quotas, and account ownership.

## Completion gate

Completion requires:

- default Codex TOML anchored to the dominant indexed-history Provider
- unchanged, integrity-checked task inventory across the repair
- valid isolated fallback profile or a healthy Desktop-transparent loopback bridge with ChatGPT auth preserved
- valid active model catalog with no unapproved collision
- requested native picker exclusions retained with `visibility = "hide"`
- every newly managed route visible from CLIProxyAPI
- a successful ephemeral `codex exec` probe for every affected model through the active mode
- Fast represented and tested as a service tier when requested
- a second sync with no changes
- backups and rollback paths reported

For an explicitly authorized quota External Mode, use the focused completion
gate in `references/quota-failover.md` instead of immediately forcing the
dominant Provider back. For Skill-only maintenance, validate instructions and
changed scripts/tests without mutating the live deployment.

If Codex cannot complete a Responses request through a route, report it as unverified and do not advertise it as usable merely because `/v1/models` lists the name.
