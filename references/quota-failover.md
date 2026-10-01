# Codex included-usage failover

Use only for ChatGPT/Codex included-usage exhaustion where Desktop blocks Send
before a request reaches 8318. Google `VALIDATION_REQUIRED`, third-party 429s,
and upstream capacity errors follow their own workflows. External Mode cannot
bypass any provider's server-side quota or account verification.

## V2 Dual-Plane Architecture & Multi-Model Coexistence

Under the V2 architecture:

1. **Dual-Plane Routing**:
   - **Data Plane (8318)**: Reverse proxy with hop-by-hop header cleanup, SSE streaming, dynamic provider state compaction, Antigravity preflight auth refresh, and upstream dispatch to 8317 (`CLIProxyAPI`) or native OpenAI.
   - **Control Plane (`quota_failover.py` & `bridge.py`)**: Asynchronous watcher querying Codex official `account/rateLimits/read` via `app-server`, managing catalog generation, atomic config backups/writes, and state machine transitions.
2. **Explicit Header Contract (`x-codex-bridge-mode = "external"`)**:
   External provider configuration targets `http://127.0.0.1:8318/v1` and injects `x-codex-bridge-mode = "external"`. The 8318 router detects this header, dynamically enables `drop_foreign` compaction mode across model switches, fast-fails any direct GPT calls with HTTP 409, and strips internal headers before forwarding upstream to 8317.
3. **Simultaneous Multi-Model Visibility**:
   All non-OpenAI models (`Visible Catalog ⊆ (8318 可路由模型 ∩ 8317 实际存在模型)`) including Gemini (3.8 Flash, 3.1 Pro, 3.7 Flash, 2.5 Pro), Claude Sonnet 4.6, DeepSeek (V4 Pro, Flash), GLM (5.3 Flash, 5.2), and MiniMax-M3 are simultaneously visible and selectable in the model picker.
4. **Topology vs. Runtime Decoupling**:
   Topology fingerprinting monitors only `model_provider`, `openai_base_url`, and `model_catalog_json`. Switching models in the UI dropdown during external mode does not trigger `routing_conflict` and does not halt the monitor.
5. **4-State Lifecycle Machine**:
   - `OPENAI_ACTIVE`: Normal operation with OpenAI baseline.
   - `EXTERNAL_RESTART_REQUIRED`: Quota exhausted; config switched to external provider, pending desktop app restart.
   - `EXTERNAL_ACTIVE`: Running on external provider through 8318 router.
   - `OPENAI_RESTORE_PENDING`: Quota recovered; config restored to OpenAI baseline, pending desktop app restart. Users may continue active external model tasks without interruption.

## Diagnose before activation

1. Run the normal redacted bridge audit and record the indexed Provider
   distribution. Do not modify task rows or the app bundle.
2. Run:

   ```bash
   python3 <skill-dir>/scripts/quota_failover.py quota-status --live
   ```

   The helper uses a separate stdio app-server process with an in-memory
   `model_provider="openai"` override for account queries. It does not change
   the active config, sign the user out, or run model inference. It records
   the rate-limit snapshot in an owner-only state file. Official endpoint:
   [Codex App Server](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).
3. Check `status`, `quota_source`, `quota_stale`, and `last_error` before using
   the quota values. A failed `--live` query exits 2 and reports `status=error`;
   any retained quota is explicitly cached, not a successful live reading.
   `quota=null` or missing fields mean unavailable, not exhausted or recovered.

### CLI discovery / Python compatibility

The shared resolver tries `CODEX_CLI_PATH`, PATH, desktop bundle layouts under
`/Applications` and `~/Applications`, then common Homebrew locations. It
checks that each candidate is executable. Supported resource suffixes inside
both ChatGPT.app and Codex.app include:

- `Contents/Resources/codex-cli/bin/codex`
- `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`
- `Contents/Resources/codex`

These are candidates, not promises about every release. An explicit `--codex`
is authoritative: if invalid, fail with an actionable error rather than
silently running another CLI. Verify a selected binary with `--version` before
diagnosing an app-server protocol mismatch. LaunchAgents use the resolved
absolute executable and Python interpreter, not Terminal's PATH. If an app
update moves a pinned executable, re-resolve and reinstall the watcher only
with authorization.

Tests must parse TOML through `bridge.tomllib` (`tomllib` on Python 3.11+,
existing `tomli` fallback otherwise). Do not assume macOS system Python has
`tomllib`, and do not replace the system interpreter to work around this.

## Preview and authorized entry

```bash
python3 <skill-dir>/scripts/quota_failover.py external-mode
```

The preview fetches quota but writes no state/config/catalog/service files.
It selects the active OpenAI catalog by default; `--catalog` is an explicit
override. Explain before applying:

- The root Provider temporarily becomes `cli_proxy` with
  `requires_openai_auth=false`. Existing OpenAI tasks may disappear from the
  active UI history scope. They are not deleted; restoring the saved Provider
  restores that scope. Obtain explicit acceptance of this tradeoff.
- Use the existing loopback profile and its command-backed auth. Do not
  duplicate credentials, start another CLIProxyAPI instance, or switch to a
  random fallback model silently. Validate the chosen model's live route.
