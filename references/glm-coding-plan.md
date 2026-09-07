# GLM Coding Plan on Codex

Use this path when the user has a **GLM Coding Plan** key and wants `glm-5.3` in the Codex picker **together with** existing GPT / Grok / DeepSeek models.

Do not follow the vendor helper that overwrites `~/.codex/config.toml` with `model_provider = "ZAI"`. That hides the dominant OpenAI history.

## Detect the Desktop path first

```bash
python3 <skill-dir>/scripts/bridge.py audit
```

| `openai_base_url` | Path |
|---|---|
| `http://127.0.0.1:4202/_codex-router/.../v1` | Codex Router. This is the verified coexistence path. |
| `http://127.0.0.1:8318/v1` | CLIProxyAPI transparent header proxy. Do not add GLM by switching the default Provider. |
| missing / ChatGPT native | Do not invent a Provider. Ask before installing Router or transparent mode. |

Official Codex docs use Responses at `https://api.z.ai/api/v1`. Codex Router instead talks to the Coding Plan **Chat Completions** endpoint and keeps `model_provider = "openai"`. Prefer the Router path when Router is already the Desktop catalog owner.

## Verified Router procedure

1. Confirm the key is a Coding Plan key. The ordinary BigModel `/api/paas/v4` URL returns `1113`.
2. Write the key to `~/.codex/codex-router/zai-coding-api-key.secret` (mode `0600`). Do not put it on a command line.
3. For a mainland key, set `ZAI_CODING_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4` on the Router process. On macOS that is usually a LaunchAgent env. On Windows, set it in the shell or process that starts Router. Keep the existing Node 24 binary and body-size limits, then restart Router.
4. Add only `glm-5.3` in `~/.codex/codex-router/user-models.json` so a Router upgrade does not drop it. Registry copies may still list `glm-5.2` / `glm-5-turbo`.
5. Hide `zai-coding/glm-5.2` and `zai-coding/glm-5-turbo` in `model-picker.json` unless the user asked for them.
6. Enable the provider:

```bash
./bin/providers enable zai-coding
```

from `~/.local/share/codex-router`, with `CODEX_ROUTER_STATE_DIR` pointing at `~/.codex/codex-router`.

7. If `merged-models.json` still lacks `zai-coding/glm-5.3`, restore `native-models.json` from a native-only snapshot, then run `src/catalog.mjs`. Do **not** run `bin/refresh-catalog` while Router is enabled. That command disables routing even when passed `--help`.
8. Live-test:

```bash
./bin/test-model zai-coding/glm-5.3 --live --yes --quick --json
```

9. Tell the user to fully quit and reopen Codex, including the tray.

Keep root `model_provider = "openai"` and the existing merged catalog. Do not run `npx @z_ai/coding-helper`.

## What not to do

- Do not change the Desktop default Provider to `ZAI` or `cli_proxy`.
- Do not register GLM only in CLIProxyAPI and expect the Router picker to show it. WorkBuddy uses `:8317`; Desktop Router uses `:4202`.
- Do not reinstall Router or switch its Node binary as part of adding GLM.
- Do not copy the key into `experimental_bearer_token`.
- Do not add `glm-5.3` to this Skill's `models/*.json` catalog for a Router-owned Desktop. That catalog is for the isolated CLIProxyAPI profile / 8318 path.

## Isolated CLIProxyAPI profile

Use `configure` + a Responses-proven route when Desktop is **not** on Router, including the default Windows path, and the user accepts `codex --profile cli-proxy`. Chat Completions-only GLM is not Codex-compatible. Do not report success from `/v1/models` alone. Do not require Codex Router or a LaunchAgent to complete the isolated profile.
