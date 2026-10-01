#!/usr/bin/env python3
"""Probe-gated synchronization of official models into the active Codex catalog."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
import time
import urllib.error
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
DEFAULT_STATE = Path(os.environ.get(
    "CODEX_BRIDGE_STATE_DIR", Path.home() / ".config" / "codex-cli-model-bridge"
)).expanduser() / "official-model-sync-state.json"
BASE_INTERVAL_SECONDS = 6 * 60 * 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60
OFFICIAL_PREFIXES = ("gpt-", "codex-", "o1", "o3", "o4")
VERIFIED_SPECS = {
    "gpt-6.1-sol": {
        "template_slug": "gpt-6-sol",
        "display_name": "GPT-6.1 Sol",
        "description": "Near-Astra performance for complex work at a lower cost.",
        "context_window": 1050000,
        "max_context_window": 1050000,
        "default_reasoning_level": "medium",
        "reasoning_levels": ("low", "medium", "high", "xhigh", "max"),
    },
}


def read_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def active_catalog(config_path):
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw = config.get("model_catalog_json")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("active config has no model_catalog_json")
    target = Path(raw).expanduser()
    if not target.is_absolute() or not target.is_file():
        raise ValueError("active model catalog is not an existing absolute file")
    return target


def account_visible_models(auth_path):
    """Return (visible slugs, status); never emit the token or response body."""
    try:
        token = read_json(auth_path).get("tokens", {}).get("access_token")
        if not isinstance(token, str) or not token:
            return None, "auth_unavailable"
        request = urllib.request.Request("https://api.openai.com/v1/models",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=8) as response:
            models = json.load(response).get("models")
        if not isinstance(models, list):
            return None, "invalid_account_catalog"
        return {m["slug"] for m in models if isinstance(m, dict)
                and isinstance(m.get("slug"), str) and m.get("visibility") == "list"}, "ok"
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return None, "account_catalog_unavailable"


def native_entries(cache_path):
    try:
        models = read_json(cache_path).get("models", [])
        return {m["slug"]: m for m in models if isinstance(m, dict)
                and isinstance(m.get("slug"), str)}
    except (OSError, ValueError, TypeError):
        return {}


def source_fingerprints(cache_path, auth_path):
    try:
        cache_digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    except OSError:
        cache_digest = "missing"
    try:
        auth = read_json(auth_path)
        account_id = auth.get("tokens", {}).get("account_id")
        if account_id:
            account_key = f"account:{account_id}"
        else:
            # Older credential formats have no stable account ID. A file change
            # still triggers one check; never persist a token or its hash.
            account_key = f"auth-file:{auth_path.stat().st_mtime_ns}"
    except (OSError, ValueError, TypeError, AttributeError):
        account_key = "auth-unavailable"
    return cache_digest, hashlib.sha256(account_key.encode()).hexdigest()


def read_sync_state(path):
    try:
        state = read_json(path)
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            return {}
        if not isinstance(state.get("next_due"), (int, float)):
            return {}
        if not isinstance(state.get("failures"), int) or not 0 <= state["failures"] <= 3:
            return {}
        return state
    except (OSError, ValueError, TypeError):
        return {}


def write_sync_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, 0o600)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def resolve_codex_cli():
    requested = os.environ.get("CODEX_CLI_PATH")
    candidates = [requested, shutil.which("codex")]
    for applications in (Path("/Applications"), Path.home() / "Applications"):
        for app in ("ChatGPT.app", "Codex.app"):
            resources = applications / app / "Contents" / "Resources"
            candidates.extend((
                resources / "codex-cli" / "CodexCLI.app" / "Contents" / "MacOS" / "codex",
                resources / "codex",
            ))
    candidates.extend(("/opt/homebrew/bin/codex", "/usr/local/bin/codex"))
    for candidate in candidates:
        if candidate and (found := shutil.which(str(candidate))):
            return found
    return "codex"


def candidate_entry(slug, current, native):
    if slug in native:
        return copy.deepcopy(native[slug])
    spec = VERIFIED_SPECS.get(slug)
    if not spec:
        return None
    template = next((m for m in current if m.get("slug") == spec["template_slug"]), None)
    if template is None:
        template = native.get(spec["template_slug"])
    if template is None:
        return None
    entry = copy.deepcopy(template)
    for key in ("display_name", "description", "context_window", "max_context_window", "default_reasoning_level"):
        entry[key] = spec[key]
    entry["slug"] = slug
    entry["visibility"] = "list"
    descriptions = {level["effort"]: level["description"] for level in template.get("supported_reasoning_levels", [])
                    if isinstance(level, dict) and "effort" in level and "description" in level}
    entry["supported_reasoning_levels"] = [
        {"effort": effort, "description": descriptions.get(effort, effort)}
        for effort in spec["reasoning_levels"]
    ]
    return entry


def probe_candidate(slug, payload, codex, timeout=90):
    """Run a real, read-only Codex turn with a private temporary catalog."""
    with tempfile.TemporaryDirectory(prefix="codex-official-probe-") as raw:
        temp = Path(raw)
        os.chmod(temp, 0o700)
        temp_catalog = temp / "catalog.json"
        temp_catalog.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(temp_catalog, 0o600)
        output = temp / "answer.txt"
        command = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
                   "--sandbox", "read-only", "--model", slug,
                   "--config", f'model_catalog_json={json.dumps(str(temp_catalog))}',
                   "--output-last-message", str(output), "Reply with exactly: CODEX_BRIDGE_OK"]
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
            answer_ok = output.is_file() and output.read_text(encoding="utf-8").strip() == "CODEX_BRIDGE_OK"
            if proc.returncode == 0 and answer_ok:
                return True, "completed"
            diagnostic = proc.stderr.lower()
            for marker, label in (
                ("unexpected argument", "cli_arguments"),
                ("unknown option", "cli_arguments"),
                ("error parsing", "cli_arguments"),
                ("failed to parse", "cli_arguments"),
                ("invalid value", "cli_arguments"),
                ("unknown model", "model_unavailable"),
                ("model_not_found", "model_unavailable"),
                ("not found", "model_unavailable"),
                ("not supported", "model_unavailable"),
                ("model is not available", "model_unavailable"),
                ("unauthorized", "auth_denied"),
                ("forbidden", "auth_denied"),
                ("403", "auth_denied"),
                ("401", "auth_denied"),
                ("429", "rate_limited"),
                ("400 bad request", "route_rejected"),
                ("502", "route_unavailable"),
                ("connection refused", "route_unavailable"),
                ("stream disconnected", "route_unavailable"),
                ("invalid catalog", "catalog_invalid"),
                ("model catalog", "catalog_invalid"),
            ):
                if marker in diagnostic:
                    return False, label
            return False, "incomplete_response" if proc.returncode == 0 else f"codex_exit_{proc.returncode}"
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except OSError:
            return False, "codex_unavailable"


def atomic_catalog_write(target, payload, original_digest):
    if hashlib.sha256(target.read_bytes()).hexdigest() != original_digest:
        raise ValueError("active catalog changed during verification")
    backup = target.with_name(f"{target.name}.backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    shutil.copy2(target, backup)
    os.chmod(backup, 0o600)
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, 0o600)
        os.replace(raw, target)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)
    return backup


def sync_catalog(config_path, cache_path, auth_path, *, candidate=None, apply=False,
                 codex="codex", account_fetch=account_visible_models, probe=probe_candidate,
                 state_path=DEFAULT_STATE, clock=time.time, force=False):
    target = active_catalog(config_path)
    original = target.read_bytes()
    catalog = json.loads(original)
    models = catalog.get("models")
    if not isinstance(models, list):
        raise ValueError("active catalog has no models array")
    now = clock()
    cache_digest, account_digest = source_fingerprints(cache_path, auth_path)
    state = read_sync_state(state_path) if apply else {}
    source_changed = (state.get("cache_digest") != cache_digest or
                      state.get("account_digest") != account_digest or
                      state.get("catalog") != str(target))
    if apply and not force and not candidate and not source_changed and now < state.get("next_due", 0):
        return {"status": "deferred", "catalog": str(target),
                "next_due": state["next_due"], "added_models": []}

    def finish(result):
        if apply:
            failure = result.get("account_catalog") != "ok" or bool(
                result.get("probe_failed") or result.get("pending_metadata")
            )
            previous = state.get("failures", 0) if not source_changed else 0
            failures = min(previous + 1, 3) if failure else 0
            delay = min(BASE_INTERVAL_SECONDS * 2 ** max(failures - 1, 0), MAX_INTERVAL_SECONDS)
            write_sync_state(state_path, {
                "schema_version": 1, "catalog": str(target),
                "cache_digest": cache_digest, "account_digest": account_digest,
                "failures": failures, "next_due": now + delay,
                "last_status": result["status"],
            })
            result["next_due"] = now + delay
        return result

    current = {m.get("slug") for m in models if isinstance(m, dict)}
    native = native_entries(cache_path)
    visible, account_status = account_fetch(auth_path)
    discovered = ({slug for slug, entry in native.items()
                   if slug.startswith(OFFICIAL_PREFIXES) and entry.get("visibility") == "list"} |
                  {slug for slug in (visible or set()) if slug.startswith(OFFICIAL_PREFIXES)} |
                  set(VERIFIED_SPECS)) - current
    if visible is not None:
        discovered &= visible
    if candidate:
        if not candidate.startswith(OFFICIAL_PREFIXES):
            raise ValueError("candidate is not an official model slug")
        if candidate not in current:
            discovered.add(candidate)
    pending_metadata = sorted(slug for slug in discovered if candidate_entry(slug, models, native) is None)
    available = sorted(discovered - set(pending_metadata))
    result = {"status": "unchanged", "catalog": str(target), "account_catalog": account_status,
              "candidates": available, "pending_metadata": pending_metadata, "added_models": []}
    if not apply or not available:
        result["status"] = "planned" if available or pending_metadata else "unchanged"
        return finish(result)
    approved = []
    for slug in available:
        entry = candidate_entry(slug, models, native)
        temporary_payload = {**catalog, "models": models + [entry]}
        outcome = probe(slug, temporary_payload, codex)
        passed, reason = outcome if isinstance(outcome, tuple) else (outcome, "probe_failed")
        if passed:
            approved.append(entry)
        else:
            result.setdefault("probe_diagnostics", {})[slug] = reason
    result["probe_failed"] = sorted(set(available) - {m["slug"] for m in approved})
    if approved:
        if active_catalog(config_path) != target:
            raise ValueError("active catalog changed during verification")
        payload = {**catalog, "models": models + approved}
        result["backup"] = str(atomic_catalog_write(target, payload, hashlib.sha256(original).hexdigest()))
        result["added_models"] = [m["slug"] for m in approved]
        result["status"] = "applied"
    else:
        result["status"] = "blocked"
    return finish(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CODEX_HOME / "config.toml")
    parser.add_argument("--native-cache", type=Path, default=CODEX_HOME / "models_cache.json")
    parser.add_argument("--auth", type=Path, default=CODEX_HOME / "auth.json")
    parser.add_argument("--candidate", help="Exact official slug for an explicit, probe-gated bootstrap")
    parser.add_argument("--codex", default=resolve_codex_cli())
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--force", action="store_true", help="Bypass the retry window for a manual check")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = sync_catalog(args.config, args.native_cache, args.auth,
                              candidate=args.candidate, apply=args.apply, codex=args.codex,
                              state_path=args.state, force=args.force)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "blocked", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