- A successful quota query is required before applying. Missing or rejected
  auth must be diagnosed first, not disguised as quota exhaustion.
- `--apply` on macOS installs the optional return watcher by default. Use
  `--no-watch` when background monitoring is not authorized. `--restart`
  requires separate user approval because quitting can interrupt active work.

After authorization, an example entry without restart or background work is:

```bash
python3 <skill-dir>/scripts/quota_failover.py external-mode --model <verified-external-model> --apply --no-watch
```

Only add watcher/restart options for the approved workflow. A restart uses the
desktop bundle enclosing the selected CLI, excluding nested CodexCLI.app.
When that identity is unknown (e.g. a standalone CLI), report manual restart
required rather than guessing which desktop app to quit.

## Recovery and rollback

State lives at `~/.config/codex-cli-model-bridge/quota-state.json` with mode
0600. It records the four original routing fields, the applied external
routing fields, last successful query, errors, blocking windows, and reset
times. All exhausted windows must reset; use the latest known reset as an
estimate, never as proof of recovery.

Automatic return requires a **fresh** response with explicit
`ordinaryUsageAllowed=true`, known percentages below 100 for each present
Codex window, and no individual-limit/spend-control block. Older CLIs may
omit the ordinary-usage field; remain waiting and report that limitation.
The watcher uses adaptive polling (up to 15 minutes far from reset, 30–120
seconds near/after reset). Query errors cannot trigger a return.

Return only restores saved root routing fields and backs up config first,
preserving MCP and unrelated edits. If those routing fields were manually
changed, stop for reconciliation rather than overwriting them. The watcher
does not quit the app; notify the user to restart when their task finishes.
Manual return is previewable and needs approval to apply:

```bash
python3 <skill-dir>/scripts/quota_failover.py openai-mode
python3 <skill-dir>/scripts/quota_failover.py openai-mode --apply
```

## Quick two-way manual toggle

To toggle between OpenAI and External mode with a single command on every run:

```bash
python3 <skill-dir>/scripts/quota_failover.py toggle --apply --restart
```

Running this command checks the active mode:
- If currently in OpenAI mode: switches to External mode (enabling all third-party models) and restarts Codex Desktop.
- If currently in External mode: restores OpenAI mode (restoring GPT native) and restarts Codex Desktop.

## Opt-in two-way automatic switching (macOS)

Use this only when the user requests automatic entry as well as automatic
return. Approval to update this Skill is not approval to deploy the monitor.
Before enabling, obtain the user's fixed external fallback model, verify its
route, and obtain acceptance that OpenAI history may be temporarily hidden.
Do not choose a different model based on catalog priority without approval.

Preview and authorized installation:

```bash
python3 <skill-dir>/scripts/quota_failover.py auto-mode --model <verified-external-model>
python3 <skill-dir>/scripts/quota_failover.py auto-mode --model <verified-external-model> --apply
```

Preview changes no files or jobs. Installation records the approved model and
baseline routing in `quota-state.json` and replaces the existing quota watcher
job with one `watch-auto` monitor; it does not immediately change mode or quit
the app. GPT checks default to every 120 seconds (`--poll-seconds` accepts
60–900); while external, use adaptive recovery polling. Query errors back off
to 15 minutes and cannot change mode.

The repeating state machine is:

- OpenAI configuration + fresh backend blocked usage (`analysis.get("blocked")`, such as 100% window exhaustion or spend limit reached): save the baseline and configure the external provider targeting 8318 with all external models visible.
- External configuration + fresh confirmed recovery (`analysis.get("recovered")`): restore the saved root routing fields once; continue monitoring, rather than exiting after return.
- Unknown/healthy quota without block: retain the current mode.
- User edits topology fields (`model_provider`, `openai_base_url`, `model_catalog_json`) or a transition fails: disable automatic switching, preserve user edits, notify once, and require reconciliation before re-enable. Note that switching models in the UI dropdown is NOT a routing conflict.

Neither transition automatically restarts an app. While Desktop is running,
the active session may continue using its previous in-memory configuration;
the notification asks the user to restart after finishing current work. Do
not describe this as seamless runtime switching or claim that Send is fixed
without UI verification. Existing task models, compaction capsules, and
provider-specific state are never silently transferred between providers.

`quota-status` includes a redacted `auto_monitor` summary. To stop:

```bash
python3 <skill-dir>/scripts/quota_failover.py stop-auto
python3 <skill-dir>/scripts/quota_failover.py stop-auto --apply
```

Stopping disables the monitor and unloads its own job if installed, retaining
the current mode and the recoverable plist. It does not switch GPT back on
or remove a separate return-only watcher. Manual restoration remains the
separately authorized `openai-mode` workflow above.

## Verification / reporting

Run `python3 -m unittest tests.test_quota_failover tests.test_bridge`, then
validate the Skill. For live activation, verify the unchanged task inventory,
backup, loopback route, fresh quota query, actual external-model Codex probe,
and Desktop Send behavior. Report pending restart or UI verification honestly.
The workaround's UI behavior is version-dependent and not established merely
by a valid `requires_openai_auth=false` config or an app-server response.
