import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quota_failover.py"
BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "bridge.py"


def load_module():
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("quota_failover", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuotaFailoverTests(unittest.TestCase):
    def make_auto_args(self, m, root):
        config = root / "config.toml"
        catalog = root / "catalog.json"
        profile = root / "profile.toml"
        catalog.write_text(json.dumps({"models": [{"slug": "deepseek-v4-pro"}]}))
        config.write_text(
            'model_provider = "openai"\nmodel = "gpt-5.6-sol"\n'
            f'model_catalog_json = {json.dumps(str(catalog))}\n'
        )
        profile.write_text(
            '[model_providers.cli_proxy]\nbase_url = "http://127.0.0.1:8317/v1"\n'
            'wire_api = "responses"\n[model_providers.cli_proxy.auth]\ncommand = "helper"\n'
        )
        return m.parser().parse_args([
            "auto-mode", "--model", "deepseek-v4-pro", "--config", str(config),
            "--profile-config", str(profile), "--state", str(root / "quota.json"),
            "--external-catalog", str(root / "external.json"), "--state-dir", str(root),
            "--watch-plist", str(root / "watch.plist"),
        ])

    def enable_test_monitor(self, m, args):
        args.apply = True
        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": True}), \
             mock.patch.object(m.bridge, "resolve_codex_cli", return_value="/test/codex"), \
             mock.patch.object(m, "install_watcher") as install:
            m.configure_auto_mode(args)
        self.assertEqual(install.call_args.kwargs["command"], "watch-auto")

    def test_auto_preview_has_no_writes_and_failed_quota_cannot_enable(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_auto_args(m, root)
            config_before = Path(args.config).read_bytes()
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": True}):
                result = m.configure_auto_mode(args)
            self.assertEqual(result["status"], "planned")
            self.assertFalse(Path(args.state).exists())
            self.assertFalse(Path(args.external_catalog).exists())
            self.assertFalse(Path(args.watch_plist).exists())
            args.apply = True
            with mock.patch.object(m.sys, "platform", "darwin"), \
                 mock.patch.object(m, "read_codex_rate_limits", side_effect=TimeoutError("offline")):
                with self.assertRaisesRegex(RuntimeError, "live quota"):
                    m.configure_auto_mode(args)
            self.assertEqual(Path(args.config).read_bytes(), config_before)
            self.assertFalse(Path(args.state).exists())

    def test_auto_two_cycles_preserve_baseline_and_never_restart(self):
        m = load_module()
        blocked = {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 100}}}
        healthy = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 0}}}
        unknown = {"ordinaryUsageAllowed": None, "rateLimits": {"primary": {"usedPercent": 0}}}
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            watch = m.parser().parse_args(["watch-auto", "--state", args.state, "--once"])
            with mock.patch.object(m, "notify"), \
                 mock.patch.object(m, "app_running", return_value=True), \
                 mock.patch.object(m, "install_watcher") as install, \
                 mock.patch.object(m, "restart_codex") as restart, \
                 mock.patch.object(m, "read_codex_rate_limits", side_effect=[
                     blocked, blocked, TimeoutError("offline"), unknown, healthy, blocked, healthy
                 ]) as read:
                first = m.auto_mode_step(watch)
                self.assertEqual(first["mode"], "external")
                self.assertEqual(first["status"], "switched")
                external_bytes = Path(args.config).read_bytes()
                for _ in range(3):
                    result = m.auto_mode_step(watch)
                    self.assertEqual(result["mode"], "external")
                    self.assertEqual(result["status"], "waiting")
                    self.assertEqual(Path(args.config).read_bytes(), external_bytes)
                recovered = m.auto_mode_step(watch)
                self.assertEqual(recovered["mode"], "openai")
                self.assertTrue(m.load_state(Path(args.state))["auto_monitor"]["enabled"])
                self.assertEqual(m.auto_mode_step(watch)["mode"], "external")
                self.assertEqual(m.auto_mode_step(watch)["mode"], "openai")
            self.assertEqual(read.call_count, 7)
            self.assertEqual(m.tomllib.loads(Path(args.config).read_text())["model"], "gpt-5.6-sol")
            restart.assert_not_called()
            install.assert_not_called()

    def test_auto_unknown_or_allowed_quota_cannot_enter_external(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            before = Path(args.config).read_bytes()
            watch = m.parser().parse_args(["watch-auto", "--state", args.state, "--once"])
            for payload in (
                {"ordinaryUsageAllowed": None, "rateLimits": {"primary": {"usedPercent": 50}}},
                {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 50}}},
            ):
                with mock.patch.object(m, "read_codex_rate_limits", return_value=payload):
                    self.assertEqual(m.auto_mode_step(watch)["mode"], "openai")
            self.assertEqual(Path(args.config).read_bytes(), before)

            # Window exhaustion (100%) triggers failover even when ordinaryUsageAllowed is None
            exhausted_payload = {"ordinaryUsageAllowed": None, "rateLimits": {"primary": {"usedPercent": 100}}}
            with mock.patch.object(m, "read_codex_rate_limits", return_value=exhausted_payload), \
                 mock.patch.object(m, "notify"):
                step_result = m.auto_mode_step(watch)
                self.assertEqual(step_result["mode"], "external")
                self.assertEqual(step_result["status"], "switched")

    def test_auto_routing_edit_stops_before_query_or_overwrite(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            modified = Path(args.config).read_text().replace('model_provider = "openai"', 'model_provider = "custom"')
            Path(args.config).write_text(modified)
            watch = m.parser().parse_args(["watch-auto", "--state", args.state, "--once"])
            with mock.patch.object(m, "read_codex_rate_limits") as read, mock.patch.object(m, "notify") as notify:
                self.assertEqual(m.auto_mode_step(watch)["status"], "stopped")
                self.assertEqual(m.auto_mode_step(watch)["status"], "stopped")
            self.assertEqual(Path(args.config).read_text(), modified)
            read.assert_not_called()
            notify.assert_called_once()

    def test_auto_model_dropdown_edit_does_not_stop_monitor(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            # Switching model in UI does NOT change topology and must not stop monitor
            modified = Path(args.config).read_text().replace("gpt-5.6-sol", "gpt-5-sol")
            Path(args.config).write_text(modified)
            watch = m.parser().parse_args(["watch-auto", "--state", args.state, "--once"])
            healthy = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 0}}}
            with mock.patch.object(m, "read_codex_rate_limits", return_value=healthy):
                result = m.auto_mode_step(watch)
                self.assertEqual(result["status"], "waiting")
                self.assertEqual(result["mode"], "openai")

    def test_auto_disable_preserves_mode_and_config(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            stop = m.parser().parse_args(["stop-auto", "--state", args.state, "--apply"])
            before = Path(args.config).read_bytes()
            with mock.patch.object(m.subprocess, "run") as run:
                result = m.stop_auto_mode(stop)
            self.assertEqual(result["mode"], "openai")
            self.assertFalse(m.load_state(Path(args.state))["auto_monitor"]["enabled"])
            self.assertEqual(Path(args.config).read_bytes(), before)
            run.assert_not_called()  # no installed job was created by this isolated test

    def test_auto_config_edit_during_query_is_preserved_and_monitor_stops(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            self.enable_test_monitor(m, args)
            watch = m.parser().parse_args(["watch-auto", "--state", args.state, "--once"])
            modified = Path(args.config).read_text().replace('model_provider = "openai"', 'model_provider = "custom"')
            def concurrent_edit(*unused):
                Path(args.config).write_text(modified)
                return {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 100}}}
            with mock.patch.object(m, "read_codex_rate_limits", side_effect=concurrent_edit), \
                 mock.patch.object(m, "notify"), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit) as raised:
                m.cmd_auto(watch)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(Path(args.config).read_text(), modified)
            self.assertFalse(Path(args.external_catalog).exists())
            self.assertFalse(m.load_state(Path(args.state))["auto_monitor"]["enabled"])

    def test_auto_install_failure_disables_monitor_and_leaves_routing_unchanged(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_auto_args(m, Path(tmp))
            before = Path(args.config).read_bytes()
            args.apply = True
            with mock.patch.object(m.sys, "platform", "darwin"), \
                 mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": True}), \
                 mock.patch.object(m.bridge, "resolve_codex_cli", return_value="/test/codex"), \
                 mock.patch.object(m, "install_watcher", side_effect=RuntimeError("launchctl failed")):
                with self.assertRaises(RuntimeError):
                    m.configure_auto_mode(args)
            self.assertFalse(m.load_state(Path(args.state))["auto_monitor"]["enabled"])
            self.assertEqual(Path(args.config).read_bytes(), before)

    def test_cli_discovery_supports_desktop_layouts_without_path(self):
        m = load_module()
        for app in ("ChatGPT.app", "Codex.app"):
            for suffix in (
                "codex-cli/bin/codex",
                "codex-cli/CodexCLI.app/Contents/MacOS/codex",
                "codex",
            ):
                with self.subTest(app=app, suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    executable = root / app / "Contents/Resources" / suffix
                    executable.parent.mkdir(parents=True)
                    executable.write_text("#!/bin/sh\nexit 0\n")
                    executable.chmod(0o700)
                    with mock.patch.dict(os.environ, {"PATH": tmp, "CODEX_CLI_PATH": ""}):
                        self.assertEqual(m.bridge.default_codex_cli((root,)), str(executable))

    def test_cli_override_precedes_path_and_explicit_invalid_path_is_rejected(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            preferred = root / "preferred"
            fallback = root / "codex"
            for path in (preferred, fallback):
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": tmp, "CODEX_CLI_PATH": str(preferred)}):
                self.assertEqual(m.bridge.default_codex_cli(()), str(preferred))
                self.assertEqual(m.bridge.resolve_codex_cli("codex"), str(fallback))
                with self.assertRaisesRegex(FileNotFoundError, "Use --codex"):
                    m.bridge.resolve_codex_cli(str(root / "missing"))
                preferred.chmod(0o600)
                self.assertEqual(m.bridge.default_codex_cli(()), str(fallback))

    def test_live_failure_is_nonzero_and_marks_cached_quota_stale(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            m.save_state(state_path, {"mode": "openai", "last_analysis": {"recovered": True}})
            args = m.parser().parse_args(["quota-status", "--live", "--state", str(state_path)])
            output = io.StringIO()
            with mock.patch.object(m, "read_codex_rate_limits", side_effect=FileNotFoundError("missing CLI")):
                with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    m.cmd_quota_status(args)
            result = json.loads(output.getvalue())
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["quota_source"], "cached")
            self.assertTrue(result["quota_stale"])

    def test_preview_does_not_create_state_or_catalog_or_modify_config(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text('model_provider = "openai"\nmodel = "gpt-5.6-sol"\n')
            profile = root / "profile.toml"
            profile.write_text(
                '[model_providers.cli_proxy]\nbase_url = "http://127.0.0.1:8317/v1"\n'
                'wire_api = "responses"\n[model_providers.cli_proxy.auth]\ncommand = "helper"\n'
            )
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"models": [{"slug": "gemini-3.8-flash-high"}]}))
            state = root / "quota.json"
            external_catalog = root / "external.json"
            args = m.parser().parse_args([
                "external-mode", "--config", str(config), "--profile-config", str(profile),
                "--catalog", str(catalog), "--state", str(state),
                "--external-catalog", str(external_catalog), "--state-dir", tmp,
            ])
            original = config.read_bytes()
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": False}):
                result = m.apply_external_mode(args)
            self.assertEqual(result["status"], "planned")
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(state.exists())
            self.assertFalse(external_catalog.exists())
            args.apply = True
            with mock.patch.object(m, "read_codex_rate_limits", side_effect=FileNotFoundError("missing CLI")):
                with self.assertRaises(FileNotFoundError):
                    m.apply_external_mode(args)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(external_catalog.exists())

    def test_missing_percentage_or_bucket_cannot_confirm_recovery(self):
        m = load_module()
        self.assertFalse(m.analyze_rate_limits({"ordinaryUsageAllowed": True})["recovered"])
        self.assertFalse(m.analyze_rate_limits({
            "ordinaryUsageAllowed": True,
            "rateLimits": {"primary": {"usedPercent": None}},
        })["recovered"])
        for window in ({}, "malformed", {"usedPercent": -1}, {"usedPercent": True}):
            with self.subTest(window=window):
                self.assertFalse(m.analyze_rate_limits({
                    "ordinaryUsageAllowed": True, "rateLimits": {"primary": window},
                })["recovered"])

    def test_external_entry_and_backend_confirmed_return_in_isolated_config(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            catalog = root / "active-catalog.json"
            catalog.write_text(json.dumps({"models": [{"slug": "deepseek-v4-pro"}]}))
            original = (
                'model_provider = "openai"\nmodel = "gpt-5.6-sol"\n'
                f'model_catalog_json = {json.dumps(str(catalog))}\n'
            )
            config.write_text(original)
            profile = root / "profile.toml"
            profile.write_text(
                '[model_providers.cli_proxy]\nbase_url = "http://127.0.0.1:8317/v1"\n'
                'wire_api = "responses"\n[model_providers.cli_proxy.auth]\ncommand = "helper"\n'
            )
            state = root / "quota.json"
            external_catalog = root / "external.json"
            args = m.parser().parse_args([
                "external-mode", "--apply", "--no-watch", "--config", str(config),
                "--profile-config", str(profile), "--state", str(state),
                "--external-catalog", str(external_catalog), "--state-dir", tmp,
            ])
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": False}), \
                 mock.patch.object(m, "notify"), \
                 mock.patch.object(m, "install_watcher") as install, \
                 mock.patch.object(m, "restart_codex") as restart:
                result = m.apply_external_mode(args)
            self.assertEqual(result["status"], "applied")
            self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
            parsed = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed["model_provider"], "cli_proxy")
            self.assertFalse(parsed["model_providers"]["cli_proxy"]["requires_openai_auth"])
            install.assert_not_called()
            restart.assert_not_called()
            config.write_text(config.read_text() + '\n[mcp_servers.new]\nurl = "https://example.test"\n')
            watch_args = m.parser().parse_args(["watch-quota", "--once", "--state", str(state)])
            with mock.patch.object(m, "read_codex_rate_limits", return_value={
                "ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 0}},
            }), mock.patch.object(m, "notify"), mock.patch.object(m, "app_running", return_value=True), \
                 mock.patch.object(m, "restart_codex") as restart:
                returned = m.watch_quota(watch_args)
            self.assertEqual(returned["mode"], "openai")
            restored = m.tomllib.loads(config.read_text())
            self.assertEqual(restored["model"], "gpt-5.6-sol")
            self.assertIn("new", restored["mcp_servers"])
            self.assertEqual(m.load_state(state)["switch_back_status"], "config_restored_restart_pending")
            restart.assert_not_called()

    def test_watcher_query_failure_cannot_restore_from_cached_recovery(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "quota.json"
            m.save_state(state, {"mode": "external", "last_analysis": {"recovered": True}})
            args = m.parser().parse_args(["watch-quota", "--once", "--state", str(state)])
            with mock.patch.object(m, "read_codex_rate_limits", side_effect=TimeoutError("offline")), \
                 mock.patch.object(m, "restore_openai_mode") as restore:
                result = m.watch_quota(args)
            self.assertEqual(result["status"], "waiting")
            self.assertIsNone(result["quota"])
            restore.assert_not_called()

    def test_unified_app_is_running_and_standalone_cli_restart_is_not_guessed(self):
        m = load_module()
        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.subprocess, "run", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)]):
            self.assertTrue(m.app_running())
        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.bridge, "resolve_codex_cli", return_value="/usr/local/bin/codex"), \
             mock.patch.object(m.subprocess, "run") as run:
            self.assertFalse(m.restart_codex())
            run.assert_not_called()

    def test_windows_app_running_and_restart(self):
        m = load_module()
        tasklist_output = 'Image Name,PID,Session Name,Session#,Mem Usage\n"Codex.exe","1234","Console","1","50,000 K"\n'
        with mock.patch.object(m, "is_windows", return_value=True), \
             mock.patch.object(m.sys, "platform", "win32"), \
             mock.patch.object(m.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=tasklist_output)):
            self.assertTrue(m.app_running())

        with mock.patch.object(m, "is_windows", return_value=True), \
             mock.patch.object(m.sys, "platform", "win32"), \
             mock.patch.object(m, "desktop_app_path", return_value=Path("C:/Users/test/AppData/Local/Programs/Codex/Codex.exe")), \
             mock.patch.object(m, "app_running", side_effect=[True, False]), \
             mock.patch.object(m.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(m.subprocess, "Popen") as popen:
            self.assertTrue(m.restart_codex())
            popen.assert_called_once()

    def test_windows_watcher_install_and_uninstall(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "quota.json"
            script_path = root / "quota_failover.py"
            script_path.write_text("# dummy", encoding="utf-8")
            startup_dir = root / "Startup"
            with mock.patch.object(m, "is_windows", return_value=True), \
                 mock.patch.object(m.bridge, "DEFAULT_STATE_DIR", root), \
                 mock.patch.object(m.bridge, "default_windows_startup_dir", return_value=startup_dir), \
                 mock.patch.object(m.bridge, "resolve_codex_cli", return_value="C:/codex/codex.exe"):
                m.install_windows_watcher(script=script_path, state_path=state_path, codex="codex")
                cmd_file = root / "run-quota-watch.cmd"
                vbs_file = root / "run-quota-watch-hidden.vbs"
                startup_file = startup_dir / "codex-quota-watch.vbs"
                self.assertTrue(cmd_file.exists())
                self.assertTrue(vbs_file.exists())
                self.assertTrue(startup_file.exists())
                self.assertIn("run-quota-watch.cmd", vbs_file.read_text(encoding="utf-8"))

                m.uninstall_windows_watcher()
                self.assertFalse(startup_file.exists())

    def test_restore_preserves_unrelated_settings_and_stops_on_routing_edits(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            original = 'model_provider = "openai"\nmodel = "gpt-5.6-sol"\n'
            external = 'model_provider = "cli_proxy"\nmodel = "gemini-3.8-flash-high"\n'
            state = root / "quota.json"
            m.save_state(state, {
                "mode": "external", "config_path": str(config),
                "openai_config": m.capture_openai_config(m.tomllib.loads(original)),
                "external_config": m.capture_openai_config(m.tomllib.loads(external)),
            })
            # 1. Topology edits (e.g. changing model_provider) must be rejected
            topology_edited = external.replace('model_provider = "cli_proxy"', 'model_provider = "custom_proxy"')
            config.write_text(topology_edited)
            with self.assertRaisesRegex(RuntimeError, "user edits"):
                m.restore_openai_mode(state_path=state)
            self.assertEqual(config.read_text(), topology_edited)

            # 2. Runtime model dropdown changes in external mode do NOT conflict and allow restore
            model_switched = external.replace("gemini-3.8-flash-high", "deepseek-v4-pro")
            config.write_text(model_switched)
            with mock.patch.object(m, "notify"), mock.patch.object(m, "app_running", return_value=True):
                result = m.restore_openai_mode(state_path=state)
            parsed = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed["model_provider"], "openai")
            self.assertEqual(parsed["model"], "gpt-5.6-sol")

            # Reset back to external mode to test preserving unrelated settings (like mcp_servers)
            config.write_text(external + '\n[mcp_servers.new]\nurl = "https://example.test"\n')
            m.save_state(state, {
                "mode": "external", "config_path": str(config),
                "openai_config": m.capture_openai_config(m.tomllib.loads(original)),
                "external_config": m.capture_openai_config(m.tomllib.loads(external)),
            })
            with mock.patch.object(m, "notify"), mock.patch.object(m, "app_running", return_value=True):
                result = m.restore_openai_mode(state_path=state)
            parsed = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed["model_provider"], "openai")
            self.assertEqual(parsed["model"], "gpt-5.6-sol")
            self.assertIn("new", parsed["mcp_servers"])
            self.assertTrue(result["restart_required"])

    def test_desktop_app_selection_ignores_nested_cli_app(self):
        m = load_module()
        with mock.patch.object(m.bridge, "resolve_codex_cli", return_value=
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        ):
            self.assertEqual(m.desktop_app_path(), Path("/Applications/ChatGPT.app"))

    def test_watcher_pins_an_absolute_cli_without_restarting_apps(self):
        m = load_module()
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plist = root / "watch.plist"
            with mock.patch.object(m.sys, "platform", "darwin"), \
                 mock.patch.object(m.bridge, "DEFAULT_STATE_DIR", root), \
                 mock.patch.object(m.bridge, "resolve_codex_cli", return_value="/opt/test/codex"), \
                 mock.patch.object(m.subprocess, "run", return_value=mock.Mock(returncode=0)):
                m.install_watcher(script=SCRIPT, plist_path=plist, state_path=root / "quota.json", codex="codex")
            payload = plistlib.loads(plist.read_bytes())
            self.assertEqual(payload["ProgramArguments"][-1], "/opt/test/codex")

    def test_latest_exhausted_window_controls_expected_recovery(self):
        m = load_module()
        payload = {
            "ordinaryUsageAllowed": False,
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 300,
                        "resetsAt": 2_000_000_000,
                    },
                    "secondary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10080,
                        "resetsAt": 2_000_100_000,
                    },
                    "spendControlReached": False,
                }
            },
        }
        result = m.analyze_rate_limits(payload)
        self.assertTrue(result["blocked"])
        self.assertFalse(result["recovered"])
        self.assertEqual(result["expected_recovery_at"], 2_000_100_000)
        self.assertEqual(
            [x["name"] for x in result["blocking_windows"]],
            ["primary", "secondary"],
        )

    def test_reset_time_alone_never_marks_recovered(self):
        m = load_module()
        payload = {
            "ordinaryUsageAllowed": None,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 0, "resetsAt": 1},
                "secondary": {"usedPercent": 0, "resetsAt": 1},
            },
        }
        self.assertFalse(m.analyze_rate_limits(payload)["recovered"])

    def test_lifecycle_state_machine_transitions(self):
        m = load_module()
        # 1. OPENAI_ACTIVE: desired=openai, effective=openai, pending_restart=False
        state = {"desired_mode": "openai", "effective_mode": "openai", "pending_restart": False}
        self.assertEqual(m.compute_lifecycle_state(state), "OPENAI_ACTIVE")

        # 2. EXTERNAL_RESTART_REQUIRED: desired=external, pending_restart=True
        state = {"desired_mode": "external", "effective_mode": "openai", "pending_restart": True}
        self.assertEqual(m.compute_lifecycle_state(state), "EXTERNAL_RESTART_REQUIRED")

        # 3. EXTERNAL_ACTIVE: desired=external, effective=external, pending_restart=False
        state = {"desired_mode": "external", "effective_mode": "external", "pending_restart": False}
        self.assertEqual(m.compute_lifecycle_state(state), "EXTERNAL_ACTIVE")

        # 4. OPENAI_RESTORE_PENDING: desired=openai, effective=external, pending_restart=True
        state = {"desired_mode": "openai", "effective_mode": "external", "pending_restart": True}
        self.assertEqual(m.compute_lifecycle_state(state), "OPENAI_RESTORE_PENDING")

    def test_render_cli_proxy_provider_targets_router_and_injects_header(self):
        m = load_module()
        profile = {
            "model_providers": {
                "cli_proxy": {
                    "name": "External Models",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "wire_api": "responses",
                    "auth": {
                        "command": "/usr/local/bin/token",
                        "args": ["get"],
                    },
                }
            }
        }
        rendered = m.render_cli_proxy_provider(profile)
        self.assertIn('base_url = "http://127.0.0.1:8318/v1"', rendered)
        self.assertIn('[model_providers.cli_proxy.http_headers]', rendered)
        self.assertIn('x-codex-bridge-mode = "external"', rendered)
        self.assertIn('requires_openai_auth = false', rendered)

    def test_routing_conflict_topology_vs_model_dropdown(self):
        m = load_module()
        original = {
            "model_provider": "openai",
            "model": "gpt-5.6-sol",
            "openai_base_url": "https://api.openai.com/v1",
            "model_catalog_json": "/path/catalog.json",
        }
        # Model change alone does NOT cause routing conflict
        model_changed = dict(original, model="gpt-5-sol")
        self.assertFalse(m.is_routing_conflict(model_changed, original))

        # Topology change DOES cause routing conflict
        provider_changed = dict(original, model_provider="cli_proxy")
        self.assertTrue(m.is_routing_conflict(provider_changed, original))

        base_url_changed = dict(original, openai_base_url="http://127.0.0.1:8318/v1")
        self.assertTrue(m.is_routing_conflict(base_url_changed, original))

        catalog_changed = dict(original, model_catalog_json="/other/catalog.json")
        self.assertTrue(m.is_routing_conflict(catalog_changed, original))

    def test_prepare_external_catalog_manifest_overlay_and_router_support(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_catalog = root / "source.json"
            target_catalog = root / "target.json"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)

            # Source catalog with native template
            source_catalog.write_text(json.dumps({
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 10,
                        "reasoning_efforts": ["low", "medium", "high"],
                    }
                ]
            }), encoding="utf-8")

            payload, path_str = m.prepare_external_catalog(
                source_catalog, state_dir, target_catalog, apply=True
            )
            slugs = {item["slug"]: item for item in payload["models"]}

            # GPT model should be hidden in external catalog
            self.assertEqual(slugs["gpt-5.6-sol"]["visibility"], "hide")

            # External models from models/ directory should be overlaid and visible as "list"
            for expected_model in ("claude-sonnet-4-6", "glm-5.2", "gemini-3.1-pro"):
                self.assertIn(expected_model, slugs)
                self.assertEqual(slugs[expected_model]["visibility"], "list")
                # Every visible external model must be supported by 8318 router
                self.assertTrue(m.bridge.is_router_supported(expected_model))

    def test_recovery_requires_backend_allow_and_windows_below_100(self):
        m = load_module()
        payload = {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 99, "resetsAt": 1},
                "secondary": {"usedPercent": 20, "resetsAt": 2},
                "spendControlReached": False,
            },
        }
        self.assertTrue(m.analyze_rate_limits(payload)["recovered"])
        payload["rateLimits"]["secondary"]["usedPercent"] = 100
        self.assertFalse(m.analyze_rate_limits(payload)["recovered"])

    def test_toggle_mode_alternates_between_openai_and_external(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text('model_provider = "openai"\nmodel = "gpt-5.6-sol"\n', encoding="utf-8")
            profile = root / "profile.toml"
            profile.write_text('[model_providers.cli_proxy]\nbase_url = "http://127.0.0.1:8317/v1"\nwire_api = "responses"\n', encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({
                "models": [
                    {"slug": "gpt-5.6-sol", "display_name": "GPT", "priority": 10},
                    {"slug": "gemini-3.8-flash-high", "display_name": "Gemini", "priority": 20},
                ]
            }), encoding="utf-8")
            ext_catalog = root / "ext_catalog.json"
            state = root / "quota.json"

            args = m.parser().parse_args([
                "toggle",
                "--config", str(config),
                "--profile-config", str(profile),
                "--catalog", str(catalog),
                "--external-catalog", str(ext_catalog),
                "--state-dir", str(root),
                "--state", str(state),
                "--apply",
                "--no-watch",
            ])

            # 1st toggle: from openai -> external
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": False}), \
                 mock.patch.object(m, "notify"):
                m.cmd_toggle_mode(args) if False else None  # type check

            # Let's call via apply_external_mode / restore_openai_mode simulation
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": False}), \
                 mock.patch.object(m, "notify"), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit) as exit1:
                m.cmd_toggle_mode(args)
            self.assertEqual(exit1.exception.code, 0)
            parsed1 = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed1["model_provider"], "cli_proxy")
            self.assertEqual(m.load_state(state)["mode"], "external")

            # 2nd toggle: from external -> openai
            with mock.patch.object(m, "notify"), mock.patch.object(m, "app_running", return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit2:
                m.cmd_toggle_mode(args)
            self.assertEqual(exit2.exception.code, 0)
            parsed2 = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed2["model_provider"], "openai")
            self.assertEqual(parsed2["model"], "gpt-5.6-sol")
            self.assertEqual(m.load_state(state)["mode"], "openai")

            # 3rd toggle: from openai -> external again
            with mock.patch.object(m, "read_codex_rate_limits", return_value={"ordinaryUsageAllowed": False}), \
                 mock.patch.object(m, "notify"), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit) as exit3:
                m.cmd_toggle_mode(args)
            self.assertEqual(exit3.exception.code, 0)
            parsed3 = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed3["model_provider"], "cli_proxy")
            self.assertEqual(m.load_state(state)["mode"], "external")

    def test_toggle_mode_succeeds_when_gpt_quota_not_exhausted(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text('model_provider = "openai"\nmodel = "gpt-5.6-sol"\n', encoding="utf-8")
            profile = root / "profile.toml"
            profile.write_text('[model_providers.cli_proxy]\nbase_url = "http://127.0.0.1:8317/v1"\nwire_api = "responses"\n', encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({
                "models": [
                    {"slug": "gpt-5.6-sol", "display_name": "GPT", "priority": 10},
                    {"slug": "gemini-3.8-flash-high", "display_name": "Gemini", "priority": 20},
                ]
            }), encoding="utf-8")
            ext_catalog = root / "ext_catalog.json"
            state = root / "quota.json"

            args = m.parser().parse_args([
                "toggle",
                "--config", str(config),
                "--profile-config", str(profile),
                "--catalog", str(catalog),
                "--external-catalog", str(ext_catalog),
                "--state-dir", str(root),
                "--state", str(state),
                "--apply",
            ])

            # Toggle from openai -> external even when GPT quota is healthy (98% remaining / 2% used)
            with mock.patch.object(m, "read_codex_rate_limits", return_value={
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": {"usedPercent": 2}},
            }), mock.patch.object(m, "notify"), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit) as exit1:
                m.cmd_toggle_mode(args)
            self.assertEqual(exit1.exception.code, 0)
            parsed = m.tomllib.loads(config.read_text())
            self.assertEqual(parsed["model_provider"], "cli_proxy")
            st = m.load_state(state)
            self.assertEqual(st["mode"], "external")
            self.assertTrue(st["manual_toggle"])

            # Verify that watch_quota respects manual_toggle and does not auto-revert
            watch_args = m.parser().parse_args(["watch-quota", "--once", "--state", str(state)])
            with mock.patch.object(m, "read_codex_rate_limits", return_value={
                "ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 0}},
            }):
                stopped = m.watch_quota(watch_args)
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(stopped["reason"], "manual toggle mode override")
            self.assertEqual(m.tomllib.loads(config.read_text())["model_provider"], "cli_proxy")

    def test_external_catalog_hides_native_models(self):
        m = load_module()
        source = {
            "models": [
                {"slug": "gpt-5.6-sol", "visibility": "list", "supported_in_api": True},
                {"slug": "gemini-3.8-flash-high", "visibility": "list", "supported_in_api": True},
                {"slug": "deepseek-v4", "visibility": "list", "supported_in_api": True},
            ]
        }
        result = m.build_external_catalog(source, {"gemini-3.8-flash-high", "deepseek-v4"})
        by_slug = {x["slug"]: x for x in result["models"]}
        self.assertEqual(by_slug["gpt-5.6-sol"]["visibility"], "hide")
        self.assertEqual(by_slug["gemini-3.8-flash-high"]["visibility"], "list")
        self.assertEqual(by_slug["deepseek-v4"]["visibility"], "list")

    def test_external_catalog_filters_ghost_models(self):
        m = load_module()
        source = {
            "models": [
                {"slug": "gpt-5.6-sol", "visibility": "list", "supported_in_api": True},
                {"slug": "gemini-3.8-flash-high", "visibility": "list", "supported_in_api": True},
                {"slug": "gemini-3.6-flash", "visibility": "list", "supported_in_api": True},
                {"slug": "gemini-3.1-pro", "visibility": "list", "supported_in_api": True},
            ]
        }
        allowed = {"gemini-3.8-flash-high"}
        result = m.build_external_catalog(source, set(), allowed_external_slugs=allowed)
        by_slug = {x["slug"]: x for x in result["models"]}
        self.assertEqual(by_slug["gemini-3.8-flash-high"]["visibility"], "list")
        self.assertEqual(by_slug["gemini-3.6-flash"]["visibility"], "hide")
        self.assertEqual(by_slug["gemini-3.1-pro"]["visibility"], "hide")
        self.assertEqual(by_slug["gpt-5.6-sol"]["visibility"], "hide")

    def test_openai_config_capture_and_restore_round_trip(self):
        m = load_module()
        bridge = __import__("bridge")
        original = (
            'model_provider = "openai"\n'
            'model = "gpt-5.6-sol"\n'
            'openai_base_url = "http://127.0.0.1:8318/v1"\n'
            'model_catalog_json = "/tmp/catalog.json"\n\n'
            '[mcp_servers.demo]\n'
            'type = "http"\n'
            'url = "https://example.test"\n'
        )
        cfg = bridge.tomllib.loads(original)
        saved = m.capture_openai_config(cfg)
        changed = bridge.replace_top_scalar(original, "model_provider", "cli_proxy")
        changed = bridge.replace_top_scalar(changed, "model", "gemini-3.8-flash-high")
        restored = changed
        for key in ("model_provider", "model", "openai_base_url", "model_catalog_json"):
            restored = m._restore_root_value(restored, key, saved[key])
        parsed = bridge.tomllib.loads(restored)
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["openai_base_url"], "http://127.0.0.1:8318/v1")
        self.assertEqual(parsed["mcp_servers"]["demo"]["url"], "https://example.test")

    def test_toggle_mode_succeeds_even_when_gpt_quota_not_exhausted(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            profile = root / "cli-proxy.config.toml"
            catalog = root / "catalog.json"
            state = root / "quota.json"
            ext_catalog = root / "external.json"

            config.write_text("""model_provider = "openai"
model = "gpt-5.6-sol"
""", encoding="utf-8")
            profile.write_text("""[model_providers.cli_proxy]
name = "CLI Proxy"
base_url = "http://127.0.0.1:8318/v1"
wire_api = "responses"
""", encoding="utf-8")
            catalog.write_text(json.dumps({"models": [{"slug": "gemini-3.8-flash-high", "visibility": "list", "priority": 1}]}), encoding="utf-8")

            healthy_quota = {
                "blocked": False,
                "recovered": True,
                "ordinary_usage_allowed": True,
                "spend_control_reached": False,
                "windows": [{"name": "primary", "used_percent": 2, "exhausted": False}],
                "blocking_windows": [],
                "expected_recovery_at": None,
            }

            args = m.parser().parse_args([
                "toggle", "--apply", "--no-watch",
                "--config", str(config),
                "--profile-config", str(profile),
                "--catalog", str(catalog),
                "--external-catalog", str(ext_catalog),
                "--state", str(state),
            ])
            with mock.patch.object(m, "refresh_state", return_value=(m.load_state(state), healthy_quota)), \
                 mock.patch.object(m, "restart_codex", return_value=True):
                result = m.apply_external_mode(args)

            self.assertEqual(result["mode"], "external")
            self.assertEqual(result["status"], "applied")
            st = m.load_state(state)
            self.assertEqual(st["mode"], "external")
            self.assertTrue(st["manual_toggle"])

            # Verify auto_mode_step does NOT revert when manual_toggle is held
            auto_args = m.parser().parse_args(["watch-auto", "--once", "--state", str(state)])
            st["auto_monitor"] = {"enabled": True, "config": str(config), "baseline": st["openai_config"], "poll_seconds": 120}
            m.save_state(state, st)
            with mock.patch.object(m, "refresh_state", return_value=(st, healthy_quota)), \
                 mock.patch.object(m, "restore_openai_mode") as mock_restore:
                step_res = m.auto_mode_step(auto_args)
                self.assertEqual(step_res["status"], "waiting")
                mock_restore.assert_not_called()

    def test_watcher_waits_until_backend_confirms_recovery(self):
        m = load_module()
        blocked = {
            "ordinary_usage_allowed": False,
            "expected_recovery_at": 1,
            "recovered": False,
        }
        self.assertIn(m.watcher_sleep_seconds(blocked, 0), (45, 120))
        unknown = None
        self.assertEqual(m.watcher_sleep_seconds(unknown), 900)


if __name__ == "__main__":
    unittest.main()
