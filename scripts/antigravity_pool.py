#!/usr/bin/env python3
"""Manage a local CLIProxyAPI Antigravity credential pool.

This helper intentionally never prints credential JSON or token values. It only
manages routing policy and redacted account metadata; OAuth itself remains in
CLIProxyAPI's official login flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


MARKER_BEGIN = "# BEGIN codex-autoheal-bridge antigravity-pool"
MARKER_END = "# END codex-autoheal-bridge antigravity-pool"


def default_config() -> Path:
    return Path(os.environ.get("CLIPROXYAPI_CONFIG", "~/.cli-proxy-api/config.yaml")).expanduser()


def config_auth_dir(config_path: Path) -> Path:
    if not config_path.exists():
        return Path("~/.cli-proxy-api").expanduser()
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*auth-dir:\s*[\"']?([^\"'\n#]+)", text)
    if not match:
        return Path("~/.cli-proxy-api").expanduser()
    value = match.group(1).strip()
    return Path(os.path.expandvars(value)).expanduser()


def redacted_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"account-{digest}"


def load_antigravity_files(auth_dir: Path) -> list[dict]:
    records: list[dict] = []
    if not auth_dir.exists():
        return records
    for path in sorted(auth_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        provider = str(data.get("type", data.get("provider", ""))).strip().lower()
        if provider != "antigravity":
            continue
        email = str(data.get("email", data.get("metadata", {}).get("email", ""))).strip()
        priority = data.get("priority", 0)
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 0
        records.append(
            {
                "path": path,
                "id": redacted_id(email or path.name),
                "file_id": redacted_id(path.name),
                "email": email,
                "email_present": bool(email),
                "priority": priority,
                "disabled": bool(data.get("disabled", False)),
                "project_present": bool(data.get("project_id", data.get("metadata", {}).get("project_id", ""))),
            }
        )
    return records


def contains_validation_required(value: object) -> bool:
    """Recognize Google's validation error without exposing its opaque URL."""
    if isinstance(value, str):
        if "VALIDATION_REQUIRED" in value.upper():
            return True
        try:
            return contains_validation_required(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return False
    if isinstance(value, dict):
        return any(contains_validation_required(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_validation_required(item) for item in value)
    return False


def validation_targets(auth_dir: Path, model: str | None = None) -> list[dict]:
    """Return accounts explicitly marked VALIDATION_REQUIRED in cooldown files."""
    accounts = load_antigravity_files(auth_dir)
    by_filename = {record["path"].name: record for record in accounts}
    by_email = {record["email"].casefold(): record for record in accounts if record["email"]}
    targets: dict[str, dict] = {}

    for status_path in sorted(auth_dir.glob("*.cds")) if auth_dir.exists() else []:
        try:
            status_data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(status_data, dict) or str(status_data.get("provider", "")).casefold() != "antigravity":
            continue

        for item in status_data.get("records", []):
            if not isinstance(item, dict):
                continue
            item_model = str(item.get("model", "")).strip()
            if model and item_model and item_model.casefold() != model.casefold():
                continue
            if not contains_validation_required((item.get("reason"), item.get("last_error"))):
                continue

            auth_id = str(item.get("auth_id", "")).strip()
            account = by_filename.get(auth_id)
            if account is None and auth_id.casefold() in by_email:
                account = by_email[auth_id.casefold()]
            if account is None:
                # CLIProxyAPI normally stores auth_id as the credential filename.
                account = by_filename.get(Path(auth_id).name)
            key = account["email"].casefold() if account and account["email"] else auth_id
            target = targets.setdefault(
                key,
                {
                    "email": account["email"] if account else "",
                    "account_id": account["id"] if account else redacted_id(auth_id or status_path.name),
                    "credential_path": account["path"] if account else None,
                    "disabled": account["disabled"] if account else None,
                    "models": set(),
                    "auth_id_present": bool(auth_id),
                },
            )
            target["models"].add(item_model or "all models")

    return sorted(targets.values(), key=lambda item: (not bool(item["email"]), item["email"].casefold() or item["account_id"]))


def print_validation_plan(targets: list[dict]) -> None:
    print(f"validation_required_accounts={len(targets)}")
    for index, target in enumerate(targets, 1):
        identity = target["email"] or target["account_id"]
        models = ",".join(sorted(target["models"]))
        disabled = "unknown" if target["disabled"] is None else str(target["disabled"]).lower()
        print(f"{index}. account={identity} models={models} disabled={disabled}")


def parse_iso_datetime(dt_str: str) -> datetime | None:
    if not dt_str or not isinstance(dt_str, str):
        return None
    clean = re.sub(r"(\.\d{6})\d+", r"\1", dt_str.strip())
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def fetch_account_tier(access_token: str, timeout: float = 2.0) -> str | None:
    if not access_token:
        return None
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity/1.0.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tier_id = data.get("currentTier", {}).get("id")
            if tier_id:
                return str(tier_id).strip()
    except Exception:
        pass
    return None


def calculate_account_priority(
    account: dict,
    status_records: list[dict],
    now: datetime,
    model: str | None = None,
    reset_hour_utc: int = 0,
    critical_window_hours: float = 2.0,
) -> dict:
    email = account.get("email", "")
    account_id = account.get("id", "")
    path = account.get("path")
    curr_priority = account.get("priority", 0)

    is_disabled = bool(account.get("disabled", False))
    has_val_req = any(
        contains_validation_required((r.get("reason"), r.get("last_error")))
        for r in status_records
        if (not model or not r.get("model") or r.get("model").casefold() == model.casefold())
    )
    if is_disabled or has_val_req:
        return {
            "account": email or account_id,
            "path": path,
            "tier": account.get("tier", "unknown"),
            "status": "BLOCKED",
            "current_priority": curr_priority,
            "calculated_priority": 0,
            "time_to_reset_sec": None,
            "reset_str": "-",
            "reason": "disabled" if is_disabled else "validation_required",
        }

    cooling_until: datetime | None = None
    cooling_reason: str = ""
    for r in status_records:
        r_model = str(r.get("model", "")).strip()
        if model and r_model and r_model.casefold() != model.casefold():
            continue
        if str(r.get("status", "")).casefold() == "cooling":
            rec_time = parse_iso_datetime(r.get("next_retry_after") or r.get("quota", {}).get("next_recover_at"))
            if not rec_time:
                last_err = r.get("last_error", {})
                if isinstance(last_err, dict) and "message" in last_err:
                    try:
                        err_j = json.loads(last_err["message"])
                        for dt in err_j.get("error", {}).get("details", []):
                            ts_str = dt.get("metadata", {}).get("quotaResetTimeStamp")
                            if ts_str:
                                rec_time = parse_iso_datetime(ts_str)
                                break
                    except Exception:
                        pass
            if rec_time and rec_time > now:
                if cooling_until is None or rec_time > cooling_until:
                    cooling_until = rec_time
                    cooling_reason = str(r.get("reason", "quota"))

    if cooling_until and cooling_until > now:
        recover_in_sec = (cooling_until - now).total_seconds()
        recover_in_hours = recover_in_sec / 3600.0
        cooling_prio = max(10, int(150 - min(100, recover_in_hours)))
        return {
            "account": email or account_id,
            "path": path,
            "tier": account.get("tier", "standard-tier"),
            "status": "COOLING",
            "current_priority": curr_priority,
            "calculated_priority": cooling_prio,
            "time_to_reset_sec": recover_in_sec,
            "reset_str": f"cools {recover_in_hours:.1f}h",
            "reason": f"cooling ({cooling_reason})",
        }

    tier = account.get("tier") or "standard-tier"
    tier_weight = 150 if "standard" in str(tier).lower() or "pro" in str(tier).lower() else 40

    raw_data = account.get("data", {})
    snapshot = account.get("snapshot", {})
    snap_reset_sec = snapshot.get("gemini5hResetSeconds")
    custom_reset = parse_iso_datetime(account.get("reset_timestamp") or raw_data.get("reset_timestamp"))
    if not custom_reset:
        for r in status_records:
            last_err = r.get("last_error", {})
            if isinstance(last_err, dict) and "message" in last_err:
                try:
                    err_j = json.loads(last_err["message"])
                    for dt in err_j.get("error", {}).get("details", []):
                        ts_str = dt.get("metadata", {}).get("quotaResetTimeStamp")
                        if ts_str:
                            custom_reset = parse_iso_datetime(ts_str)
                            break
                except Exception:
                    pass
    if isinstance(snap_reset_sec, (int, float)) and snap_reset_sec > 0:
        time_to_reset_sec = float(snap_reset_sec)
    elif custom_reset and custom_reset > now:
        time_to_reset_sec = (custom_reset - now).total_seconds()
    else:
        acct_reset_hour = account.get("reset_hour") or raw_data.get("reset_hour", reset_hour_utc)
        try:
            acct_reset_hour = int(acct_reset_hour) % 24
        except (ValueError, TypeError):
            acct_reset_hour = reset_hour_utc
        anchor = now.replace(hour=acct_reset_hour, minute=0, second=0, microsecond=0)
        if anchor <= now:
            anchor += timedelta(days=1)
        time_to_reset_sec = (anchor - now).total_seconds()
    hours_to_reset = time_to_reset_sec / 3600.0

    if hours_to_reset <= critical_window_hours:
        urgency = (critical_window_hours - hours_to_reset) / critical_window_hours
        prio = int(800 + (urgency * 120) + (tier_weight / 3.0))
        status = "IMMINENT_RESET"
        reset_str = f"in {hours_to_reset*60:.0f}m"
        reason = "imminent reset (drain quota)"
    else:
        time_factor = int(max(0, (24.0 - hours_to_reset) * 3))
        prio = int(350 + tier_weight + time_factor)
        status = "ACTIVE"
        reset_str = f"in {hours_to_reset:.1f}h"
        reason = f"active ({tier})"

    return {
        "account": email or account_id,
        "path": path,
        "tier": tier,
        "status": status,
        "current_priority": curr_priority,
        "calculated_priority": prio,
        "time_to_reset_sec": time_to_reset_sec,
        "reset_str": reset_str,
        "reason": reason,
    }


def rebalance_antigravity_pool(
    auth_dir: Path,
    model: str | None = None,
    now: datetime | None = None,
    reset_hour_utc: int = 0,
    critical_window_hours: float = 2.0,
    refresh_tier: bool = False,
) -> list[dict]:
    if now is None:
        now = datetime.now(timezone.utc)

    accounts = load_antigravity_files(auth_dir)
    evaluated = []

    snapshot_map = {}
    snap_file = auth_dir / "quota-snapshot.json"
    if snap_file.exists():
        try:
            snap_json = json.loads(snap_file.read_text(encoding="utf-8"))
            if isinstance(snap_json, dict) and isinstance(snap_json.get("accounts"), dict):
                for em, it in snap_json["accounts"].items():
                    snapshot_map[str(em).strip().casefold()] = it
        except Exception:
            pass

    for acc in accounts:
        path = acc["path"]
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_data = {}

        stem = path.stem.replace("@", "_")
        cds_path = auth_dir / f"{stem}.cds"
        status_data = {}
        if cds_path.exists():
            try:
                status_data = json.loads(cds_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        records = status_data.get("records", []) if isinstance(status_data, dict) else []

        tier = raw_data.get("tier")
        snap = snapshot_map.get(acc.get("email", "").casefold(), {})
        if snap.get("tier"):
            tier = snap["tier"]
        if not tier or refresh_tier:
            fetched = fetch_account_tier(raw_data.get("access_token"))
            if fetched:
                tier = fetched
            elif not tier:
                tier = "standard-tier"

        acc_info = dict(acc)
        acc_info["data"] = raw_data
        acc_info["tier"] = tier
        acc_info["snapshot"] = snap

        item = calculate_account_priority(
            account=acc_info,
            status_records=records,
            now=now,
            model=model,
            reset_hour_utc=reset_hour_utc,
            critical_window_hours=critical_window_hours,
        )
        evaluated.append(item)

    def sort_key(item: dict):
        status_rank = {"IMMINENT_RESET": 4, "ACTIVE": 3, "COOLING": 2, "BLOCKED": 1}.get(item["status"], 0)
        return (status_rank, item["calculated_priority"])

    evaluated.sort(key=sort_key, reverse=True)

    imminent_count = 0
    active_count = 0
    cooling_count = 0

    for item in evaluated:
        st = item["status"]
        if st == "IMMINENT_RESET":
            item["target_priority"] = max(700, 900 - (imminent_count * 30))
            imminent_count += 1
        elif st == "ACTIVE":
            tier_bonus = 50 if "standard" in str(item["tier"]).lower() or "pro" in str(item["tier"]).lower() else 0
            item["target_priority"] = max(300, 600 + tier_bonus - (active_count * 50))
            active_count += 1
        elif st == "COOLING":
            item["target_priority"] = max(20, 200 - (cooling_count * 20))
            cooling_count += 1
        else:
            item["target_priority"] = 0

    return evaluated


def cmd_rebalance(args: argparse.Namespace) -> int:
    config = args.config.expanduser()
    auth_dir = config_auth_dir(config)
    evaluated = rebalance_antigravity_pool(
        auth_dir=auth_dir,
        model=args.model,
        reset_hour_utc=args.reset_hour,
        critical_window_hours=args.critical_window,
        refresh_tier=args.refresh_tier,
    )

    if getattr(args, "json", False):
        serializable = []
        for item in evaluated:
            c = dict(item)
            c["path"] = str(c["path"]) if c.get("path") else None
            serializable.append(c)
        print(json.dumps({"auth_dir": str(auth_dir), "accounts": serializable}, indent=2))
        return 0

    print(f"auth_dir={auth_dir}")
    print(f"{'Account':<32} {'Status':<16} {'Tier':<14} {'Reset':<12} {'CurPrio':<8} {'NewPrio':<8} {'Reason'}")
    print("-" * 110)
    changed_count = 0
    for item in evaluated:
        acct = item["account"]
        status = item["status"]
        tier = item["tier"]
        reset_s = item["reset_str"]
        cur_p = item["current_priority"]
        new_p = item["target_priority"]
        reason = item["reason"]
        if cur_p != new_p:
            changed_count += 1
        print(f"{acct:<32} {status:<16} {tier:<14} {reset_s:<12} {cur_p:<8} {new_p:<8} {reason}")

    if not args.apply:
        print("\npreview only; run with --apply to update credential priority files")
        return 0

    updated_count = 0
    for item in evaluated:
        cur_p = item["current_priority"]
        new_p = item["target_priority"]
        tier = item["tier"]
        path = item.get("path")
        if not path or not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if cur_p == new_p and data.get("tier") == tier:
            continue

        saved = backup(path)
        data["priority"] = new_p
        if tier:
            data["tier"] = tier
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
        print(f"backup={saved}")
        print(f"updated={path.name} priority={new_p} tier={tier}")
        updated_count += 1

    print(f"\nrebalance complete: {updated_count} account file(s) updated")

    # Export pool-status.json for usage header plugin & UI integration
    status_file = auth_dir / "pool-status.json"
    primary_acct = evaluated[0]["account"] if evaluated else None
    status_payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "auto",
        "primaryAccount": primary_acct,
        "rankings": [
            {
                "email": item["account"],
                "priority": item["target_priority"],
                "status": item["status"],
                "tier": item["tier"],
                "reset": item["reset_str"],
                "reason": item["reason"],
            }
            for item in evaluated
        ],
    }
    try:
        tmp_status = status_file.with_name(f".{status_file.name}.tmp")
        tmp_status.write_text(json.dumps(status_payload, indent=2) + "\n", encoding="utf-8")
        tmp_status.chmod(0o600)
        os.replace(tmp_status, status_file)
        print(f"status_exported={status_file.name}")
    except Exception as exc:
        print(f"warning: could not write pool-status.json: {exc}", file=sys.stderr)

    return 0


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    target.chmod(0o600)
    return target


def replace_or_append_scalar(lines: list[str], key: str, value: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*.*$")
    for index, line in enumerate(lines):
        if pattern.match(line) and not line.startswith(" ") and not line.startswith("\t"):
            lines[index] = f"{key}: {value}\n"
            return lines
    insert_at = 0
    while insert_at < len(lines) and (lines[insert_at].startswith("#") or not lines[insert_at].strip()):
        insert_at += 1
    lines[insert_at:insert_at] = [f"{key}: {value}\n"]
    return lines


def patch_routing_block(text: str) -> str:
    block = (
        f"{MARKER_BEGIN}\n"
        "request-retry: 0\n"
        "max-retry-credentials: 0\n"
        "disable-cooling: false\n"
        "save-cooldown-status: true\n"
        "routing:\n"
        '  strategy: "fill-first"\n'
        "  session-affinity: true\n"
        '  session-affinity-ttl: "1h"\n'
        "  session-affinity-subagents: true\n"
        f"{MARKER_END}\n"
    )
    marker_pattern = re.compile(
        rf"(?ms)^{re.escape(MARKER_BEGIN)}\n.*?^{re.escape(MARKER_END)}\n?"
    )
    if marker_pattern.search(text):
        return marker_pattern.sub(block, text, count=1)

    # Remove existing top-level settings that this policy owns. This avoids
    # duplicate YAML keys while preserving all unrelated user configuration.
    lines = text.splitlines(keepends=True)
    owned = {"request-retry", "max-retry-credentials", "disable-cooling", "save-cooldown-status"}
    filtered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^([A-Za-z0-9_-]+):", line)
        if match and match.group(1) in owned:
            index += 1
            continue
        if re.match(r"^routing:\s*$", line):
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith(" ") or lines[index].startswith("\t") or lines[index].lstrip().startswith("#")):
                index += 1
            continue
        filtered.append(line)
        index += 1
    prefix = "".join(filtered)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "\n" + block


def cmd_audit(args: argparse.Namespace) -> int:
    config = args.config.expanduser()
    auth_dir = config_auth_dir(config)
    records = load_antigravity_files(auth_dir)
    print(f"config={config}")
    print(f"auth_dir={auth_dir}")
    print(f"antigravity_accounts={len(records)}")
    for record in records:
        print(
            f"{record['id']} file={record['file_id']} priority={record['priority']} "
            f"disabled={str(record['disabled']).lower()} email_present={str(record['email_present']).lower()} "
            f"project_present={str(record['project_present']).lower()}"
        )
    return 0 if records else 2


def cmd_configure(args: argparse.Namespace) -> int:
    config = args.config.expanduser()
    if not config.exists():
        print(f"error: config not found: {config}", file=sys.stderr)
        return 2
    current = config.read_text(encoding="utf-8")
    updated = patch_routing_block(current)
    if updated == current:
        print("antigravity pool policy is already configured")
        return 0
    print("planned policy: fill-first, cooldown persistence, session affinity, one retry round")
    if not args.apply:
        print("preview only; rerun with --apply")
        return 0
    saved = backup(config)
    tmp = config.with_name(f".{config.name}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, config)
    print(f"backup={saved}")
    print(f"updated={config}")
    return 0


def cmd_set_priority(args: argparse.Namespace) -> int:
    path = args.file.expanduser()
    if not path.exists():
        print(f"error: credential file not found: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: invalid credential JSON: {exc}", file=sys.stderr)
        return 2
    provider = str(data.get("type", data.get("provider", ""))).lower()
    if provider != "antigravity":
        print("error: target is not an Antigravity credential", file=sys.stderr)
        return 2
    try:
        priority = int(args.priority)
    except ValueError:
        print("error: priority must be an integer", file=sys.stderr)
        return 2
    if not args.apply:
        print(f"planned priority={priority} file={path.name}")
        return 0
    saved = backup(path)
    data["priority"] = priority
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    print(f"backup={saved}")
    print(f"updated={path.name} priority={priority}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    binary = args.binary or shutil.which("cli-proxy-api")
    if not binary:
        print("error: cli-proxy-api not found in PATH", file=sys.stderr)
        return 2
    command = [binary, "-config", str(args.config.expanduser()), "-antigravity-login"]
    print("starting official Antigravity OAuth flow; choose the second Google account in the browser")
    return subprocess.call(command)


def cmd_validation_plan(args: argparse.Namespace) -> int:
    auth_dir = config_auth_dir(args.config.expanduser())
    targets = validation_targets(auth_dir, args.model)
    print(f"auth_dir={auth_dir}")
    print_validation_plan(targets)
    if any(not item["email"] for item in targets):
        print("warning: one or more target accounts could not be mapped to an email; do not start login until resolved")
        return 2
    return 0


def find_account_by_email(auth_dir: Path, email: str) -> dict | None:
    wanted = email.casefold()
    for record in load_antigravity_files(auth_dir):
        if record["email"].casefold() == wanted:
            return record
    return None


def preserve_auth_policy(path: Path, original_data: dict) -> bool:
    """Preserve local routing metadata if the upstream login rewrites it."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    changed = False
    for key in ("priority", "disabled"):
        if key in original_data:
            if current.get(key) != original_data[key]:
                current[key] = original_data[key]
                changed = True
        elif key in current:
            current.pop(key)
            changed = True
    if changed:
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.chmod(0o600)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return True


def cmd_login_validation(args: argparse.Namespace) -> int:
    config = args.config.expanduser()
    auth_dir = config_auth_dir(config)
    targets = validation_targets(auth_dir, args.model)
    target_by_email = {item["email"].casefold(): item for item in targets if item["email"]}
    requested = [email.strip() for email in (args.account or []) if email.strip()]
    if not requested:
        print("error: specify the exact target account(s) with --account after reviewing validation-plan", file=sys.stderr)
        print_validation_plan(targets)
        return 2
    if len({email.casefold() for email in requested}) != len(requested):
        print("error: duplicate --account values", file=sys.stderr)
        return 2
    unexpected = [email for email in requested if email.casefold() not in target_by_email]
    if unexpected:
        print("error: requested account is not currently marked VALIDATION_REQUIRED; no login started", file=sys.stderr)
        print_validation_plan(targets)
        return 2
    if any(target_by_email[email.casefold()]["disabled"] for email in requested):
        print("error: a requested account is disabled; no login started", file=sys.stderr)
        return 2

    print("accounts_to_login_in_order:")
    for index, email in enumerate(requested, 1):
        print(f"{index}. {email}")
    if not args.apply:
        print("preview only; rerun with --apply after authorizing this exact account list")
        return 0

    binary = args.binary or shutil.which("cli-proxy-api")
    if not binary:
        print("error: cli-proxy-api not found in PATH", file=sys.stderr)
        return 2

    for index, expected_email in enumerate(requested, 1):
        before = find_account_by_email(auth_dir, expected_email)
        if before is None:
            print(f"error: target credential disappeared before login: {expected_email}; stopping", file=sys.stderr)
            return 2
        before_mtime = before["path"].stat().st_mtime_ns
        try:
            before_data = json.loads(before["path"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"error: could not safely inspect routing metadata for {expected_email}; stopping", file=sys.stderr)
            return 2
        print(
            f"starting login {index}/{len(requested)} for {expected_email}; "
            "select this exact Google account in the browser",
            flush=True,
        )
        command = [binary, "-config", str(config), "-antigravity-login"]
        try:
            result = subprocess.run(
                command,
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                check=False,
            )
        except OSError as exc:
            print(f"error: could not start CLIProxyAPI login ({type(exc).__name__}); stopping", file=sys.stderr)
            return 2

        authenticated = re.findall(r"(?im)^Authenticated as\s+([^\s]+)", result.stdout or "")
        reported_email = authenticated[-1].strip().casefold() if authenticated else ""
        updated = find_account_by_email(auth_dir, expected_email)
        if reported_email and reported_email != expected_email.casefold():
            print(f"error: Google authenticated a different account than requested ({authenticated[-1]}); stopping", file=sys.stderr)
            return 3
        if result.returncode != 0 or updated is None or updated["path"].stat().st_mtime_ns <= before_mtime:
            print(f"error: OAuth update for {expected_email} was not verified (exit={result.returncode}); stopping before next account", file=sys.stderr)
            return 3
        if not reported_email:
            # The on-disk Antigravity credential must independently identify the exact account.
            try:
                saved = json.loads(updated["path"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
            saved_email = str(saved.get("email", "")).casefold()
            if saved_email != expected_email.casefold():
                print(f"error: saved credential does not match {expected_email}; stopping", file=sys.stderr)
                return 3
        if not preserve_auth_policy(updated["path"], before_data):
            print(f"error: could not preserve routing metadata for {expected_email}; stopping", file=sys.stderr)
            return 3
        still_blocked = any(
            item["email"].casefold() == expected_email.casefold()
            for item in validation_targets(auth_dir, args.model)
        )
        if still_blocked:
            print(f"error: {expected_email} is still marked VALIDATION_REQUIRED; stopping before next account", file=sys.stderr)
            return 3
        print(f"authentication updated and account verified: {expected_email}", flush=True)

    print("all requested account logins completed in order")
    return 0


def extract_validation_url(record: dict) -> str | None:
    """从单条冷却记录中提取 Google 账号验证链接（不回显，仅用于打开浏览器）。"""
    import urllib.parse

    blob = json.dumps(record, ensure_ascii=False)
    if "VALIDATION_REQUIRED" not in blob.upper():
        return None
    match = re.search(r"https://accounts\.google\.com/signin/continue[^\"\\\s]+", blob)
    if not match:
        return None
    # JSON 转义还原（\u003d、\u0026 等）
    url = match.group(0)
    url = url.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\", "")
    return urllib.parse.unquote(url)


def collect_validation_fix_targets(auth_dir: Path, model: str | None = None) -> list[dict]:
    """汇总所有被 VALIDATION_REQUIRED 标记且有验证链接的账号（按 priority 升序）。"""
    accounts = {rec["path"].name: rec for rec in load_antigravity_files(auth_dir)}
    targets: list[dict] = []
    for cds_path in sorted(auth_dir.glob("*.cds")):
        try:
            data = json.loads(cds_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("provider", "")).casefold() != "antigravity":
            continue
        url = None
        model_hit = False
        for record in data.get("records", []):
            if not isinstance(record, dict):
                continue
            item_model = str(record.get("model", "")).strip()
            if model and item_model and item_model.casefold() != model.casefold():
                continue
            found = extract_validation_url(record)
            if found:
                url = found
                model_hit = True
        if not url:
            continue
        cred = accounts.get(data.get("auth_id", ""), None)
        email = (cred or {}).get("email", "")
        targets.append(
            {
                "email": email,
                "cds_path": cds_path,
                "url": url,
                "priority": (cred or {}).get("priority", 0),
                "disabled": bool((cred or {}).get("disabled", False)),
                "model": item_model if model_hit else "all models",
            }
        )
    return sorted(targets, key=lambda t: (t["priority"], t["email"].casefold()))


def find_launchd_cliproxy_label() -> str | None:
    """从 launchctl 列表中探测 CLIProxyAPI 的 LaunchAgent label。"""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "cliproxyapi" in parts[-1].casefold():
            return parts[-1]
    return None


def probe_model_via_proxy(config_path: Path, model: str, timeout: float = 60.0) -> tuple[bool, str]:
    """用 config.yaml 里的首个 api-key 走本机代理探测模型是否恢复（不回显密钥）。"""
    import urllib.error
    import urllib.request

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as err:
        return False, f"无法读取配置文件: {err}"
    match = re.search(r"(?ms)api-keys:.*?(sk-[A-Za-z0-9_-]+)", text)
    if not match:
        return False, "配置文件中未找到 api-key"
    key = match.group(1)
    port_match = re.search(r"(?m)^\s*port:\s*(\d+)", text)
    port = port_match.group(1) if port_match else "8317"
    body = json.dumps(
        {
            "model": model,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "reply with ok"}]}
            ],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as err:
        payload = err.read().decode("utf-8", "replace")
        status = err.code
    except Exception as err:  # 网络层失败
        return False, f"请求失败: {err}"
    if status == 200 and '"error":null' in payload.replace(" ", ""):
        return True, "上游返回 completed"
    if "VALIDATION_REQUIRED" in payload.upper():
        return False, "仍被 Google 拦截 VALIDATION_REQUIRED（可能验证未生效或冷却未清除）"
    if "RESOURCE_EXHAUSTED" in payload.upper() or "429" in payload[:200]:
        return False, "账号验证已通过，但上游配额耗尽（429），需等待配额重置"
    snippet = re.sub(r"\s+", " ", payload)[:160]
    return False, f"HTTP {status}: {snippet}"


def cmd_validation_fix(args: argparse.Namespace) -> int:
    """处理 VALIDATION_REQUIRED 的完整流程：打开专属验证链接 → 清冷却 → 重启代理 → 探测。

    2026-09-26 实测结论：CLIProxyAPI 的 OAuth 重新登录（-antigravity-login）无法解除
    Google 的账号验证拦截，必须在浏览器打开 403 响应携带的 accounts.google.com/signin/continue
    专属链接完成验证；随后删除 .cds 冷却文件并重启代理（冷却状态在内存中，仅删文件无效）。
    """
    config = args.config.expanduser()
    auth_dir = config_auth_dir(config)
    targets = collect_validation_fix_targets(auth_dir, args.model)
    if args.account:
        wanted = {email.strip().casefold() for email in args.account if email.strip()}
        targets = [t for t in targets if t["email"].casefold() in wanted]
        missing = wanted - {t["email"].casefold() for t in targets}
        if missing:
            print(f"error: 以下账号没有待处理的验证链接: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not targets:
        print("no VALIDATION_REQUIRED targets with a validation_url; nothing to fix")
        return 0
    print(f"validation_fix_targets={len(targets)}")
    for index, target in enumerate(targets, 1):
        print(f"{index}. account={target['email'] or target['cds_path'].stem} models={target['model']}")

    if args.print_url:
        for target in targets:
            print(f"url[{target['email'] or target['cds_path'].stem}]={target['url']}")
        return 0
    if not args.apply:
        print("preview only; rerun with --apply to open verification pages and clear cooldown files")
        return 0

    import webbrowser

    # 逐账号打开验证页，等待用户在浏览器完成后确认
    for index, target in enumerate(targets, 1):
        identity = target["email"] or target["cds_path"].stem
        print(f"[{index}/{len(targets)}] 正在打开 {identity} 的验证页面；请在浏览器中选择该账号并完成验证……", flush=True)
        webbrowser.open(target["url"])
        try:
            input(f"完成后按回车继续（账号 {identity}）……")
        except (KeyboardInterrupt, EOFError):
            print("已中止；未删除任何冷却文件", file=sys.stderr)
            return 3

    # 备份并删除冷却文件（内存冷却状态需重启代理才会清空）
    backup_dir = auth_dir / f"cds-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for target in targets:
        backup_path = backup_dir / target["cds_path"].name
        shutil.copy2(target["cds_path"], backup_path)
        target["cds_path"].unlink()
        print(f"cooldown cleared: {target['cds_path'].name} (backup: {backup_path})")

    # 重启 CLIProxyAPI 以清空内存冷却状态
    if args.restart:
        label = args.launchd_label or find_launchd_cliproxy_label()
        if label:
            try:
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                    check=True,
                    timeout=15,
                )
                print(f"CLIProxyAPI restarted via launchd: {label}")
            except (OSError, subprocess.SubprocessError) as err:
                print(f"warning: launchd 重启失败（{err}）；请手动重启 CLIProxyAPI 后重新探测", file=sys.stderr)
        else:
            print("warning: 未探测到 launchd 服务；请手动重启 CLIProxyAPI 后重新探测", file=sys.stderr)

    # 探测验证恢复情况
    model = args.model or "gemini-3.8-flash-high"
    print(f"probing model={model} via local proxy ……")
    ok, detail = probe_model_via_proxy(config, model)
    print(("PROBE OK: " if ok else "PROBE FAILED: ") + detail)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity multi-account pool helper")
    parser.add_argument("--config", type=Path, default=default_config())
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit")
    audit.set_defaults(func=cmd_audit)

    configure = sub.add_parser("configure")
    configure.add_argument("--apply", action="store_true")
    configure.set_defaults(func=cmd_configure)

    priority = sub.add_parser("set-priority")
    priority.add_argument("--file", required=True, type=Path)
    priority.add_argument("--priority", required=True)
    priority.add_argument("--apply", action="store_true")
    priority.set_defaults(func=cmd_set_priority)

    login = sub.add_parser("login")
    login.add_argument("--binary")
    login.set_defaults(func=cmd_login)

    validation_plan = sub.add_parser("validation-plan")
    validation_plan.add_argument("--model", help="only list validation blocks matching this model")
    validation_plan.set_defaults(func=cmd_validation_plan)

    login_validation = sub.add_parser("login-validation")
    login_validation.add_argument("--model", help="only process validation blocks matching this model")
    login_validation.add_argument("--account", action="append", help="exact email to log in; repeat in desired order")
    login_validation.add_argument("--binary")
    login_validation.add_argument("--apply", action="store_true")
    login_validation.set_defaults(func=cmd_login_validation)

    # VALIDATION_REQUIRED 账号验证自愈：打开 Google 专属验证链接 → 清冷却 → 重启代理 → 探测
    validation_fix = sub.add_parser("validation-fix")
    validation_fix.add_argument("--model", help="only process validation blocks matching this model")
    validation_fix.add_argument("--account", action="append", help="exact email to fix; repeat in desired order")
    validation_fix.add_argument("--launchd-label", help="launchd service label for CLIProxyAPI restart")
    validation_fix.add_argument("--print-url", action="store_true", help="print validation URLs instead of opening a browser (headless use)")
    validation_fix.add_argument("--restart", action="store_true", help="restart CLIProxyAPI via launchd after clearing cooldown files")
    validation_fix.add_argument("--apply", action="store_true", help="open verification pages and clear cooldown files")
    validation_fix.set_defaults(func=cmd_validation_fix)

    rebalance = sub.add_parser("rebalance")
    rebalance.add_argument("--model", help="model filter for status and reset calculations")
    rebalance.add_argument("--apply", action="store_true", help="write priority changes to credential files")
    rebalance.add_argument("--refresh-tier", action="store_true", help="force re-fetching tier from Google API")
    rebalance.add_argument("--critical-window", type=float, default=2.0, help="imminent reset threshold in hours (default: 2.0)")
    rebalance.add_argument("--reset-hour", type=int, default=0, help="daily quota reset hour in UTC (default: 0)")
    rebalance.add_argument("--json", action="store_true", help="output structured JSON")
    rebalance.set_defaults(func=cmd_rebalance)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
