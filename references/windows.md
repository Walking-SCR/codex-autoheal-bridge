# Windows notes

Windows supports both **Desktop-transparent mode** (native model picker dropdown integration with hidden background auto-heal) and **Isolated profile mode** (`--profile cli-proxy`).

## What to install

1. Codex CLI for Windows, already logged in if Desktop history should stay on ChatGPT.
2. Python 3.11 or newer. Prefer `py -3` when `python3` is missing.
3. Node.js for Desktop-transparent 8318 routing gateway.
4. CLIProxyAPI on loopback. Practical sources:
   - [CLIProxyAPI GitHub Releases](https://github.com/router-for-me/CLIProxyAPI/releases)
   - [EasyCLIProxyAPI](https://github.com/router-for-me/EasyCLIProxyAPI) if a tray app is easier

Keep the proxy on `127.0.0.1`. Do not enable remote management.

## Paths

| Role | Location |
| --- | --- |
| Codex home | `%USERPROFILE%\.codex` or `$env:CODEX_HOME` |
| Isolated profile | `%USERPROFILE%\.codex\cli-proxy.config.toml` |
| Bridge state | `%USERPROFILE%\.config\codex-cli-model-bridge` |
| Credential helper | `%USERPROFILE%\.config\codex-cli-proxy\read-client-key.py` |
| CLIProxyAPI config | `CLIPROXYAPI_CONFIG`, `%USERPROFILE%\.cli-proxy-api\config.yaml`, or the EasyCLIProxyAPI `cpa-core\config.yaml` |

Unix mode `0600` is not a Windows ACL. Keep these files inside the current user profile and do not share them.

## Default workflow

```text
<python> <skill-dir>/scripts/bridge.py audit
<python> <skill-dir>/scripts/bridge.py configure --apply
<python> <skill-dir>/scripts/bridge.py sync --apply
<python> <skill-dir>/scripts/bridge.py probe --models grok-4.6,deepseek-v4-pro
```

Then start Codex with `codex --profile cli-proxy`. Do not rewrite root `model_provider` to `cli_proxy` when ChatGPT history should stay visible in Desktop.

Pass `--proxy-config` and `--proxy-binary` when PATH discovery misses the Windows install.

## Desktop-transparent mode (Native Dropdown Coexistence)

`configure-desktop --platform windows --apply` automatically implements the optimal zero-console-window background daemon for Windows:

1. **Environment runner**: Generates `%USERPROFILE%\.config\codex-cli-model-bridge\run-router.cmd` with all required environment variables (`CODEX_BRIDGE_LISTEN_PORT=8318`, `CODEX_BRIDGE_CLIPROXY_BASE_URL`, etc.).
2. **Zero-black-window launcher**: Generates `%USERPROFILE%\.config\codex-cli-model-bridge\run-router-hidden.vbs` using `WScript.Shell` with `0, False` (SW_HIDE) to launch the gateway with absolutely no command prompt window.
3. **Logon auto-start**: Automatically places `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-model-router.vbs` into the user's Startup folder. On boot or user logon, the 8318 self-healing router starts invisibly in the background.

### One-line CLI setup for Windows:

```bash
python scripts\bridge.py configure-desktop --platform windows --apply
```

After configuration, restart Codex Desktop (`Alt + F4` or close and reopen). The native model dropdown will seamlessly include all custom models alongside official GPT models!

## GLM Coding Plan

Do not run `npx @z_ai/coding-helper`. Isolated CLIProxyAPI can host GLM only after a Responses probe passes. Codex Router remains optional and is a Node process, not a Windows service from this Skill.

## What this Skill does not require

- Ruby
- Homebrew
- macOS LaunchAgents
- a second CLIProxyAPI on the same OAuth directory
