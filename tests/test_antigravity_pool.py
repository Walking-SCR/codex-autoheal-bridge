import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import antigravity_pool


class AntigravityPoolTests(unittest.TestCase):
    def test_redacted_audit_does_not_include_email(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "antigravity-user@example.com.json"
            path.write_text(json.dumps({"type": "antigravity", "email": "user@example.com", "priority": 100}))
            records = antigravity_pool.load_antigravity_files(Path(directory))
            self.assertEqual(len(records), 1)
            self.assertNotIn("user@example.com", records[0]["id"])
            self.assertTrue(records[0]["email_present"])
            output = StringIO()
            with redirect_stdout(output):
                args = type("Args", (), {"config": Path(directory) / "config.yaml"})()
                antigravity_pool.cmd_audit(args)
            self.assertNotIn("user@example.com", output.getvalue())

    def test_policy_patch_is_idempotent_and_has_single_routing_block(self):
        initial = "auth-dir: ~/.cli-proxy-api\n\n"
        once = antigravity_pool.patch_routing_block(initial)
        twice = antigravity_pool.patch_routing_block(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("routing:"), 1)
        self.assertIn('strategy: "fill-first"', once)
        self.assertIn("save-cooldown-status: true", once)

    def test_set_priority_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"type": "antigravity", "refresh_token": "secret", "priority": 1}))
            args = type("Args", (), {"file": path, "priority": "50", "apply": True})()
            self.assertEqual(antigravity_pool.cmd_set_priority(args), 0)
            data = json.loads(path.read_text())
            self.assertEqual(data["priority"], 50)
            self.assertEqual(data["refresh_token"], "secret")
            self.assertTrue(any(p.name.startswith("account.json.bak-") for p in path.parent.iterdir()))

    def test_validation_plan_maps_only_accounts_with_google_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "antigravity-user@example.com.json"
            credential.write_text(json.dumps({"type": "antigravity", "email": "user@example.com", "priority": 100}))
            (root / "user.cds").write_text(json.dumps({
                "provider": "antigravity",
                "records": [
                    {
                        "auth_id": credential.name,
                        "model": "gemini-3.8-flash-high",
                        "status": "cooling",
                        "reason": json.dumps({
                            "error": {
                                "status": "PERMISSION_DENIED",
                                "details": [{
                                    "reason": "VALIDATION_REQUIRED",
                                    "metadata": {"validation_url": "https://accounts.google.com/?opaque=must-not-print"},
                                }],
                            },
                        }),
                    },
                    {
                        "auth_id": credential.name,
                        "model": "gemini-3.8-flash-high",
                        "status": "cooling",
                        "reason": "quota",
                    },
                    {
                        "auth_id": credential.name,
                        "model": "gemini-3.7-flash-high",
                        "status": "cooling",
                        "reason": "VALIDATION_REQUIRED",
                    },
                ],
            }))

            targets = antigravity_pool.validation_targets(root, "gemini-3.8-flash-high")
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["email"], "user@example.com")
            self.assertEqual(targets[0]["models"], {"gemini-3.8-flash-high"})
            output = StringIO()
            with redirect_stdout(output):
                antigravity_pool.print_validation_plan(targets)
            self.assertIn("user@example.com", output.getvalue())
            self.assertNotIn("opaque=must-not-print", output.getvalue())

    def test_validation_login_requires_exact_marked_account_and_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "antigravity-user@example.com.json"
            credential.write_text(json.dumps({"type": "antigravity", "email": "user@example.com"}))
            (root / "user.cds").write_text(json.dumps({
                "provider": "antigravity",
                "records": [{
                    "auth_id": credential.name,
                    "model": "gemini-3.8-flash-high",
                    "reason": "VALIDATION_REQUIRED",
                }],
            }))
            config = root / "config.yaml"
            config.write_text(f"auth-dir: {root}\n")

            preview = type("Args", (), {
                "config": config,
                "model": "gemini-3.8-flash-high",
                "account": ["user@example.com"],
                "apply": False,
                "binary": "/not/run/in-preview",
            })()
            self.assertEqual(antigravity_pool.cmd_login_validation(preview), 0)

            unapproved = type("Args", (), {
                "config": config,
                "model": "gemini-3.8-flash-high",
                "account": ["other@example.com"],
                "apply": True,
                "binary": "/not/run/in-test",
            })()
            self.assertEqual(antigravity_pool.cmd_login_validation(unapproved), 2)

    def test_preserve_auth_policy_keeps_priority_and_disabled_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"type": "antigravity", "email": "user@example.com", "refresh_token": "secret", "priority": 50, "disabled": False}))
            original = {"priority": 100, "disabled": False}
            self.assertTrue(antigravity_pool.preserve_auth_policy(path, original))
            data = json.loads(path.read_text())
            self.assertEqual(data["priority"], 100)
            self.assertFalse(data["disabled"])
            self.assertEqual(data["refresh_token"], "secret")

    def test_validation_login_stops_after_wrong_google_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for email in ("first@example.com", "second@example.com"):
                (root / f"antigravity-{email}.json").write_text(json.dumps({"type": "antigravity", "email": email}))
            (root / "status.cds").write_text(json.dumps({
                "provider": "antigravity",
                "records": [
                    {"auth_id": f"antigravity-{email}.json", "model": "gemini-3.8-flash-high", "reason": "VALIDATION_REQUIRED"}
                    for email in ("first@example.com", "second@example.com")
                ],
            }))
            config = root / "config.yaml"
            config.write_text(f"auth-dir: {root}\n")
            args = type("Args", (), {
                "config": config,
                "model": "gemini-3.8-flash-high",
                "account": ["first@example.com", "second@example.com"],
                "apply": True,
                "binary": "/mock/cli-proxy-api",
            })()
            wrong_account = subprocess.CompletedProcess(
                args=["cli-proxy-api"], returncode=0, stdout="Authenticated as unrelated@example.com\n"
            )
            with patch.object(antigravity_pool.subprocess, "run", return_value=wrong_account) as run:
                self.assertEqual(antigravity_pool.cmd_login_validation(args), 3)
            self.assertEqual(run.call_count, 1)

    def test_rebalance_imminent_reset_prioritized_over_normal_pro(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Account A: Free tier, resets in 30 mins (imminent)
            (root / "antigravity-imminent@example.com.json").write_text(json.dumps({
                "type": "antigravity",
                "email": "imminent@example.com",
                "tier": "free-tier",
                "priority": 50,
                "reset_hour": 0,
            }))
            # Account B: Pro tier, resets in 16 hours (normal active)
            (root / "antigravity-pro@example.com.json").write_text(json.dumps({
                "type": "antigravity",
                "email": "pro@example.com",
                "tier": "standard-tier",
                "priority": 100,
                "reset_hour": 16,
            }))
            # Account C: Cooling in 429
            (root / "antigravity-cooling@example.com.json").write_text(json.dumps({
                "type": "antigravity",
                "email": "cooling@example.com",
                "tier": "standard-tier",
                "priority": 200,
            }))
            (root / "antigravity-cooling_example.com.cds").write_text(json.dumps({
                "provider": "antigravity",
                "records": [{
                    "auth_id": "antigravity-cooling@example.com.json",
                    "status": "cooling",
                    "reason": "quota",
                    "next_retry_after": "2099-01-01T00:00:00Z",
                }],
            }))

            from datetime import datetime, timezone, timedelta
            fixed_now = datetime(2026, 9, 25, 23, 30, tzinfo=timezone.utc)
            # At 23:30 UTC, next 00:00 UTC reset is in 30 mins!
            # So imminent@example.com resets in 30 mins (imminent window)
            evaluated = antigravity_pool.rebalance_antigravity_pool(
                auth_dir=root,
                now=fixed_now,
                reset_hour_utc=0,
                critical_window_hours=1.0,
            )
            by_email = {item["account"]: item for item in evaluated}
            self.assertEqual(by_email["imminent@example.com"]["status"], "IMMINENT_RESET")
            self.assertEqual(by_email["pro@example.com"]["status"], "ACTIVE")
            self.assertEqual(by_email["cooling@example.com"]["status"], "COOLING")
            # Imminent reset priority > Pro active priority > Cooling priority
            self.assertGreater(by_email["imminent@example.com"]["target_priority"], by_email["pro@example.com"]["target_priority"])
            self.assertGreater(by_email["pro@example.com"]["target_priority"], by_email["cooling@example.com"]["target_priority"])

    def test_rebalance_pro_prioritized_over_free_when_both_normal_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "antigravity-free@example.com.json").write_text(json.dumps({
                "type": "antigravity", "email": "free@example.com", "tier": "free-tier", "priority": 100
            }))
            (root / "antigravity-pro@example.com.json").write_text(json.dumps({
                "type": "antigravity", "email": "pro@example.com", "tier": "standard-tier", "priority": 50
            }))
            from datetime import datetime, timezone
            fixed_now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc) # 12h to reset
            evaluated = antigravity_pool.rebalance_antigravity_pool(
                auth_dir=root, now=fixed_now, reset_hour_utc=0, critical_window_hours=2.0
            )
            by_email = {item["account"]: item for item in evaluated}
            self.assertEqual(by_email["pro@example.com"]["status"], "ACTIVE")
            self.assertEqual(by_email["free@example.com"]["status"], "ACTIVE")
            # Pro tier gets higher priority than Free tier when reset time is identical
            self.assertGreater(by_email["pro@example.com"]["target_priority"], by_email["free@example.com"]["target_priority"])


if __name__ == "__main__":
    unittest.main()
