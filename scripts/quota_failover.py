#!/usr/bin/env python3
"""
Codex Autoheal Bridge quota failover helper.

Goals:
- When ChatGPT/Codex included usage is exhausted and Codex Desktop disables Send,
  switch Desktop to the existing local CLIProxyAPI provider whose
  requires_openai_auth=false, so external models remain usable.
- Record the authoritative Codex rate-limit snapshot and reset time.
- While in external mode, poll very lightly and restore the user's previous
  OpenAI transparent-router configuration only after the backend confirms
  ordinaryUsageAllowed=true AND the Codex primary/secondary windows are <100%.
- Never fake quota state, patch Desktop UI, or edit Codex thread history/state DB.

This file is designed to live next to scripts/bridge.py and reuses its safe
backup/atomic-write/TOML helpers.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

import bridge


STATE_VERSION = 1
WATCH_LABEL = "com.zhijian.codex-cli-model-bridge-quota"
DEFAULT_QUOTA_STATE = bridge.DEFAULT_STATE_DIR / "quota-state.json"
DEFAULT_EXTERNAL_CATALOG = bridge.DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.external.json"
DEFAULT_WATCH_PLIST = Path(
    "~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-quota.plist"
).expanduser()
DEFAULT_CONFIG = bridge.DEFAULT_CODEX_HOME / "config.toml"
DEFAULT_PROFILE = bridge.DEFAULT_PROFILE_CONFIG
DEFAULT_CATALOG = bridge.DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.json"
EXTERNAL_PROVIDER_ID = bridge.PROVIDER_ID
tomllib = bridge.tomllib


def is_windows() -> bool:
    return bridge.is_windows()

EXTERNAL_PREFIXES = (
    "gemini-",
    "claude-",
    "deepseek-",
    "glm-",
    "minimax",
    "qwen-",
    "kimi-",
    "moonshot-",
    "doubao-",
    "baichuan-",
    "yi-",
    "llama-",
    "mistral-",
    "grok-",
    "zai-",
    "custom-",
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def iso_from_epoch(value: int | float | None) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat(timespec="seconds")


def epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def emit(value: Any, exit_code: int = 0) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def compute_lifecycle_state(state: dict[str, Any]) -> str:
    desired = state.get("desired_mode") or state.get("mode", "openai")
    effective = state.get("effective_mode") or state.get("mode", "openai")
    pending = bool(state.get("pending_restart", False))

    if desired == "openai" and effective == "openai" and not pending:
        return "OPENAI_ACTIVE"
    if desired == "external" and pending:
        return "EXTERNAL_RESTART_REQUIRED"
    if desired == "external" and effective == "external" and not pending:
        return "EXTERNAL_ACTIVE"
    if desired == "openai" and pending:
        return "OPENAI_RESTORE_PENDING"
    return "OPENAI_ACTIVE" if desired == "openai" else "EXTERNAL_ACTIVE"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        initial: dict[str, Any] = {
            "schema_version": STATE_VERSION,
            "mode": "openai",
            "desired_mode": "openai",
            "effective_mode": "openai",
            "pending_restart": False,
            "state_machine": "OPENAI_ACTIVE",
        }
        return initial
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": STATE_VERSION,
            "mode": "unknown",
            "desired_mode": "unknown",
            "effective_mode": "unknown",
            "pending_restart": False,
            "state_machine": "UNKNOWN",
        }
    if not isinstance(value, dict):
        return {
            "schema_version": STATE_VERSION,
            "mode": "unknown",
            "desired_mode": "unknown",
            "effective_mode": "unknown",
            "pending_restart": False,
            "state_machine": "UNKNOWN",
        }
    mode = value.get("mode", "openai")
    value.setdefault("desired_mode", mode)
    value.setdefault("effective_mode", mode)
    value.setdefault("pending_restart", False)
    value["state_machine"] = compute_lifecycle_state(value)
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    payload = dict(state)
    payload["schema_version"] = STATE_VERSION
    payload["state_machine"] = compute_lifecycle_state(payload)
    bridge.atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        0o600,
    )


def _reader_thread(stream, target: queue.Queue[str | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            target.put(line)
    finally:
        target.put(None)


def _wait_jsonrpc(
    target: queue.Queue[str | None],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        timeout = max(0.05, deadline - time.monotonic())
        try:
            line = target.get(timeout=timeout)
        except queue.Empty:
            break
        if line is None:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            return payload
    raise TimeoutError(f"timed out waiting for JSON-RPC response id={request_id}")


def read_codex_rate_limits(codex: str | None = None, timeout: int = 20) -> dict[str, Any]:
    """
    Ask Codex's own app-server for account/rateLimits/read.

    This intentionally uses the supported app-server account endpoint instead of
    parsing Desktop UI state or reading private auth tokens directly.
    """
    executable = bridge.resolve_codex_cli(codex)
    proc = subprocess.Popen(
        [executable, "app-server", "-c", 'model_provider="openai"'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    if proc.stdin is None or proc.stdout is None:
        proc.kill()
        raise RuntimeError("failed to open codex app-server stdio")

    q: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True).start()
    deadline = time.monotonic() + timeout

    try:
        initialize = {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "codex-autoheal-bridge",
                    "title": "Codex Autoheal Bridge",
                    "version": "1",
                }
            },
        }
        proc.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        init_response = _wait_jsonrpc(q, 1, deadline)
        if "error" in init_response:
            raise RuntimeError(f"app-server initialize failed: {init_response['error']}")

        proc.stdin.write('{"method":"initialized"}\n')
        proc.stdin.write('{"method":"account/rateLimits/read","id":2}\n')
        proc.stdin.flush()

        response = _wait_jsonrpc(q, 2, deadline)
        if "error" in response:
            raise RuntimeError(f"account/rateLimits/read failed: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("account/rateLimits/read returned no result object")
        return result
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        proc.stdin.close()
        proc.stdout.close()


def _window(snapshot: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = snapshot.get(name)
    return value if isinstance(value, dict) else None


def select_codex_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex = by_id.get("codex")
        if isinstance(codex, dict):
            return codex
    fallback = payload.get("rateLimits")
    return fallback if isinstance(fallback, dict) else {}


def analyze_rate_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Recovery policy:
      1. ordinaryUsageAllowed MUST be explicitly true.
      2. Codex primary and secondary windows, when present, MUST be <100%.
      3. spendControlReached MUST NOT be true.

    Reset time is advisory only. It schedules the next verification; it never
    causes a switch-back by itself.
    """
    codex = select_codex_snapshot(payload)
    ordinary = payload.get("ordinaryUsageAllowed")
    spend_reached = codex.get("spendControlReached")

    windows: list[dict[str, Any]] = []
    malformed_window = False
    for name in ("primary", "secondary"):
        if codex.get(name) is None:
            continue
        raw = _window(codex, name)
        if raw is None:
            malformed_window = True
            continue
        used = raw.get("usedPercent")
        reset = raw.get("resetsAt")
        exhausted = isinstance(used, (int, float)) and used >= 100
        windows.append(
            {
                "name": name,
                "used_percent": used,
                "window_duration_mins": raw.get("windowDurationMins"),
                "resets_at": reset,
                "resets_at_iso": iso_from_epoch(reset),
                "exhausted": exhausted,
            }
        )

    individual = codex.get("individualLimit")
    individual_blocked = bool(
        isinstance(individual, dict)
        and isinstance(individual.get("remainingPercent"), (int, float))
        and individual.get("remainingPercent") <= 0
    )
    individual_reset = individual.get("resetsAt") if isinstance(individual, dict) else None

    blocking_windows = [item for item in windows if item["exhausted"]]
    candidate_resets = [
        item["resets_at"]
        for item in blocking_windows
        if isinstance(item.get("resets_at"), (int, float))
    ]
    if individual_blocked and isinstance(individual_reset, (int, float)):
        candidate_resets.append(individual_reset)

    expected_recovery = max(candidate_resets) if candidate_resets else None

    windows_below_100 = not malformed_window and all(
        isinstance(item.get("used_percent"), (int, float))
        and not isinstance(item["used_percent"], bool)
        and 0 <= item["used_percent"] < 100
        for item in windows
    )
    recovered = (
        ordinary is True
        and bool(codex)
        and windows_below_100
        and spend_reached is not True
        and not individual_blocked
    )
    blocked = ordinary is False or bool(blocking_windows) or spend_reached is True or individual_blocked

    return {
        "ordinary_usage_allowed": ordinary,
        "rate_limit_reached_type": codex.get("rateLimitReachedType"),
        "spend_control_reached": spend_reached,
        "windows": windows,
        "blocking_windows": blocking_windows,
        "individual_limit_blocked": individual_blocked,
        "expected_recovery_at": expected_recovery,
        "expected_recovery_at_iso": iso_from_epoch(expected_recovery),
        "blocked": blocked,
        "recovered": recovered,
    }


