import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "auto_sync_official.py"
spec = importlib.util.spec_from_file_location("auto_sync_official", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class OfficialCatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.active = self.root / "active.json"
        self.other = self.root / "default-unused.json"
        self.config = self.root / "config.toml"
        self.cache = self.root / "models_cache.json"
        self.auth = self.root / "auth.json"
        self.state = self.root / "sync-state.json"
        self.now = 1000
        self.template = {
            "slug": "gpt-6-sol", "display_name": "GPT-6 Sol",
            "supported_reasoning_levels": [
                {"effort": effort, "description": effort}
                for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "default_reasoning_level": "low", "context_window": 1050000,
            "visibility": "list", "priority": 3,
        }
        self.active.write_text(json.dumps({"models": [self.template, {"slug": "claude-sonnet-4-6"}]}))
        self.other.write_text(json.dumps({"models": []}))
        self.config.write_text(f'model_catalog_json = "{self.active}"\n')
        self.cache.write_text(json.dumps({"models": [self.template]}))
        self.auth.write_text(json.dumps({"tokens": {"account_id": "account-one"}}))

    def run_sync(self, **kwargs):
        return sync.sync_catalog(self.config, self.cache, self.auth, state_path=self.state,
                                 clock=lambda: self.now, **kwargs)

    def test_account_discovery_probes_and_preserves_unmanaged_models(self):
        calls = []
        def probe(slug, payload, codex):
            calls.append((slug, payload))
            return True
        result = self.run_sync(apply=True, account_fetch=lambda _: ({"gpt-6.1-sol"}, "ok"), probe=probe)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["added_models"], ["gpt-6.1-sol"])
        self.assertEqual(len(calls), 1)
        self.assertEqual([m["slug"] for m in json.loads(self.active.read_text())["models"]],
                         ["gpt-6-sol", "claude-sonnet-4-6", "gpt-6.1-sol"])
        self.assertEqual(json.loads(self.other.read_text()), {"models": []})
        self.assertEqual([m["effort"] for m in calls[0][1]["models"][-1]["supported_reasoning_levels"]],
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(calls[0][1]["models"][-1]["default_reasoning_level"], "medium")
        self.assertEqual(os.stat(self.active).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(result["backup"]).st_mode & 0o777, 0o600)
        again = self.run_sync(apply=True, account_fetch=lambda _: self.fail("cooldown should skip account API"),
                              probe=lambda *args: self.fail("idempotent sync probed again"))
        self.assertEqual(again["status"], "deferred")
        again = self.run_sync(apply=True, force=True, account_fetch=lambda _: ({"gpt-6.1-sol"}, "ok"),
                              probe=lambda *args: self.fail("idempotent sync probed again"))
        self.assertEqual(again["status"], "unchanged")

    def test_403_and_failed_probe_never_publish_or_remove_models(self):
        original = self.active.read_bytes()
        result = self.run_sync(account_fetch=lambda _: (None, "http_403"))
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["candidates"], ["gpt-6.1-sol"])
        self.assertEqual(self.active.read_bytes(), original)
        result = self.run_sync(candidate="gpt-6.1-sol", apply=True,
                               account_fetch=lambda _: (None, "http_403"), probe=lambda *args: False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["probe_failed"], ["gpt-6.1-sol"])
        self.assertEqual(self.active.read_bytes(), original)

    def test_unknown_account_model_requires_metadata(self):
        result = self.run_sync(account_fetch=lambda _: ({"gpt-9-unknown"}, "ok"),
                               probe=lambda *args: self.fail("metadata missing"))
        self.assertEqual(result["pending_metadata"], ["gpt-9-unknown"])
        self.assertEqual(result["added_models"], [])

    def test_preview_and_config_switch_do_not_write(self):
        original = self.active.read_bytes()
        preview = self.run_sync(candidate="gpt-6.1-sol", account_fetch=lambda _: (None, "http_403"))
        self.assertEqual(preview["status"], "planned")
        self.assertEqual(self.active.read_bytes(), original)
        def switching_probe(*args):
            self.config.write_text(f'model_catalog_json = "{self.other}"\n')
            return True
        with self.assertRaisesRegex(ValueError, "active catalog changed"):
            self.run_sync(candidate="gpt-6.1-sol", apply=True,
                          account_fetch=lambda _: (None, "http_403"), probe=switching_probe)
        self.assertEqual(self.active.read_bytes(), original)
        self.assertEqual(json.loads(self.other.read_text()), {"models": []})

    def test_real_probe_shape_uses_temporary_catalog_and_classifies_failure(self):
        payload = {"models": [self.template, sync.candidate_entry("gpt-6.1-sol", [self.template], {})]}
        seen = []
        def completed(command, **kwargs):
            seen.append(command)
            catalog_override = command[command.index("--config") + 1]
            path = Path(json.loads(catalog_override.split("=", 1)[1]))
            self.assertEqual(json.loads(path.read_text())["models"][-1]["slug"], "gpt-6.1-sol")
            self.assertIn("--ephemeral", command)
            self.assertIn("read-only", command)
            Path(command[command.index("--output-last-message") + 1]).write_text("CODEX_BRIDGE_OK")
            return subprocess.CompletedProcess(command, 0, stderr="")
        with patch.object(sync.subprocess, "run", side_effect=completed):
            self.assertEqual(sync.probe_candidate("gpt-6.1-sol", payload, "codex"), (True, "completed"))
        self.assertEqual(len(seen), 1)
        with patch.object(sync.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, stderr="model_not_found")):
            self.assertEqual(sync.probe_candidate("gpt-6.1-sol", payload, "codex"),
                             (False, "model_unavailable"))

    def test_backoff_and_source_change_bypass_cooldown(self):
        calls = []
        def account_fetch(_):
            calls.append(self.now)
            return None, "http_403"
        def failed_probe(*args):
            return False, "model_unavailable"
        first = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertEqual(first["next_due"], self.now + 6 * 3600)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
        skipped = self.run_sync(apply=True, account_fetch=lambda _: self.fail("premature request"),
                                probe=lambda *args: self.fail("premature probe"))
        self.assertEqual(skipped["status"], "deferred")
        self.now += 6 * 3600
        second = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertEqual(second["next_due"], self.now + 12 * 3600)
        self.now += 12 * 3600
        third = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertEqual(third["next_due"], self.now + 24 * 3600)
        self.now += 24 * 3600
        fourth = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertEqual(fourth["next_due"], self.now + 24 * 3600)
        self.cache.write_text(json.dumps({"models": [self.template, {"slug": "gpt-7-new",
                                                                      "visibility": "list"}]}))
        changed = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertNotEqual(changed["status"], "deferred")
        self.assertEqual(changed["next_due"], self.now + 6 * 3600)
        self.auth.write_text(json.dumps({"tokens": {"account_id": "account-two"}}))
        switched = self.run_sync(apply=True, account_fetch=account_fetch, probe=failed_probe)
        self.assertNotEqual(switched["status"], "deferred")
        self.assertEqual(switched["next_due"], self.now + 6 * 3600)
        self.assertEqual(len(calls), 6)

    def test_manual_force_bypasses_cooldown(self):
        self.run_sync(apply=True, account_fetch=lambda _: (None, "http_403"),
                      probe=lambda *args: False)
        forced = self.run_sync(apply=True, force=True, account_fetch=lambda _: (None, "http_403"),
                               probe=lambda *args: False)
        self.assertEqual(forced["status"], "blocked")
        self.assertEqual(json.loads(self.state.read_text())["failures"], 2)


if __name__ == "__main__":
    unittest.main()
