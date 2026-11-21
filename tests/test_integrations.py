"""Cross-entry deduplication, CLI/MCP transport, and safe global configuration."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import integrations
import notify_bridge as bridge
import notification_queue as queue
import test_codex_ntfy as fixtures


class BridgeChecks(fixtures.BehaviorChecks):
    # Reuse temporary rollout fixtures. load_tests selects only additional checks.
    def native(self, thread="thread-1", turn="turn-1"):
        return {"type": "agent-turn-complete", "thread-id": thread, "turn-id": turn,
                "cwd": "PRIVATE", "last-assistant-message": "PRIVATE CODE"}

    def test_hook_and_watcher_dedup_in_both_orders(self):
        path = self.sessions / "rollout-thread-1.jsonl"
        self.write(path, self.meta())
        self.start()
        bridge.codex_hook(self.config, self.native())
        self.assertEqual(len(self.jobs()), 0)  # callback cannot bypass final reply
        context, final = self.final_records()
        self.write(path, context, final, self.quota(), self.done(), with_final=False)
        bridge.codex_hook(self.config, self.native())
        self.assertIn('5 小时剩余 75% · 7 天剩余 71%', self.jobs()[0]["payload"])
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)
        context, final = self.final_records("turn-2")
        self.write(path, context, self.quota(100, 40), final, self.done("turn-2"), with_final=False)
        self.watcher.scan()
        bridge.codex_hook(self.config, self.native(turn="turn-2"))
        self.assertEqual(len(self.jobs()), 2)
        self.assertIn('5 小时剩余 0% · 7 天剩余 60%', self.jobs()[1]["payload"])
        self.assertTrue(all("PRIVATE" not in job["payload"] for job in self.jobs()))

    def test_hook_excludes_subagents_unknown_and_non_completion(self):
        self.start()
        self.write(self.sessions / "rollout-child.jsonl", self.meta({"subagent": {"depth": 1}}, "child"))
        bridge.codex_hook(self.config, self.native(thread="child"))
        bridge.codex_hook(self.config, self.native(thread="missing"))
        bridge.codex_hook(self.config, {**self.native(), "type": "approval-requested"})
        self.assertEqual(self.jobs(), [])

    def test_cli_opt_in_and_desktop_application_labels(self):
        self.config["sources"] = ["vscode", "cli"]
        self.start()
        self.write(self.sessions / "rollout-cli.jsonl", self.meta("cli", "cli"), self.done())
        meta = self.meta(ident="desktop")
        meta["payload"]["originator"] = "codex_work_desktop"
        self.write(self.sessions / "rollout-desktop.jsonl", meta)
        self.watcher.scan()  # store cursor, then exercise resumed metadata lookup
        self.write(self.sessions / "rollout-desktop.jsonl", self.done())
        self.watcher.scan()
        self.assertEqual({json.loads(row["payload"])["title"] for row in self.jobs()}, {"Codex", "ChatGPT"})

    def test_multiple_producers_while_watcher_running(self):
        self.start()
        first = queue.enqueue(self.config, "token-plan", "turn-1")
        again = queue.enqueue(self.config, "token-plan", "turn-1")
        queue.enqueue(self.config, "zcodium", "turn-1")
        self.assertTrue(first["queued"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.jobs()), 2)
        self.watcher.close()
        self.watcher = None
        self.start()
        with patch.object(fixtures.module, "publish", return_value="ack") as send:
            self.watcher.deliver_one()
            self.watcher.deliver_one()
            self.watcher.deliver_one()
            self.assertEqual(send.call_count, 2)

    def test_cli_executes_without_network_or_daemon_lock(self):
        self.start()
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps({key: str(value) if isinstance(value, Path) else value
                                         for key, value in self.config.items()}))
        command = [sys.executable, str(integrations.ROOT / "notify_bridge.py"), "--config", str(config_path),
                   "complete", "--app", "token-plan", "--event-id", "test-turn"]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(json.loads(result.stdout)["phone_received"], "unknown")
        self.assertEqual(len(self.jobs()), 1)

    def test_mcp_stdio_handshake_tool_and_repeat_dedup(self):
        self.start()
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "notify_task_complete", "arguments": {"app": "zcodium", "event_id": "t1"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "notify_task_complete", "arguments": {"app": "zcodium", "event_id": "t1"}}},
        ]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(json.dumps(r) for r in requests))), patch("sys.stdout", output):
            bridge.serve_mcp(self.config)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(json.loads(responses[3]["result"]["content"][0]["text"])["duplicate"])
        self.assertEqual(len(self.jobs()), 1)

    def test_mcp_rejects_content_fields_without_exporting_them(self):
        self.start()
        result = bridge.mcp_result(self.config, "tools/call", {"name": "notify_task_complete",
            "arguments": {"app": "zcodium", "event_id": "t1", "message": "PRIVATE"}})
        self.assertTrue(result["isError"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.jobs(), [])

    def test_invalid_ids_cannot_collide_with_codex_event_keys(self):
        for bad in ["", "t:1", "\n", "x" * 201, None]:
            with self.assertRaises(ValueError):
                queue.enqueue(self.config, "generic", bad)


class ConfigurationChecks(unittest.TestCase):
    def test_toml_preserved_and_registration_idempotent(self):
        original = 'model = "existing"\n# keep comment\n[projects."/tmp/project"]\ntrust_level = "trusted"\n'
        once = integrations.codex_config(original)
        twice = integrations.codex_config(once)
        self.assertEqual(once, twice)
        self.assertTrue(once.endswith(original))
        self.assertEqual(integrations.remove_block(once, integrations.TOML_BEGIN, integrations.TOML_END), original)
        # System Python can be 3.10; Python 3.11+ adds an independent TOML parser.
        try:
            import tomllib
        except ImportError:
            return
        self.assertEqual(tomllib.loads(once)["model"], "existing")

    def test_existing_unrelated_notify_preserved(self):
        original = 'notify = ["my-existing-notifier"]\n[features]\n'
        with self.assertRaises(ValueError):
            integrations.codex_config(original)

    def test_global_instructions_preserved_and_idempotent(self):
        original = "用户原来的全局习惯。\n"
        once = integrations.codex_instructions(original)
        self.assertEqual(once, integrations.codex_instructions(once))
        self.assertTrue(once.startswith(original))
        self.assertEqual(once.count(integrations.MD_BEGIN), 1)

    def test_install_repeat_and_remove_preserve_existing_settings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "notify_bridge.py").write_text("#!/usr/bin/python3\n")
            (project / "config.json").write_text('{"topic":"test","sources":["vscode"]}')
            codex = home / ".codex"
            codex.mkdir(parents=True)
            original = 'model = "unchanged"\n[features]\nexample = true\n'
            (codex / "config.toml").write_text(original)
            (codex / "AGENTS.override.md").write_text("Existing override.\n")
            with patch.object(integrations, "ROOT", project), patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"CODEX_HOME": str(codex)}), patch("sys.stdout", io.StringIO()):
                integrations.install()
                content = (codex / "config.toml").read_text()
                count = len(list((project / ".state/backups").rglob("*")))
                integrations.install()
                self.assertEqual(content, (codex / "config.toml").read_text())
                self.assertEqual(count, len(list((project / ".state/backups").rglob("*"))))
                self.assertFalse((codex / "AGENTS.md").exists())
                integrations.uninstall()
                self.assertEqual((codex / "config.toml").read_text(), original)
                self.assertEqual((codex / "AGENTS.override.md").read_text().strip(), "Existing override.")
                self.assertFalse((home / ".local/bin/ai-notify").exists())


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(BridgeChecks(name) for name in sorted(BridgeChecks.__dict__) if name.startswith("test_"))
    suite.addTests(loader.loadTestsFromTestCase(ConfigurationChecks))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
