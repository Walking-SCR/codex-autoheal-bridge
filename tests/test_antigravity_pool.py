import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
