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
from datetime import datetime, timezone
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
                "email_present": bool(email),
                "priority": priority,
                "disabled": bool(data.get("disabled", False)),
                "project_present": bool(data.get("project_id", data.get("metadata", {}).get("project_id", ""))),
            }
        )
    return records


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