def compact_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    codex = select_codex_snapshot(payload)
    return {
        "ordinaryUsageAllowed": payload.get("ordinaryUsageAllowed"),
        "rateLimits": {
            "limitId": codex.get("limitId"),
            "limitName": codex.get("limitName"),
            "primary": codex.get("primary"),
            "secondary": codex.get("secondary"),
            "credits": codex.get("credits"),
            "individualLimit": codex.get("individualLimit"),
            "spendControlReached": codex.get("spendControlReached"),
            "planType": codex.get("planType"),
            "rateLimitReachedType": codex.get("rateLimitReachedType"),
        },
    }


def refresh_state(
    state_path: Path,
    codex: str,
    *,
    allow_error: bool = False,
    persist: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = load_state(state_path)
    try:
        payload = read_codex_rate_limits(codex)
        analysis = analyze_rate_limits(payload)
        state.update(
            {
                "last_checked_at": iso_now(),
                "last_successful_check_at": iso_now(),
                "last_snapshot": compact_snapshot(payload),
                "last_analysis": analysis,
                "blocking_window": analysis["blocking_windows"],
                "resets_at": analysis["expected_recovery_at"],
                "resets_at_iso": analysis["expected_recovery_at_iso"],
                "last_error": None,
            }
        )
        if persist:
            save_state(state_path, state)
        return state, analysis
    except Exception as exc:
        state.update(
            {
                "last_checked_at": iso_now(),
                "last_error": f"{type(exc).__name__}: {exc}",
            }
        )
        if persist:
            save_state(state_path, state)
        if allow_error:
            return state, None
        raise


def _root_value_state(config: dict[str, Any], key: str) -> dict[str, Any]:
    return {"present": key in config, "value": config.get(key)}


def capture_openai_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _root_value_state(config, key)
        for key in ("model_provider", "model", "openai_base_url", "model_catalog_json")
    }


DEFAULT_ROUTER_URL = getattr(bridge, "DEFAULT_TRANSPARENT_PROXY_URL", "http://127.0.0.1:8318/v1")


def _normalize_value_state(config: dict[str, Any], key: str) -> dict[str, Any]:
    val = config.get(key)
    if isinstance(val, dict) and "present" in val and "value" in val:
        return {"present": bool(val.get("present")), "value": val.get("value")}
    return {"present": key in config, "value": val}


def routing_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _normalize_value_state(config, key)
        for key in ("model_provider", "openai_base_url", "model_catalog_json")
    }


def is_routing_conflict(current_config: dict[str, Any], expected_config: dict[str, Any]) -> bool:
    if not isinstance(current_config, dict) or not isinstance(expected_config, dict):
        return True
    return routing_fingerprint(current_config) != routing_fingerprint(expected_config)


def _restore_root_value(text: str, key: str, state: dict[str, Any]) -> str:
    if state.get("present"):
        value = state.get("value")
        if not isinstance(value, str):
            raise ValueError(f"saved {key} value is not a string")
        return bridge.replace_top_scalar(text, key, value)
    return bridge.remove_top_scalar(text, key)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def render_cli_proxy_provider(
    profile: dict[str, Any],
    router_base_url: str = DEFAULT_ROUTER_URL,
) -> str:
    provider = dict(profile.get("model_providers", {}).get(EXTERNAL_PROVIDER_ID, {}))
    if not provider:
        raise ValueError(
            f"{EXTERNAL_PROVIDER_ID} provider is missing; run bridge.py configure --apply first"
        )
    base_url = router_base_url
    if not isinstance(base_url, str) or not bridge.is_loopback(base_url):
        raise ValueError("external provider base_url must be loopback-only")

    top_fields = (
        "name",
        "wire_api",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "supports_websockets",
    )
    lines = [f"[model_providers.{EXTERNAL_PROVIDER_ID}]"]
    lines.append(f'name = {toml_value(provider.get("name", "External Models"))}')
    lines.append(f"base_url = {toml_value(base_url)}")
    lines.append(f'wire_api = {toml_value(provider.get("wire_api", "responses"))}')
    for key in top_fields:
        if key in ("name", "wire_api"):
            continue
        if key in provider:
            lines.append(f"{key} = {toml_value(provider[key])}")
    lines.append("requires_openai_auth = false")

    auth = provider.get("auth")
    if isinstance(auth, dict) and "command" in auth:
        lines.extend(["", f"[model_providers.{EXTERNAL_PROVIDER_ID}.auth]"])
        for key in ("command", "args", "timeout_ms", "refresh_interval_ms"):
            if key in auth:
                lines.append(f"{key} = {toml_value(auth[key])}")

    lines.extend([
        "",
        f"[model_providers.{EXTERNAL_PROVIDER_ID}.http_headers]",
        'x-codex-bridge-mode = "external"',
    ])
    return "\n".join(lines)


