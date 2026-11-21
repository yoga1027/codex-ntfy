"""Project indexing, visible history, and completion gating for Claude sessions."""
from contextlib import closing
import datetime as dt
import base64
import json
from pathlib import Path
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from account_usage import claude_usage
from claude_sessions import ClaudeSessions, PREFIX
from claude_stop_hook import stop
from notification_queue import open_database
from phone_conversations import PhoneConversations


class ClaudeSessionsChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.config = {
            "claude_home": self.base / "claude", "sessions_dir": self.base / "codex" / "sessions",
            "state_dir": self.base / "state", "topic": "fixture", "priority": 4, "transport": "tailscale",
            "max_notification_age_seconds": 300,
        }
        self.phone = PhoneConversations(self.config)
        self.raw_id = str(uuid.uuid4())
        self.id = PREFIX + self.raw_id
        self.turn = str(uuid.uuid4())
        self.cwd = self.base / "example"
        self.cwd.mkdir()
        path = self.config["claude_home"] / "projects" / "example"
        path.mkdir(parents=True)
        self.path = path / (self.raw_id + ".jsonl")

    def write_turn(self, *, final=True):
        moment = time.time() - 2
        records = [
            {"type": "user", "uuid": self.turn, "message": {"content": "我要修改文件"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "PRIVATE"}]}},
            {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "正在检查"},
                {"type": "tool_use", "id": "edit", "name": "Edit", "input": {"file_path": "a.py"}},
            ]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "edit",
                                                     "content": "PRIVATE"}]}},
        ]
        if final:
            records.append({"type": "assistant", "uuid": str(uuid.uuid4()),
                            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "已完成"}]}})
        with self.path.open("w", encoding="utf-8") as stream:
            for item in records:
                item["timestamp"] = dt.datetime.fromtimestamp(moment, dt.timezone.utc).isoformat()
                stream.write(json.dumps(item) + "\n")
        return moment

    def test_export_filters_tool_output_and_bumps_history_for_quota(self):
        moment = self.write_turn()
        first = self.phone.thread_page(self.id, "", None)
        turn = first["turns"][0]
        self.assertEqual(turn["id"], self.turn)
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["files_edited"], 1)
        self.assertEqual([message["kind"] for message in turn["messages"]], ["user", "progress", "assistant"])
        self.assertNotIn("PRIVATE", str(turn))
        reset = int(moment) + 300
        with closing(open_database(self.config)) as db, db:
            db.execute("INSERT INTO claude_usage VALUES (?, ?, ?, ?)",
                       (self.id, self.turn, time.time_ns(), json.dumps([[300, 75, reset]])))
        changed = self.phone.thread_page(self.id, first["revision"], None)
        self.assertNotEqual(first["revision"], changed["revision"])
        self.assertEqual(changed["turns"][0]["quota_reset_ms"], {"300": reset * 1000})
        self.assertEqual(changed["turns"][0]["quota_remaining_percent"], {"300": 75})

    def test_native_stop_is_deduplicated_and_requires_visible_final_reply(self):
        event = {"hook_event_name": "Stop", "session_id": self.raw_id, "transcript_path": str(self.path),
                 "last_assistant_message": "已完成"}
        self.write_turn(final=False)
        stop(self.config, event)
        with closing(open_database(self.config)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
            self.write_turn()
            with patch.object(ClaudeSessions, "sdk", return_value=SimpleNamespace(custom_title="我的 Claude", summary=None)):
                stop(self.config, event)
                stop(self.config, event)
            rows = db.execute("SELECT event_key, payload FROM notifications").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_key"], self.id + ":" + self.turn)
            self.assertEqual(json.loads(rows[0]["payload"])["title"], "claude 我的 Claude")

    def test_native_catalog_and_phone_archive_share_the_underlying_session(self):
        self.write_turn()
        info = SimpleNamespace(session_id=self.raw_id, custom_title="我的 Claude", summary=None, cwd=str(self.cwd))
        with closing(open_database(self.config)) as db, patch.object(self.phone.claude, "sdk", return_value=[info]):
            first = self.phone.catalog(db)["threads"][0]
            self.assertEqual(first["title"], "我的 Claude")
            self.assertEqual(first["project"], "Claude · example")
            self.assertFalse(first["archived"])
            original = self.path.read_bytes()
            self.assertEqual(self.phone.set_archive({"thread_id": self.id}, archived=True), {"status": "archived"})
            self.assertTrue(self.phone.catalog(db)["threads"][0]["archived"])
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.phone.move({"thread_id": self.id, "project_id": "codex-project"}),
                             {"status": "unavailable"})

    def test_usage_reads_the_desktop_profile_and_allows_the_usage_request(self):
        reply = SimpleNamespace(stdout=b'{"response":{"request_id":"get_usage","subtype":"success","response":{}}}')
        with patch.dict(os.environ, {}, clear=True), patch("claude_sessions.subprocess.run", return_value=reply) as cli:
            for home in (Path.home() / ".claude", self.base / "custom-profile"):
                claude = ClaudeSessions({**self.config, "claude_home": home})
                claude.control_info(home, "get_usage")
                env = cli.call_args.kwargs["env"]
                self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), None if home == Path.home() / ".claude" else str(home))
                self.assertNotIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", env)
                self.assertIn(b'"skip_behaviors": true', cli.call_args.kwargs["input"])

    def test_cached_account_usage_has_its_own_timestamp_and_matches_the_account(self):
        now = 1900000000
        cache = {"accountUuid": "mine", "fetchedAtMs": (now - 60) * 1000, "utilization": {
            "five_hour": {"utilization": 8, "resets_at": now - 1},
            "seven_day": {"utilization": 21, "resets_at": now + 10000}}}
        settings = self.phone.claude.home / ".claude.json"
        with patch.object(self.phone.claude, "control_info", return_value={"rate_limits": None}), \
                patch("account_usage.time.time", return_value=now):
            for account in ("mine", "another"):
                settings.write_text(json.dumps({"oauthAccount": {"accountUuid": account}, "cachedUsageUtilization": cache}))
                data = claude_usage(self.phone.claude)
                if account == "mine":
                    self.assertEqual(data["observed_at"], now - 60)
                    self.assertIsNone(data["300"])
                    self.assertEqual(data["10080"]["remaining"], 79)
                else:
                    self.assertEqual(data, {"unavailable": True})

    def test_attachment_reply_uses_claude_image_blocks(self):
        photo = b"\xff\xd8\xfffixture"
        image = base64.b64encode(photo).decode()
        request = {"thread_id": self.id, "request_id": str(uuid.uuid4()), "text": "看这张图",
                   "attachments": [{"kind": "image", "name": "photo.jpg", "mime": "image/jpeg", "data": image}]}
        with closing(open_database(self.config)) as db, \
                patch.object(self.phone.claude, "prepare", return_value=("ready", str(self.cwd), True)), \
                patch.object(self.phone.claude, "start", return_value=("accepted", "new-turn")) as start:
            self.assertEqual(self.phone.reply(db, request)["status"], "accepted")
            self.assertIn("photo.jpg", start.call_args.args[2])
            self.assertEqual(start.call_args.kwargs["images"], [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}])

    def test_recorded_quota_failure_updates_history_with_both_reset_times(self):
        self.write_turn(final=False)
        first = self.phone.thread_page(self.id, "", None)
        reset = int(time.time()) + 3600
        usage = {"300": {"remaining": 10, "reset": reset}, "10080": {"remaining": 0, "reset": reset + 3600}}
        with patch("account_usage.claude_usage", return_value=usage):
            self.phone.claude.record_failure(self.id, self.turn, "quota")
        later = self.phone.thread_page(self.id, first["revision"], None)
        self.assertEqual(later["turns"][0]["status"], "quota")
        self.assertEqual(later["turns"][0]["quota_reset_ms"], {"300": reset * 1000, "10080": (reset + 3600) * 1000})
        self.assertEqual(later["turns"][0]["quota_remaining_percent"], {"300": 10, "10080": 0})

    def test_selected_model_is_forwarded_to_the_claude_workspace(self):
        options = [{"id": "sonnet", "label": "Sonnet", "efforts": ["medium", "high"]}]
        with closing(open_database(self.config)) as db, \
                patch.object(self.phone.claude, "prepare", return_value=("ready", str(self.cwd), True)), \
                patch.object(self.phone.claude, "models", return_value=options) as load, \
                patch.object(self.phone.claude, "start", return_value=("accepted", "claude-turn")) as start:
            request = {"thread_id": self.id, "request_id": str(uuid.uuid4()), "text": "继续",
                       "model": "sonnet", "effort": "high"}
            self.assertEqual(self.phone.reply(db, request), {"status": "accepted", "turn_id": "claude-turn"})
            self.assertEqual(self.phone.reply(db, request), {"status": "accepted", "turn_id": "claude-turn"})
            load.assert_called_once_with(str(self.cwd))
            self.assertEqual(start.call_count, 1)
            self.assertEqual(start.call_args.kwargs, {"model": "sonnet", "effort": "high"})
