# Troubleshooting and rollback

## Failure classes

- **Invalid TOML**: restore the newest `config.toml.backup-*`, then repair only the Provider block.
- **Non-loopback Provider**: stop before rebinding; changing an existing shared endpoint can break other clients.
- **Credential helper failure**: verify the helper is owner-only and CLIProxyAPI contains a client key. Do not print helper output.
- **Route absent**: repair or authorize the upstream Provider before touching the Codex catalog.
- **`unknown provider for model gpt-5.6-sol`**: CLIProxyAPI returned HTTP 400 because WorkBuddy Fast aliases replaced the native Codex ID. Keep `oauth-model-alias` entries for `gpt-5.6-sol-standard` / `gpt-5.6-sol-fast` with `fork: true` so the original `gpt-5.6-sol` route stays listed. Do not point Codex at `gpt-5.6-sol-standard`; App Thread and `create_thread` still need the native slug.
- **`unknown provider for model grok-4.6` during a live Grok repair**: this is usually a mid-session CLIProxyAPI restart, not a missing Grok alias. Do not restart the proxy again from a Grok session; wait for `/v1/models` to list `grok-4.6` and continue.
- **Catalog collision**: preserve the manual entry. Use `--adopt` only after the user confirms this Skill should own that exact slug.
- **Listed but `codex exec` fails**: the route is not proven Responses-compatible; inspect proxy errors and do not declare success.
- **Shell fails with empty arguments, `missing field cmd`, or `incompatible payload`**: inspect the rollout for `{}` function arguments and the model catalog for an inherited `tool_mode = "code_mode_only"`. Compatibility proxies commonly translate the freeform `exec` tool into an empty-schema function for non-OpenAI models. Set `"tool_mode": null` only in the affected third-party manifest, resync, and require `probe --desktop --shell`; do not alter Shell permissions or disable code mode globally.
- **History disappeared after Provider work**: compare `state_5.sqlite` Provider counts with the root `model_provider`. Restore the dominant Provider with `restore-default`; do not rewrite thread rows.
- **Native GPT labels show Custom**: `model_provider = "cli_proxy"` is active globally. Restore the OpenAI Provider identity before enabling Desktop-transparent mode.
- **Third-party model absent from the normal desktop picker**: root `model_catalog_json` or Desktop-transparent mode is missing/stale. If `openai_base_url` is Codex Router on `127.0.0.1:4202`, fix the Router catalog instead of `configure-desktop`. Otherwise run `configure-desktop`, verify `127.0.0.1:8318`, and fully restart Codex. Use `codex --profile cli-proxy` only as a fallback.
- **GLM-5.3 missing while GPT history must stay**: follow [glm-coding-plan.md](glm-coding-plan.md). Do not overwrite `model_provider` with `ZAI`. Coding Plan keys fail on `/api/paas/v4` with `1113`; use the coding `/paas/v4` URL.
- **`bin/refresh-catalog` left Desktop on native catalog**: that command disables routing, and `--help` still runs it. Restore with `src/config-manager.mjs enable` after removing an unmanaged leftover `model_catalog_json` pointing at `native-catalog-pre-router.json`.
- **Transparent route returns 401**: Codex was pointed directly at authenticated port `8317`, or the header-rewriting proxy is down. Keep `openai_base_url` on `127.0.0.1:8318`; do not change `auth.json` from ChatGPT to API-key mode. On macOS check the LaunchAgent; on Windows check that `node transparent_proxy.mjs` is still running.
- **ChatGPT plugins/account features disappear**: the root credential was switched to API-key auth. Restore the ChatGPT `auth.json` before continuing; never use `forced_login_method = "api"` as a probe against the shared Codex home.
- **WebSocket retries on a third-party model**: set the managed catalog entry's `prefer_websockets` to `false` and resync. HTTP Responses is the compatibility baseline.
- **Subagent fails with HTTP 422 and `ModelInput`**: Codex Multi-Agent v2 sent a private `agent_message` item that the third-party Responses endpoint does not deserialize. Enable CLIProxyAPI's official `codex.optimize-multi-agent-v2` compatibility transform and verify with `probe-multi-agent`. Keep the 8318 transparent proxy limited to header rewriting.
- **Profile list stale**: start a new profile-backed CLI task after the catalog is valid. Do not edit SQLite or app resources.
- **Fast rejected**: remove `service_tier = "fast"` or use the default tier. Do not rename the model to imply Fast.

## Rollback

Backups stay next to their source:

- `~/.codex/config.toml.backup-<timestamp>`
- `~/.codex/cli-proxy.config.toml.backup-<timestamp>`
- `~/.codex/model-catalog-cli-proxy.json.backup-<timestamp>`

To roll back, copy the chosen backup over its source, preserve mode `0600`, and start a new Codex task. Restore both files when a Provider and catalog change were applied together.

`restore-default` also removes `openai_base_url`. The transparent proxy process or LaunchAgent may remain running harmlessly; stop it only through an explicit cleanup request after the root config has been restored.

On Windows, if `python3` is missing, retry with `py -3` or `python`. If `cliproxyapi` is not on PATH, pass `--proxy-binary` and `--proxy-config`. Isolated profile plus `codex --profile cli-proxy` is enough; do not block on LaunchAgents or Homebrew.

The bridge state file tracks ownership only. Removing it does not restore configuration; use the backups.