def is_external_slug(slug: str) -> bool:
    lower = slug.lower()
    return lower.startswith(EXTERNAL_PREFIXES) and bridge.is_router_supported(slug)


def build_external_catalog(
    source_payload: dict[str, Any],
    managed_ids: set[str],
    allowed_external_slugs: set[str] | None = None,
) -> dict[str, Any]:
    models = source_payload.get("models")
    if not isinstance(models, list):
        raise ValueError("source catalog must contain models array")

    out_models: list[dict[str, Any]] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        slug = str(item.get("slug", ""))
        is_allowed = (slug in allowed_external_slugs) if allowed_external_slugs is not None else True
        external = (
            is_allowed
            and (slug in managed_ids or is_external_slug(slug))
            and bridge.is_router_supported(slug)
        )
        if external:
            if item.get("supported_in_api") is not False:
                item["visibility"] = "list"
        else:
            # Keep native metadata available for Codex internals/thread handling,
            # but do not let the external-provider picker choose a GPT route that
            # CLIProxyAPI cannot authenticate as ChatGPT.
            item["visibility"] = "hide"
        out_models.append(item)
    out_models.sort(key=lambda item: int(item.get("priority", 9999)))
    return {"models": out_models}


def choose_external_model(payload: dict[str, Any], requested: str | None = None) -> str:
    models = payload.get("models", [])
    candidates = [
        item
        for item in models
        if isinstance(item, dict)
        and item.get("visibility") != "hide"
        and item.get("supported_in_api") is not False
        and isinstance(item.get("slug"), str)
        and is_external_slug(item["slug"])
    ]
    if requested:
        if any(item["slug"] == requested for item in candidates):
            return requested
        raise ValueError(f"requested external model is not available: {requested}")
    if not candidates:
        raise ValueError("external catalog contains no usable third-party model")
    candidates.sort(key=lambda item: int(item.get("priority", 9999)))
    return str(candidates[0]["slug"])


def prepare_external_catalog(
    source_path: Path,
    state_dir: Path,
    target_path: Path,
    *,
    apply: bool = True,
    manifests_dir: Path | None = None,
) -> tuple[dict[str, Any], str]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    bridge_state = bridge.read_json(
        state_dir / "state.json",
        {"schema_version": 1, "managed_model_ids": []},
    )
    managed = set(bridge_state.get("managed_model_ids", [])) if isinstance(bridge_state, dict) else set()

    # Overlay local manifests from models/ if not yet present in source catalog
    models_dir = manifests_dir or (bridge.SKILL_DIR / "models")
    manifest_slugs: set[str] = set()
    if models_dir.exists():
        existing_slugs = {item.get("slug") for item in source.get("models", []) if isinstance(item, dict)}
        templates = {
            item["slug"]: item
            for item in source.get("models", [])
            if isinstance(item, dict) and "slug" in item
        }
        for manifest_file in sorted(models_dir.glob("*.json")):
            try:
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                slug = manifest_data.get("slug")
                if slug:
                    manifest_slugs.add(slug)
                if slug and manifest_data.get("template_slug") in templates:
                    if slug not in existing_slugs:
                        new_entry = bridge.build_entry(manifest_data, templates)
                        source.setdefault("models", []).append(new_entry)
                        existing_slugs.add(slug)
                        managed.add(slug)
                    else:
                        for idx, item in enumerate(source.get("models", [])):
                            if isinstance(item, dict) and item.get("slug") == slug:
                                source["models"][idx] = bridge.build_entry(manifest_data, templates)
                                break
                        managed.add(slug)
            except Exception:
                pass

    custom_existing_slugs = {
        item["slug"]
        for item in source.get("models", [])
        if isinstance(item, dict)
        and isinstance(item.get("slug"), str)
        and item["slug"].lower().startswith(("minimax", "qwen-", "kimi-", "moonshot-", "doubao-", "baichuan-", "yi-"))
        and bridge.is_router_supported(item["slug"])
    }
    allowed_slugs = (manifest_slugs | managed | custom_existing_slugs) if manifest_slugs else None
    payload = build_external_catalog(source, managed, allowed_external_slugs=allowed_slugs)
    if apply:
        bridge.atomic_write(
            target_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            0o600,
        )
    return payload, str(target_path)


