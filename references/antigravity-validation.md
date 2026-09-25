# Antigravity account validation recovery

Use this workflow only when the upstream Google response explicitly contains
`403 PERMISSION_DENIED` and `VALIDATION_REQUIRED` / “Verify your account to
continue.” OAuth expiry, 429 quota cooldown, and model-capacity 503s are
different failure classes and must not trigger account re-login.

## Key lesson (verified 2026-09-26)

Google's `VALIDATION_REQUIRED` wall is an **account-level verification
requirement**, not an OAuth token problem. The official CLIProxyAPI re-login
flow (`-antigravity-login`, wrapped by `login-validation`) refreshes the OAuth
credential but does **not** clear the wall — accounts stayed blocked after a
successful OAuth consent. The only verified fix is completing Google's own
challenge at the per-account `validation_url` that Google embeds in the 403
response body (`https://accounts.google.com/signin/continue?...&plt=<account-bound token>`).

Additional verified facts:

- Each account's `validation_url` carries an account-bound `plt` token; URLs
  must not be swapped between accounts.
- CLIProxyAPI keeps cooldown state **in memory** for 30 minutes
  (`next_retry_after`) and re-serves the cached "last upstream error" without
  touching Google. Deleting the `.cds` file alone is therefore not enough;
  after deleting it, CLIProxyAPI must be restarted to force a real retry.
- `validation-plan` / the cds check only looks for the `VALIDATION_REQUIRED`
  marker and ignores cooldown expiry, so a just-fixed account can still appear
  blocked right after a failed probe. Prefer the automated flow below.

## Automated recovery (preferred)

Run the dedicated self-healing command:

```bash
# 预览受影响账号（只读，不打开浏览器）
python3 <skill-dir>/scripts/antigravity_pool.py validation-fix \
  --model gemini-3.8-flash-high

# 执行修复：逐账号打开验证页 → 用户完成验证 → 清冷却 → 重启代理 → 探测
python3 <skill-dir>/scripts/antigravity_pool.py validation-fix \
  --model gemini-3.8-flash-high --apply --restart
```

The command:

1. Collects every Antigravity `.cds` cooldown file that contains a
   `VALIDATION_REQUIRED` record with a `validation_url`, and sorts targets by
   credential priority.
2. Opens each account's validation page in the browser and waits for the user
   to press Enter after finishing Google's challenge (account chooser must
   match the printed email).
3. Backs up and deletes the matching `.cds` files
   (`cds-backup-<timestamp>/` inside the auth dir).
4. Restarts CLIProxyAPI through launchd (`kickstart -k`) to flush in-memory
   cooldown — without the restart the proxy keeps serving the cached 403.
5. Probes `/v1/responses` through the local proxy with the configured API key
   and reports one of: recovered / still `VALIDATION_REQUIRED` / quota
   exhausted (429 — verification succeeded, wait for reset).

Safety properties: it never prints OAuth tokens; the validation URL is opened
directly and not echoed (use `--print-url` only for headless sessions);
`.cds` deletions are backed up; pass `--account` to restrict the fix to
explicitly approved emails.

## If no validation_url is present

Some flagged accounts may have cds records without an embedded URL (e.g. the
403 body was truncated). In that case trigger a fresh 403 by restarting
CLIProxyAPI after clearing the account's cds file, or fall back to the
serial OAuth flow below — but note OAuth re-login alone has never cleared
this wall (see the key lesson above); it only refreshes credentials so the
next 403 carries a fresh validation_url.

### Fallback: serial OAuth re-login (secondary)

1. Read-only plan first: `antigravity_pool.py validation-plan --model <model>`.
2. Present the exact account list and order to the user; start OAuth only
   after they authorize that specific list.
3. `antigravity_pool.py login-validation --model <model> --account <email>
   --apply`, one account at a time; never rely on the browser's default
   account, and stop immediately on mismatch or cancellation.
4. Re-run `validation-fix --apply --restart` afterwards to complete the real
   verification and clear the cooldown.

## Verification and reporting

After `validation-fix` reports `PROBE OK`, optionally re-probe through Codex
itself:

```bash
python3 <skill-dir>/scripts/bridge.py probe --desktop --models gemini-3.8-flash-high
```

Report which accounts were fixed, whether any account hit 429 quota
exhaustion after verification (quota resets are separate from validation and
can take hours to days), and the final probe result. Never claim recovery
before a probe passes.
