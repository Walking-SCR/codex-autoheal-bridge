# Windows notes

Isolated profile is the default Windows path. Homebrew, LaunchAgents, and Codex Router are optional.

## What to install

1. Codex CLI for Windows, already logged in if Desktop history should stay on ChatGPT.
2. Python 3.11 or newer. Prefer `py -3` when `python3` is missing.
3. Node.js only if the user wants Desktop-transparent mode.
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

## Optional Desktop-transparent mode

`configure-desktop` starts `node transparent_proxy.mjs` as a detached process instead of a LaunchAgent. The user can also keep a terminal open:

```text
node <skill-dir>/scripts/transparent_proxy.mjs
```

If that process exits, Codex Desktop on `127.0.0.1:8318` fails until it is started again.

## GLM Coding Plan

Do not run `npx @z_ai/coding-helper`. Isolated CLIProxyAPI can host GLM only after a Responses probe passes. Codex Router remains optional and is a Node process, not a Windows service from this Skill.

## What this Skill does not require

- Ruby
- Homebrew
- macOS LaunchAgents
- a second CLIProxyAPI on the same OAuth directory