def app_running() -> bool:
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["/bin/ps", "-axww", "-o", "command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            stdout = getattr(proc, "stdout", None)
            if proc.returncode == 0 and isinstance(stdout, str) and stdout:
                for line in stdout.splitlines():
                    cmd = line.strip()
                    if any(f"/{name}.app/Contents/MacOS/{name}" in cmd for name in ("ChatGPT", "Codex")):
                        return True
            return any(
                subprocess.run(
                    ["/usr/bin/pgrep", "-x", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode == 0
                for name in ("Codex", "ChatGPT")
            )
        except (OSError, subprocess.SubprocessError):
            return False
    elif is_windows():
        try:
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            stdout = getattr(proc, "stdout", None)
            if proc.returncode == 0 and isinstance(stdout, str) and stdout:
                for line in stdout.splitlines():
                    lower = line.lower()
                    if '"codex.exe"' in lower or '"chatgpt.exe"' in lower or 'codex.exe' in lower or 'chatgpt.exe' in lower:
                        return True
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def notify(title: str, message: str) -> None:
    if sys.platform == "darwin":
        safe_title = title.replace('"', '\\"')
        safe_message = message.replace('"', '\\"')
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'display notification "{safe_message}" with title "{safe_title}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    elif is_windows():
        try:
            safe_t = title.replace("'", "''")
            safe_m = message.replace("'", "''")
            ps_script = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;"
                "$n = new-object System.Windows.Forms.NotifyIcon;"
                "$n.Icon = [System.Drawing.SystemIcons]::Information;"
                "$n.Visible = $true;"
                f"$n.ShowBalloonTip(5000, '{safe_t}', '{safe_m}', [System.Windows.Forms.ToolTipIcon]::Info)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def desktop_app_path(codex: str | None = None) -> Path | None:
    """Select the enclosing desktop app, never the nested CodexCLI.app."""
    try:
        executable = Path(bridge.resolve_codex_cli(codex))
    except FileNotFoundError:
        return None

    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.name in ("ChatGPT.app", "Codex.app"):
                return parent
        # A separately installed CLI does not identify which Desktop to restart.
        return None
    elif is_windows():
        for parent in executable.parents:
            for name in ("Codex.exe", "ChatGPT.exe"):
                app = parent / name
                if app.is_file():
                    return app
        local_app = os.environ.get("LOCALAPPDATA")
        prog_files = os.environ.get("ProgramFiles")
        candidates = []
        if local_app:
            candidates.extend([
                Path(local_app) / "Programs" / "Codex" / "Codex.exe",
                Path(local_app) / "Programs" / "ChatGPT" / "ChatGPT.exe",
            ])
        if prog_files:
            candidates.extend([
                Path(prog_files) / "Codex" / "Codex.exe",
                Path(prog_files) / "ChatGPT" / "ChatGPT.exe",
            ])
        for cand in candidates:
            if cand.is_file():
                return cand
        return None
    return None


def restart_codex(codex: str | None = None) -> bool:
    app = desktop_app_path(codex)
    if app is None:
        return False
    if sys.platform == "darwin":
        try:
            quit_result = subprocess.run(
                ["/usr/bin/osascript", "-e", f"tell application {json.dumps(str(app))} to quit"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if quit_result.returncode != 0:
                return False

            # 等待旧进程完全退出并释放 CDP 调试端口（最多等待 8 秒，轮询步长 200ms）
            deadline = time.time() + 8.0
            while time.time() < deadline and app_running():
                time.sleep(0.2)

            # 若超时仍有残余进程，进行温和清理确保端口释放
            if app_running():
                for name in ("Codex", "ChatGPT"):
                    subprocess.run(["/usr/bin/pkill", "-x", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                deadline_cleanup = time.time() + 3.0
                while time.time() < deadline_cleanup and app_running():
                    time.sleep(0.2)
                time.sleep(0.5)

            time.sleep(0.5)
            quota_header_app = Path.home() / 'Applications/Codex Quota Header.app'
            open_cmd = ['/usr/bin/open', str(quota_header_app)] if quota_header_app.exists() else ['/usr/bin/open', '-a', str(app)]
            proc = subprocess.run(
                open_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    elif is_windows():
        try:
            for name in ("Codex.exe", "ChatGPT.exe"):
                subprocess.run(["taskkill", "/F", "/IM", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            deadline = time.time() + 5.0
            while time.time() < deadline and app_running():
                time.sleep(0.2)
            time.sleep(0.5)
            subprocess.Popen([str(app)], shell=False)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def apply_external_mode(
    args: argparse.Namespace,
    *,
    quota_result: tuple[dict[str, Any], dict[str, Any]] | None = None,
    expected_routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config_path = Path(args.config).expanduser()
    profile_path = Path(args.profile_config).expanduser()
    external_catalog = Path(args.external_catalog).expanduser()
    state_path = Path(args.state).expanduser()
    state_dir = Path(args.state_dir).expanduser()

    config, config_error = bridge.load_config(config_path)
    if config_error:
        raise RuntimeError(f"Codex config unavailable: {config_error}")
    prior_state = load_state(state_path)
    saved_catalog = prior_state.get("source_catalog") if prior_state.get("mode") == "external" else None
    source_catalog = Path(
        args.catalog or saved_catalog or config.get("model_catalog_json") or DEFAULT_CATALOG
    ).expanduser()
    profile, profile_error = bridge.load_config(profile_path)
    if profile_error:
        raise RuntimeError(
            f"CLI proxy profile unavailable: {profile_error}; run bridge.py configure --apply first"
        )
    if not source_catalog.exists():
        raise RuntimeError(f"bridge catalog is missing: {source_catalog}")

    is_manual_toggle = bool(getattr(args, "toggle", False) or getattr(args, "command", "") in ("toggle", "toggle-mode"))
    if quota_result is not None:
        state, analysis = quota_result
    elif is_manual_toggle:
        # 方案1优化：手动切换模式直接复用本地已有快照，解除对远程网络 app-server 查询的同步阻塞
        state = load_state(state_path)
        analysis = state.get("last_analysis")
    else:
        state, analysis = refresh_state(
            state_path, args.codex, allow_error=not args.apply, persist=args.apply
        )
    active_provider = config.get("model_provider", "openai")
    if active_provider != "openai" and not (
        active_provider == EXTERNAL_PROVIDER_ID and state.get("mode") == "external"
    ):
        raise RuntimeError("external mode requires the OpenAI baseline or an existing managed external mode")
    original = state.get("openai_config")
    saved_model = original.get("model", {}).get("value") if isinstance(original, dict) else None
    if saved_model and is_external_slug(str(saved_model)):
        original = None
    if state.get("mode") != "external" or not isinstance(original, dict):
        original = capture_openai_config(config)
        captured_model = original.get("model", {}).get("value")
        if captured_model and is_external_slug(str(captured_model)):
            original["model"] = {"present": True, "value": "gpt-6.1-sol"}

    external_payload, external_catalog_str = prepare_external_catalog(
        source_catalog, state_dir, external_catalog, apply=False
    )
    target_model = choose_external_model(external_payload, args.model)
    provider_block = render_cli_proxy_provider(profile)

    current = config_path.read_text(encoding="utf-8")
    if expected_routing is not None and is_routing_conflict(tomllib.loads(current), expected_routing):
        raise RuntimeError("routing changed during the quota query; refusing automatic entry")
    updated = bridge.replace_top_scalar(current, "model_provider", EXTERNAL_PROVIDER_ID)
    updated = bridge.replace_top_scalar(updated, "model_catalog_json", external_catalog_str)
    updated = bridge.replace_top_scalar(updated, "model", target_model)
    updated = bridge.replace_provider_block(updated, EXTERNAL_PROVIDER_ID, provider_block)

    result: dict[str, Any] = {
        "status": "planned" if not args.apply else "unchanged",
        "mode": "external",
        "model": target_model,
        "config": str(config_path),
        "external_catalog": external_catalog_str,
        "quota": analysis,
        "last_error": state.get("last_error"),
        "backup": None,
        "restart_required": updated != current,
        "history_scope_warning": (
            "Changing the default provider can hide OpenAI task history in the UI. "
            "No task rows are modified; returning to the saved provider restores its history scope."
        ),
    }

    if args.apply:
        bridge.atomic_write(
            external_catalog, json.dumps(external_payload, ensure_ascii=False, indent=2) + "\n", 0o600
        )
    if args.apply and updated != current:
        result["backup"] = str(bridge.backup(config_path))
        bridge.atomic_write(config_path, updated, 0o600)
        result["status"] = "applied"

    if args.apply:
        running = app_running()
        restarted = restart_codex(args.codex) if args.restart else False
        pending_restart = running and not restarted
        effective_mode = "external" if restarted or not running else "openai"
        lifecycle_state = "EXTERNAL_ACTIVE" if not pending_restart else "EXTERNAL_RESTART_REQUIRED"
        is_manual_toggle = bool(getattr(args, "toggle", False) or getattr(args, "command", "") in ("toggle", "toggle-mode"))

        state.update(
            {
                "mode": "external",
                "desired_mode": "external",
                "effective_mode": effective_mode,
                "pending_restart": pending_restart,
                "restart_reason": "manual_failover" if is_manual_toggle else ("quota_exhausted_failover" if pending_restart else None),
                "manual_toggle": is_manual_toggle,
                "lifecycle_state": lifecycle_state,
                "entered_external_at": (
                    state.get("entered_external_at") if prior_state.get("mode") == "external"
                    else iso_now()
                ),
                "openai_config": original,
                "external_config": capture_openai_config(tomllib.loads(updated)),
                "external_model": target_model,
                "external_catalog": external_catalog_str,
                "switch_back_status": "manual" if is_manual_toggle else "watching",
                "config_path": str(config_path),
                "profile_config": str(profile_path),
                "source_catalog": str(source_catalog),
            }
        )
        save_state(state_path, state)

        if sys.platform == "darwin":
            if is_manual_toggle or getattr(args, "no_watch", False):
                domain = f"gui/{os.getuid()}"
                target = f"{domain}/{WATCH_LABEL}"
                subprocess.run(
                    ["/bin/launchctl", "bootout", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                result["watcher"] = "disabled"
            elif not args.no_watch and state.get("auto_monitor", {}).get("enabled") is not True:
                install_watcher(
                    script=Path(__file__).resolve(),
                    plist_path=Path(args.watch_plist).expanduser(),
                    state_path=state_path,
                    codex=args.codex,
                )
                result["watcher"] = "installed"
        elif is_windows():
            if is_manual_toggle or getattr(args, "no_watch", False):
                uninstall_windows_watcher()
                result["watcher"] = "disabled"
            elif not args.no_watch and state.get("auto_monitor", {}).get("enabled") is not True:
                install_windows_watcher(
                    script=Path(__file__).resolve(),
                    state_path=state_path,
                    codex=args.codex,
                )
                result["watcher"] = "installed"

        if args.restart:
            result["codex_restarted"] = restarted
        else:
            notify(
                "Codex Autoheal Bridge",
                "已切到外部模型配置；请在完成当前任务后重启，并确认外部模型可用。",
            )
    return result


def restore_openai_mode(
    *,
    state_path: Path,
    config_path: Path | None = None,
    apply: bool = True,
    restart: bool = False,
    codex: str | None = None,
) -> dict[str, Any]:
    state = load_state(state_path)
    saved = state.get("openai_config")
    if not isinstance(saved, dict):
        raise RuntimeError("no saved OpenAI configuration; enter external mode first")

    actual_config = config_path or Path(state.get("config_path") or DEFAULT_CONFIG).expanduser()
    current = actual_config.read_text(encoding="utf-8")
    parsed = tomllib.loads(current)
    if state.get("mode") != "external":
        return {"status": "unchanged", "mode": state.get("mode"), "restart_required": False}
    expected = state.get("external_config")
    if isinstance(expected, dict):
        if is_routing_conflict(parsed, expected):
            raise RuntimeError("routing config changed since external mode; refusing to overwrite user edits")
    elif parsed.get("model_provider") != EXTERNAL_PROVIDER_ID:
        raise RuntimeError("active provider changed; refusing to restore an obsolete configuration")
    updated = current
    for key in ("model_provider", "model", "openai_base_url", "model_catalog_json"):
        item = saved.get(key)
        if not isinstance(item, dict):
            raise RuntimeError(f"saved OpenAI config is missing {key}")
        if key == "model" and item.get("value") and is_external_slug(str(item.get("value"))):
            item = {"present": True, "value": "gpt-6.1-sol"}
        updated = _restore_root_value(updated, key, item)

    result: dict[str, Any] = {
        "status": "planned" if not apply else "unchanged",
        "mode": "openai",
        "config": str(actual_config),
        "backup": None,
        "restart_required": updated != current,
    }
    if apply and updated != current:
        result["backup"] = str(bridge.backup(actual_config))
        bridge.atomic_write(actual_config, updated, 0o600)
        result["status"] = "applied"

    if apply:
        running = app_running()
        restarted = restart_codex(codex) if restart else False
        if restart:
            result["codex_restarted"] = restarted
        pending_restart = running and not restarted
        effective_mode = "openai" if restarted or not running else "external"
        lifecycle_state = "OPENAI_ACTIVE" if not pending_restart else "OPENAI_RESTORE_PENDING"
        state.update(
            {
                "mode": "openai",
                "desired_mode": "openai",
                "effective_mode": effective_mode,
                "pending_restart": pending_restart,
                "manual_toggle": False,
                "restart_reason": "quota_recovered_restore" if pending_restart else None,
                "lifecycle_state": lifecycle_state,
                "restored_openai_at": iso_now(),
                "switch_back_status": (
                    "restored_and_restarted"
                    if restarted
                    else ("config_restored_restart_pending" if running else "restored")
                ),
            }
        )
        save_state(state_path, state)
        if sys.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            target = f"{domain}/{WATCH_LABEL}"
            subprocess.run(
                ["/bin/launchctl", "bootout", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif is_windows():
            uninstall_windows_watcher()
        if running and not restarted:
            notify(
                "GPT 用量已恢复",
                "已自动切回 GPT 配置；当前 Codex 完成任务后重启一次即可生效。",
            )
        else:
            notify("GPT 用量已恢复", "已自动切回 GPT 模式。")
    return result


def configure_auto_mode(args: argparse.Namespace) -> dict[str, Any]:
    """Plan/enable one monitor, with a fixed user-selected external model."""
    if args.apply and not (sys.platform == "darwin" or is_windows()):
        raise RuntimeError("automatic background installation currently supports macOS and Windows only")
    preview_args = argparse.Namespace(**vars(args))
    preview_args.apply = False
    preview_args.no_watch = True
    preview_args.restart = False
    plan = apply_external_mode(preview_args)
    if plan.get("last_error"):
        raise RuntimeError(f"cannot enable automatic switching without live quota: {plan['last_error']}")
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    config_path = Path(args.config).expanduser().absolute()
    config, error = bridge.load_config(config_path)
    if error:
        raise RuntimeError(error)
    baseline = capture_openai_config(config)
    if state.get("mode") == "external":
        if is_routing_conflict(state.get("external_config"), baseline):
            raise RuntimeError("external routing changed; reconcile before enabling automatic switching")
        baseline = state["openai_config"]
    monitor = {
        "enabled": True,
        "config": str(config_path),
        "profile_config": str(Path(args.profile_config).expanduser().absolute()),
        "catalog": str(Path(args.catalog or (state.get("source_catalog") if state.get("mode") == "external" else None)
                            or config.get("model_catalog_json")
                            or DEFAULT_CATALOG).expanduser().absolute()),
        "external_catalog": str(Path(args.external_catalog).expanduser().absolute()),
        "state_dir": str(Path(args.state_dir).expanduser().absolute()),
        "model": args.model,
        "baseline": baseline,
        "poll_seconds": args.poll_seconds,
        "watch_plist": str(Path(args.watch_plist).expanduser().absolute()),
        "status": "watching",
    }
    result = {
        "status": "planned" if not args.apply else "enabled",
        "model": args.model,
        "poll_seconds": args.poll_seconds,
        "auto_restart": False,
        "watch_plist": str(Path(args.watch_plist).expanduser()),
        "history_scope_warning": plan["history_scope_warning"],
    }
    if args.apply:
        executable = bridge.resolve_codex_cli(args.codex)
        monitor["enabled_at"] = iso_now()
        state["auto_monitor"] = monitor
        save_state(state_path, state)
        try:
            if sys.platform == "darwin":
                install_watcher(
                    script=Path(__file__).resolve(), plist_path=Path(args.watch_plist).expanduser(),
                    state_path=state_path.absolute(), codex=executable, command="watch-auto",
                )
            elif is_windows():
                install_windows_watcher(
                    script=Path(__file__).resolve(),
                    state_path=state_path.absolute(),
                    codex=executable,
                    command="watch-auto",
                )
        except Exception:
            monitor.update(enabled=False, status="installation_failed")
            save_state(state_path, state)
            raise
    return result


def auto_mode_step(args: argparse.Namespace) -> dict[str, Any]:
    """One fresh observation and at most one transition. Never restart apps."""
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    monitor = state.get("auto_monitor")
    if not isinstance(monitor, dict) or monitor.get("enabled") is not True:
        return {"status": "stopped", "reason": "automatic switching is not enabled"}
    mode = state.get("mode")
    config, error = bridge.load_config(Path(monitor["config"]))
    expected = state.get("external_config") if mode == "external" else monitor.get("baseline")
    if error or mode not in ("openai", "external") or is_routing_conflict(config, expected):
        monitor.update(enabled=False, status="routing_conflict")
        save_state(state_path, state)
        notify("Codex 自动切换已停止", "路由配置被修改或无法读取；已保留你的设置，请检查后再启用。")
        return {"status": "stopped", "reason": "routing config changed or unavailable"}

    state, analysis = refresh_state(state_path, args.codex, allow_error=True)
    monitor = state["auto_monitor"]
    transition = None
    if analysis is None:
        monitor["status"] = "query_error"
        sleep_for = 900
    elif mode == "openai" and analysis.get("blocked"):
        entry = argparse.Namespace(
            **{key: monitor[key] for key in (
                "config", "profile_config", "catalog", "external_catalog", "state_dir", "model"
            )},
            state=str(state_path), codex=args.codex, apply=True, no_watch=True, restart=False,
            watch_plist=str(DEFAULT_WATCH_PLIST),
        )
        transition = apply_external_mode(
            entry, quota_result=(state, analysis), expected_routing=monitor["baseline"]
        )
        state = load_state(state_path)
        monitor = state["auto_monitor"]
        monitor["status"] = "external_configured_restart_if_running"
        sleep_for = watcher_sleep_seconds(analysis)
    elif mode == "external" and analysis.get("recovered"):
        if state.get("manual_toggle"):
            monitor["status"] = "manual_toggle_held"
            sleep_for = monitor["poll_seconds"]
        else:
            transition = restore_openai_mode(state_path=state_path, apply=True, restart=False)
            state = load_state(state_path)
            monitor = state["auto_monitor"]
            monitor["status"] = "openai_configured_restart_if_running"
            sleep_for = monitor["poll_seconds"]
    else:
        monitor["status"] = "watching"
        sleep_for = watcher_sleep_seconds(analysis) if mode == "external" else monitor["poll_seconds"]
    if transition:
        monitor["last_transition_at"] = iso_now()
        monitor["last_transition_to"] = transition["mode"]
    monitor["next_check_at"] = iso_from_epoch(time.time() + sleep_for)
    save_state(state_path, state)
    return {
        "status": "switched" if transition else "waiting", "mode": state["mode"],
        "sleep_seconds": sleep_for, "quota": analysis, "transition": transition,
    }


def watch_auto_mode(args: argparse.Namespace) -> dict[str, Any]:
    while True:
        result = auto_mode_step(args)
        if args.once or result["status"] == "stopped":
            return result
        time.sleep(result["sleep_seconds"])


def stop_auto_mode(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    result = {"status": "planned" if not args.apply else "stopped", "mode": state.get("mode")}
    if args.apply:
        monitor = state.get("auto_monitor")
        if isinstance(monitor, dict):
            monitor.update(enabled=False, status="disabled_by_user")
            save_state(state_path, state)
            plist_path = Path(monitor.get("watch_plist") or DEFAULT_WATCH_PLIST)
            try:
                plist = plistlib.loads(plist_path.read_bytes())
                owns_job = plist.get("Label") == WATCH_LABEL and "watch-auto" in plist.get("ProgramArguments", [])
            except (OSError, ValueError, plistlib.InvalidFileException):
                owns_job = False
            if sys.platform == "darwin" and owns_job:
                subprocess.run(
                    ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{WATCH_LABEL}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            elif is_windows():
                uninstall_windows_watcher()
    return result


def watcher_sleep_seconds(analysis: dict[str, Any] | None, checks_after_reset: int = 0) -> int:
    if not analysis:
        return 900
    reset = analysis.get("expected_recovery_at")
    now = time.time()
    if isinstance(reset, (int, float)):
        delta = reset - now
        if delta > 1800:
            return min(900, max(300, int(delta - 900)))
        if delta > 600:
            return 300
        if delta > 120:
            return 120
        if delta > 0:
            return max(30, min(60, int(delta)))
        return 45 if checks_after_reset < 10 else 120
    return 900


def watch_quota(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state).expanduser()
    after_reset_checks = 0

    while True:
        state = load_state(state_path)
        if state.get("mode") != "external":
            return {"status": "stopped", "reason": "not in external mode"}
        if state.get("manual_toggle"):
            return {"status": "stopped", "reason": "manual toggle mode override"}

        state, analysis = refresh_state(state_path, args.codex, allow_error=True)
        if analysis and analysis.get("recovered"):
            restored = restore_openai_mode(
                state_path=state_path,
                apply=True,
                restart=False,  # never interrupt an active external-model turn
            )
            restored["quota"] = analysis
            return restored

        reset = analysis.get("expected_recovery_at") if analysis else None
        if isinstance(reset, (int, float)) and time.time() >= reset:
            after_reset_checks += 1

        sleep_for = watcher_sleep_seconds(analysis, after_reset_checks)
        state["next_check_at"] = iso_from_epoch(time.time() + sleep_for)
        state["switch_back_status"] = "watching"
        save_state(state_path, state)

        if args.once:
            return {
                "status": "waiting",
                "sleep_seconds": sleep_for,
                "quota": analysis,
                "state": str(state_path),
            }
        time.sleep(sleep_for)


def install_watcher(
    *,
    script: Path,
    plist_path: Path,
    state_path: Path,
    codex: str,
    command: str = "watch-quota",
) -> None:
    if sys.platform != "darwin":
        return
    executable = bridge.resolve_codex_cli(codex)
    plist_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    log_dir = bridge.DEFAULT_STATE_DIR
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "Label": WATCH_LABEL,
        "ProgramArguments": [
            python,
            str(script),
            command,
            "--state",
            str(state_path),
            "--codex",
            executable,
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "quota-watch.log"),
        "StandardErrorPath": str(log_dir / "quota-watch.err.log"),
    }
    raw = plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")
    bridge.atomic_write(plist_path, raw, 0o600)

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{WATCH_LABEL}"
    subprocess.run(
        ["/bin/launchctl", "bootout", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    proc = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("launchctl failed to install quota watcher")


def default_windows_watch_startup_path() -> Path:
    startup = bridge.default_windows_startup_dir()
    return startup / "codex-quota-watch.vbs"


def install_windows_watcher(
    *,
    script: Path,
    state_path: Path,
    codex: str,
    command: str = "watch-quota",
) -> None:
    executable = bridge.resolve_codex_cli(codex)
    state_dir = bridge.DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    python = bridge.python_executable()
    cmd_path = state_dir / "run-quota-watch.cmd"
    vbs_path = state_dir / "run-quota-watch-hidden.vbs"

    cmd_content = (
        "@echo off\r\n"
        f'"{python}" "{script}" {command} --state "{state_path}" --codex "{executable}"\r\n'
    )
    bridge.atomic_write(cmd_path, cmd_content, 0o700)
    vbs_content = bridge.windows_hidden_vbs_source(cmd_path)
    bridge.atomic_write(vbs_path, vbs_content, 0o700)

    startup_path = default_windows_watch_startup_path()
    try:
        startup_path.parent.mkdir(parents=True, exist_ok=True)
        startup_content = bridge.windows_hidden_vbs_source(vbs_path)
        bridge.atomic_write(startup_path, startup_content, 0o700)
    except OSError:
        pass


def uninstall_windows_watcher() -> None:
    startup_path = default_windows_watch_startup_path()
    try:
        if startup_path.exists():
            startup_path.unlink()
    except OSError:
        pass


def cmd_quota_status(args: argparse.Namespace) -> None:
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    live = None
    if args.live:
        state, live = refresh_state(state_path, args.codex, allow_error=True)
    quota = live if live is not None else state.get("last_analysis")
    live_failed = args.live and live is None
    stale = quota is not None and bool(state.get("last_error"))
    status = "error" if live_failed else (
        "unavailable" if quota is None else ("stale" if stale else "ok")
    )
    lifecycle_state = state.get("lifecycle_state") or compute_lifecycle_state(state)
    emit(
        {
            "status": status,
            "quota_source": "live" if live is not None else ("cached" if quota else "unavailable"),
            "quota_stale": stale,
            "codex_cli": args.codex,
            "mode": state.get("mode"),
            "desired_mode": state.get("desired_mode") or state.get("mode", "openai"),
            "effective_mode": state.get("effective_mode") or state.get("mode", "openai"),
            "pending_restart": bool(state.get("pending_restart", False)),
            "restart_reason": state.get("restart_reason"),
            "lifecycle_state": lifecycle_state,
            "entered_external_at": state.get("entered_external_at"),
            "resets_at": state.get("resets_at"),
            "resets_at_iso": state.get("resets_at_iso"),
            "last_checked_at": state.get("last_checked_at"),
            "last_successful_check_at": state.get("last_successful_check_at"),
            "switch_back_status": state.get("switch_back_status"),
            "auto_monitor": {key: state.get("auto_monitor", {}).get(key) for key in (
                "enabled", "model", "status", "next_check_at", "last_transition_to"
            )},
            "last_error": state.get("last_error"),
            "quota": quota,
        },
        2 if live_failed else 0,
    )


def cmd_quota_refresh(args: argparse.Namespace) -> None:
    try:
        state, analysis = refresh_state(Path(args.state).expanduser(), args.codex, allow_error=False)
    except Exception as exc:
        emit({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, 2)
    emit({"status": "ok", "quota": analysis, "state": str(Path(args.state).expanduser())})


def cmd_external_mode(args: argparse.Namespace) -> None:
    try:
        if getattr(args, "toggle", False):
            args.toggle = True
            if not getattr(args, "watch", False):
                args.no_watch = True
            config_path = Path(args.config).expanduser()
            config, _ = bridge.load_config(config_path)
            state_path = Path(args.state).expanduser()
            state = load_state(state_path)
            active_provider = config.get("model_provider", "openai") if isinstance(config, dict) else "openai"
            mode = state.get("mode", "openai")
            if active_provider == EXTERNAL_PROVIDER_ID or mode == "external":
                result = restore_openai_mode(
                    state_path=state_path,
                    config_path=config_path,
                    apply=args.apply,
                    restart=args.restart,
                    codex=args.codex,
                )
                result["toggle_action"] = "restored_to_openai"
                emit(result)
        emit(apply_external_mode(args))
    except Exception as exc:
        emit({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, 2)


def cmd_openai_mode(args: argparse.Namespace) -> None:
    try:
        result = restore_openai_mode(
            state_path=Path(args.state).expanduser(),
            config_path=Path(args.config).expanduser(),
            apply=args.apply,
            restart=args.restart,
            codex=args.codex,
        )
        emit(result)
    except Exception as exc:
        emit({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, 2)


def cmd_toggle_mode(args: argparse.Namespace) -> None:
    try:
        args.toggle = True
        if not getattr(args, "watch", False):
            args.no_watch = True
        config_path = Path(args.config).expanduser()
        config, error = bridge.load_config(config_path)
        if error:
            raise RuntimeError(f"Codex config unavailable: {error}")
        state_path = Path(args.state).expanduser()
        state = load_state(state_path)

        active_provider = config.get("model_provider", "openai")
        mode = state.get("mode", "openai")

        if active_provider == EXTERNAL_PROVIDER_ID or mode == "external":
            result = restore_openai_mode(
                state_path=state_path,
                config_path=config_path,
                apply=args.apply,
                restart=args.restart,
                codex=args.codex,
            )
            result["toggle_action"] = "restored_to_openai"
            emit(result)
        else:
            result = apply_external_mode(args)
            result["toggle_action"] = "switched_to_external"
            emit(result)
    except Exception as exc:
        emit({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, 2)


def cmd_watch(args: argparse.Namespace) -> None:
    try:
        emit(watch_quota(args))
    except Exception as exc:
        emit({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, 2)


def cmd_auto(args: argparse.Namespace) -> None:
    try:
        handler = {"auto-mode": configure_auto_mode, "watch-auto": watch_auto_mode, "stop-auto": stop_auto_mode}
        emit(handler[args.command](args))
    except Exception as exc:
        if args.command == "watch-auto":
            state_path = Path(args.state).expanduser()
            state = load_state(state_path)
            monitor = state.get("auto_monitor")
            if isinstance(monitor, dict):
                monitor.update(enabled=False, status="transition_error")
                save_state(state_path, state)
                notify("Codex 自动切换已停止", "切换未能完成；请检查额度与模型配置后重新启用。")
        emit({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, 2)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Codex quota-aware external-model failover")
    sub = root.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=str(DEFAULT_QUOTA_STATE))
    common.add_argument("--codex", default=bridge.default_codex_cli())

    status = sub.add_parser("quota-status", parents=[common])
    status.add_argument("--live", action="store_true")
    status.set_defaults(func=cmd_quota_status)

    refresh = sub.add_parser("quota-refresh", parents=[common])
    refresh.set_defaults(func=cmd_quota_refresh)

    external = sub.add_parser("external-mode", parents=[common])
    external.add_argument("--config", default=str(DEFAULT_CONFIG))
    external.add_argument("--profile-config", default=str(DEFAULT_PROFILE))
    external.add_argument("--catalog", help="Source catalog; defaults to the active/saved OpenAI catalog")
    external.add_argument("--external-catalog", default=str(DEFAULT_EXTERNAL_CATALOG))
    external.add_argument("--state-dir", default=str(bridge.DEFAULT_STATE_DIR))
    external.add_argument("--watch-plist", default=str(DEFAULT_WATCH_PLIST))
    external.add_argument("--model")
    external.add_argument("--apply", action="store_true")
    external.add_argument("--restart", action="store_true")
    external.add_argument("--no-watch", action="store_true")
    external.add_argument("--toggle", action="store_true", help="Toggle between OpenAI and External mode")
    external.set_defaults(func=cmd_external_mode)

    openai = sub.add_parser("openai-mode", parents=[common])
    openai.add_argument("--config", default=str(DEFAULT_CONFIG))
    openai.add_argument("--apply", action="store_true")
    openai.add_argument("--restart", action="store_true")
    openai.set_defaults(func=cmd_openai_mode)

    for toggle_name in ("toggle", "toggle-mode"):
        toggle_parser = sub.add_parser(toggle_name, parents=[common])
        toggle_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
        toggle_parser.add_argument("--profile-config", default=str(DEFAULT_PROFILE))
        toggle_parser.add_argument("--catalog", help="Source catalog; defaults to the active/saved OpenAI catalog")
        toggle_parser.add_argument("--external-catalog", default=str(DEFAULT_EXTERNAL_CATALOG))
        toggle_parser.add_argument("--state-dir", default=str(bridge.DEFAULT_STATE_DIR))
        toggle_parser.add_argument("--watch-plist", default=str(DEFAULT_WATCH_PLIST))
        toggle_parser.add_argument("--model")
        toggle_parser.add_argument("--apply", action="store_true")
        toggle_parser.add_argument("--restart", action="store_true")
        toggle_parser.add_argument("--no-watch", action="store_true")
        toggle_parser.add_argument("--watch", action="store_true")
        toggle_parser.set_defaults(func=cmd_toggle_mode, toggle=True)

    watch = sub.add_parser("watch-quota", parents=[common])
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    auto = sub.add_parser("auto-mode", parents=[common])
    auto.add_argument("--model", required=True, help="Explicitly approved, verified external fallback model")
    auto.add_argument("--config", default=str(DEFAULT_CONFIG))
    auto.add_argument("--profile-config", default=str(DEFAULT_PROFILE))
    auto.add_argument("--catalog")
    auto.add_argument("--external-catalog", default=str(DEFAULT_EXTERNAL_CATALOG))
    auto.add_argument("--state-dir", default=str(bridge.DEFAULT_STATE_DIR))
    auto.add_argument("--watch-plist", default=str(DEFAULT_WATCH_PLIST))
    auto.add_argument("--poll-seconds", type=int, choices=range(60, 901), default=120, metavar="60..900")
    auto.add_argument("--apply", action="store_true")
    auto.set_defaults(func=cmd_auto)

    auto_watch = sub.add_parser("watch-auto", parents=[common])
    auto_watch.add_argument("--once", action="store_true")
    auto_watch.set_defaults(func=cmd_auto)

    auto_stop = sub.add_parser("stop-auto", parents=[common])
    auto_stop.add_argument("--apply", action="store_true")
    auto_stop.set_defaults(func=cmd_auto)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
