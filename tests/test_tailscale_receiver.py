"""Exercise the phone pull protocol against the real SQLite outbox."""
from http.client import HTTPConnection, RemoteDisconnected
import base64
from pathlib import Path
import datetime as dt
import json
import threading
import sqlite3
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import uuid
from zoneinfo import ZoneInfo

import notification_queue as queue
import notify_bridge as bridge
import tailscale_receiver as receiver
import test_codex_ntfy as fixtures


class TailscaleChecks(fixtures.BehaviorChecks):
    def setUp(self):
        super().setUp()
        self.config.update(transport="tailscale", max_notification_age_seconds=300,
                           project_manager_db=self.base / "projects.sqlite", tailscale={
            "listen_address": "127.0.0.1", "peer_address": "127.0.0.1", "port": 0,
        })
        self.start()
        self.server = receiver.ReceiverServer(self.config, interface=None, wait_seconds=0.05)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, *, source="127.0.0.1", headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2,
                                source_address=(source, 0))
        try:
            client.request("GET" if body is None else "POST", path, body, headers or {})
            reply = client.getresponse()
            return reply.status, reply.read()
        finally:
            client.close()

    def test_pulls_real_completion_with_quota_until_phone_acknowledges(self):
        path = self.sessions / "rollout-live.jsonl"
        index = self.base / "session_index.jsonl"
        index.write_text(
            '{"id":"thread-1","thread_name":"旧标题"}\n'
            'invalid record\n'
            '{"id":"thread-1","thread_name":"手机通知 App"}\n',
            encoding="utf-8",
        )
        context, final = self.final_records()
        done = self.done()
        self.write(path, self.meta(), context, self.quota(), final, done, with_final=False)
        self.watcher.scan()
        with patch.object(fixtures.module, "publish") as ntfy, patch("oppo_sender.publish") as oppo, \
                patch("qq_sender.publish") as qq:
            self.watcher.deliver_one()
            ntfy.assert_not_called()
            oppo.assert_not_called()
            qq.assert_not_called()

        status, body = self.request("/v1/reminders")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/v1/reminders"), (200, body))
        message = json.loads(body)
        self.assertEqual(message["event_id"], "thread-1:turn-1")
        ended = dt.datetime.fromtimestamp(fixtures.module.event_time(done), ZoneInfo("Asia/Shanghai"))
        self.assertEqual(message["title"], "codex 手机通知 App")
        self.assertEqual(message["summary"], f"完成 · {ended:%Y-%m-%d %H:%M:%S} · 5h75% 7天71%")
        self.assertEqual(message["message"],
                         f"对话完成\n结束 {ended:%Y-%m-%d %H:%M:%S}（北京时间）\n5h 75% · 7天 71%")
        self.assertNotIn("PRIVATE", message["message"])
        row = self.jobs()[0]
        self.assertEqual(message["expires_at_ms"], int((row["completed_at"] + 300) * 1000))
        self.assertEqual(row["message_id"], receiver.OFFERED)
        self.assertIsNone(row["sent_at"])
        ack = json.dumps({"event_id": message["event_id"]})
        self.assertEqual(self.request("/v1/ack", ack)[0], 204)
        self.assertEqual(self.request("/v1/ack", ack)[0], 204)
        self.assertEqual(self.request("/v1/reminders")[0], 204)
        self.assertEqual(self.jobs()[0]["message_id"], receiver.POSTED)
        self.assertIsNotNone(self.jobs()[0]["sent_at"])

    def test_quota_interruption_without_final_survives_restart_and_deduplicates(self):
        path = self.sessions / "rollout-thread-1.jsonl"
        context, _ = self.final_records()
        self.write(path, self.meta(), context, self.quota(42, 100))
        self.watcher.scan()
        self.watcher.close()
        self.start()
        done = self.done()
        done["payload"]["error"] = {"codex_error_info": "usage_limit_exceeded", "message": "PRIVATE"}
        self.write(path, done, done, with_final=False)
        bridge.codex_hook(self.config, {"type": "agent-turn-complete",
                          "thread-id": "thread-1", "turn-id": "turn-1"})
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)
        status, body = self.request("/v1/reminders")
        self.assertEqual(status, 200)
        message = json.loads(body)
        self.assertEqual(message["title"], "codex 未命名对话")
        self.assertTrue(message["summary"].startswith("额度中断 · "))
        self.assertTrue(message["summary"].endswith(" · 5h58% 7天0%"))
        self.assertTrue(message["message"].startswith("额度不足中断\n结束 "))
        self.assertTrue(message["message"].endswith("\n5h 58% · 7天 0%"))
        self.assertNotIn("PRIVATE", message["message"])

    def test_quota_snapshot_or_error_without_matching_terminal_does_not_notify(self):
        path = self.sessions / "rollout-live.jsonl"
        context, final = self.final_records()
        mismatch = self.done("other-turn")
        mismatch["payload"]["error"] = {"codex_error_info": "usage_limit_exceeded"}
        failure = self.done()
        failure["payload"]["error"] = {"codex_error_info": "server_overloaded"}
        self.write(path, self.meta(), context, self.quota(100, 85),
                   self.record("event_msg", {"type": "error", "codex_error_info": "usage_limit_exceeded"}),
                   mismatch, final, failure,
                   self.record("event_msg", {"type": "turn_aborted", "turn_id": "turn-1"}),
                   with_final=False)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_missing_or_reset_quota_displays_placeholder_in_fixed_order(self):
        path = self.sessions / "rollout-live.jsonl"
        context, final = self.final_records()
        done = self.done()
        reset = int(fixtures.module.event_time(done)) - 1
        self.write(path, self.meta(), context, self.quota(resets_at=reset), final, done, with_final=False)
        self.watcher.scan()
        status, body = self.request("/v1/reminders")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["summary"].endswith(" · 5h— 7天71%"))

    def test_each_remaining_quota_shows_its_own_beijing_reset_time(self):
        path = self.sessions / "rollout-thread-1.jsonl"
        context, final = self.final_records()
        completed = self.done()
        ended = dt.datetime.fromtimestamp(fixtures.module.event_time(completed), ZoneInfo("Asia/Shanghai"))
        primary = ended + dt.timedelta(minutes=30)
        secondary = ended + dt.timedelta(days=4, minutes=15)
        quota = self.quota(resets_at=int(primary.timestamp()))
        quota["payload"]["rate_limits"]["secondary"]["resets_at"] = int(secondary.timestamp())
        self.write(path, self.meta(), context, quota, final)
        self.watcher.scan()
        self.watcher.close()
        self.start()
        self.write(path, completed, with_final=False)
        bridge.codex_hook(self.config, {"type": "agent-turn-complete",
                          "thread-id": "thread-1", "turn-id": "turn-1"})
        self.watcher.scan()
        status, body = self.request("/v1/reminders")
        self.assertEqual(status, 200)
        message = json.loads(body)
        self.assertIn(f"5h 75% · 重置 {primary:%m-%d %H:%M}", message["message"])
        self.assertIn(f"7天 71% · 重置 {secondary:%m-%d %H:%M}", message["message"])
        short = f"{primary:%H:%M}" if primary.date() == ended.date() else f"{primary:%m/%d %H:%M}"
        self.assertIn(f"5h75%({short}) 7天71%({secondary:%m/%d %H:%M})", message["summary"])
        self.assertTrue(message["summary"].startswith(f"完成 · {ended:%Y-%m-%d %H:%M:%S} · "))
        self.assertEqual(message["message"].splitlines()[:2], [
            "对话完成", f"结束 {ended:%Y-%m-%d %H:%M:%S}（北京时间）",
        ])

    def test_expires_offer_and_skips_other_transports_in_ambiguous_state(self):
        now = time.time()
        queue.enqueue(self.config, "generic", "old-channel", completed_at=now - 50)
        queue.enqueue(self.config, "generic", "expires", completed_at=now - 290)
        with self.watcher.db:
            self.watcher.db.execute(
                "UPDATE notifications SET message_id='oppo:attempting' WHERE event_key LIKE '%old-channel'")
        status, body = self.request("/v1/reminders")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["event_id"], "external:generic:expires")
        with patch("tailscale_receiver.time.time", return_value=now + 11):
            self.assertEqual(self.request("/v1/reminders")[0], 204)
            self.assertEqual(self.request("/v1/ack", json.dumps({"event_id": "external:generic:expires"}))[0], 204)
            self.watcher.deliver_one()
        rows = {r["event_key"]: r for r in self.jobs()}
        self.assertIsNone(rows["external:generic:expires"]["sent_at"])
        self.assertIsNotNone(rows["external:generic:expires"]["expired_at"])
        self.assertEqual(rows["external:generic:old-channel"]["message_id"], "oppo:attempting")

    def test_only_configured_phone_can_open_the_service(self):
        self.assertEqual(self.request("/health"), (200, b"Tailscale link OK\n"))
        with self.assertRaises((RemoteDisconnected, ConnectionResetError)):
            self.request("/health", source="127.0.0.2")

    def test_current_account_usage_includes_available_cards_only_and_is_phone_only(self):
        phone = self.server.conversations
        live = {"rateLimitsByLimitId": {"codex": {
            "secondary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": 2000000000},
            "primary": {"windowDurationMins": 10080, "usedPercent": 87, "resetsAt": 2100000000}}},
            "rateLimitResetCredits": {"availableCount": 3, "credits": [
                {"status": "available", "expiresAt": 2010000000, "id": "PRIVATE"},
                {"status": "redeemed", "expiresAt": 1900000000},
                {"status": "available", "expiresAt": None}]}}
        with patch.object(phone.app_server, "request", return_value=live) as read, \
                patch.object(phone.claude, "control_info", side_effect=OSError("offline")):
            self.assertEqual(self.request("/v1/account/usage", headers={"Origin": "https://example.com"})[0], 403)
            status, body = self.request("/v1/account/usage")
        self.assertEqual(status, 200)
        read.assert_called_once_with("account/rateLimits/read", {})
        self.assertNotIn(b"PRIVATE", body)
        data = json.loads(body)
        self.assertEqual(data["codex"], {"300": {"remaining": 75, "reset": 2000000000},
                                        "10080": {"remaining": 13, "reset": 2100000000},
                                        "card_count": 3, "expiries": [2010000000, None]})
        self.assertTrue(data["claude"]["unavailable"])

    def test_codex_usage_fallback_verifies_account_and_ignores_reset_windows_and_expired_cards(self):
        phone = self.server.conversations
        now = int(time.time())
        live = {"accountId": "work", "rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 30, "resetsAt": now + 30},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 10, "resetsAt": now + 9000}},
            "rateLimitResetCredits": {"availableCount": 1, "credits": [{"status": "available", "expiresAt": now + 30}]}}
        with patch.object(phone.claude, "control_info", side_effect=OSError("offline")), \
                patch.object(phone.app_server, "request", return_value=live), \
                patch("account_usage.time.time", return_value=now):
            self.assertEqual(json.loads(self.request("/v1/account/usage")[1])["codex"]["card_count"], 1)
        for account in ("work", "other"):
            profile = {"account": {"type": "chatgpt"}, "workspaceRouting": {"chatgptAccountId": account}}
            with patch.object(phone.claude, "control_info", side_effect=OSError("offline")), \
                    patch.object(phone.app_server, "request", side_effect=[TimeoutError(), profile]), \
                    patch("account_usage.time.time", return_value=now + 60):
                result = json.loads(self.request("/v1/account/usage")[1])["codex"]
            if account == "work":
                self.assertEqual(result, {"observed_at": now, "10080": {"remaining": 90, "reset": now + 9000}})
            else:
                self.assertEqual(result, {"unavailable": True})

    def test_claude_usage_without_codex_preserves_zero_and_unknowns(self):
        phone = self.server.conversations
        live = {"rate_limits": {"five_hour": {"utilization": 100, "resets_at": None},
                                "seven_day": {"utilization": 2.5, "resets_at": "2035-01-01T08:00:00+08:00"}}}
        with patch.object(phone.app_server, "request", side_effect=OSError("offline")), \
                patch.object(phone.claude, "control_info", return_value=live):
            status, body = self.request("/v1/account/usage")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["codex"]["unavailable"])
        self.assertEqual(data["claude"], {"300": {"remaining": 0, "reset": None},
                                         "10080": {"remaining": 97.5, "reset": 2051222400}})

    def delivered_conversation(self):
        path = self.sessions / "rollout-2026-11-20T15-00-00-thread-1.jsonl"
        context, final = self.final_records()
        done = self.done()
        done["payload"]["duration_ms"] = 168000
        self.write(path, self.meta(), context,
                   self.record("event_msg", {"type": "agent_message", "phase": "commentary",
                                              "message": "正在核对。"}),
                   self.record("response_item", {"type": "message", "role": "assistant", "phase": "commentary",
                                                 "content": [{"type": "output_text", "text": "正在核对。"}]}),
                   self.record("event_msg", {"type": "patch_apply_end", "turn_id": "turn-1", "success": True,
                                              "changes": {"one": {}, "two": {}}}),
                   self.record("event_msg", {"type": "patch_apply_end", "turn_id": "turn-1", "success": True,
                                              "changes": {"two": {}, "three": {}}}),
                   self.record("event_msg", {"type": "agent_reasoning", "text": "PRIVATE reasoning"}),
                   self.record("response_item", {"type": "message", "role": "developer",
                                                 "content": [{"type": "input_text", "text": "PRIVATE instructions"}]}),
                   final, done, with_final=False)
        self.watcher.scan()
        event_id = "thread-1:turn-1"
        route = "/v1/conversation?" + urlencode({"event_id": event_id})
        self.assertEqual(self.request(route)[0], 404)
        self.assertEqual(self.request("/v1/reminders")[0], 200)
        self.assertEqual(self.request("/v1/ack", json.dumps({"event_id": event_id}))[0], 204)
        return path, event_id, route

    def test_turn_file_preview_counts_undo_redo_and_conflict(self):
        source = self.base / "model.py"
        source.write_text("header\nafter\nfooter\n")
        rollout = self.sessions / "rollout-2026-11-20-thread-1.jsonl"
        meta = self.meta()
        meta["payload"]["cwd"] = str(self.base)
        context, final = self.final_records()
        patch_event = self.record("event_msg", {
            "type": "patch_apply_end", "turn_id": "turn-1", "success": True,
            "changes": {"model.py": {"type": "update",
                                     "unified_diff": "@@ -1,3 +1,3 @@\n header\n-before\n+after\n footer\n"}}})
        self.write(rollout, meta, context, patch_event, final, self.done(), with_final=False)
        thread_url = "/v1/thread?thread_id=thread-1"
        self.assertEqual(json.loads(self.request(thread_url)[1])["turns"][0]["files"], [{
            "path": "model.py", "added": 1, "removed": 1, "line": 1, "replayable": True}])
        preview = "/v1/thread/file?" + urlencode({"thread_id": "thread-1", "path": str(source), "line": "2"})
        self.assertEqual(self.request(preview, headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(json.loads(self.request(preview)[1])["lines"][1], "after")
        blocked = "/v1/thread/file?" + urlencode({"thread_id": "thread-1", "path": "../model.py"})
        self.assertEqual(json.loads(self.request(blocked)[1])["status"], "unavailable")

        def change(action, path="model.py"):
            data = {"thread_id": "thread-1", "turn_id": "turn-1", "path": path, "action": action}
            code, body = self.request("/v1/thread/file/change", json.dumps(data),
                                      headers={"Content-Type": "application/json"})
            self.assertEqual(code, 200)
            return json.loads(body)["status"]

        self.assertEqual(change("undo", "unlisted.py"), "unavailable")
        self.assertEqual(change("redo"), "conflict")
        source.write_text("header\nexternal edit\nfooter\n")
        self.assertEqual(change("undo"), "conflict")
        self.assertIn("external edit", source.read_text())
        source.write_text("header\nafter\nfooter\n")
        self.assertEqual(change("undo"), "undone")
        self.assertEqual(change("undo"), "undone")
        self.assertEqual(source.read_text(), "header\nbefore\nfooter\n")
        self.assertEqual(change("redo"), "applied")
        self.assertEqual(source.read_text(), "header\nafter\nfooter\n")
        source.write_text(source.read_text() + "later\n")
        self.assertEqual(change("undo"), "conflict")
        self.assertTrue(source.read_text().endswith("later\n"))

    def test_delivered_conversation_recovers_text_edits_and_subsequent_turns(self):
        path, _, route = self.delivered_conversation()
        code, body = self.request(route)
        self.assertEqual(code, 200)
        snapshot = json.loads(body)
        anchor = snapshot["turns"][0]
        self.assertEqual(anchor["elapsed_ms"], 168000)
        self.assertEqual(anchor["files_edited"], 3)
        self.assertEqual(anchor["messages"], [
            {"kind": "progress", "text": "正在核对。"},
            {"kind": "assistant", "text": "Fixture final reply"},
        ])
        self.assertNotIn("PRIVATE", body.decode())
        self.assertEqual(self.request(route, headers={"Origin": "https://example.invalid"})[0], 403)
        same = route + "&" + urlencode({"revision": snapshot["revision"]})
        self.assertEqual(json.loads(self.request(same)[1]), {"unchanged": True})
        context, final = self.final_records("turn-2")
        user = self.record("event_msg", {"type": "user_message", "message": "继续"})
        self.write(path, context, user, final, self.done("turn-2"), with_final=False)
        later = json.loads(self.request(same)[1])["turns"][1]
        self.assertEqual(later["messages"][0], {"kind": "user", "text": "继续",
                                               "sent_at_ms": int(fixtures.module.event_time(user) * 1000)})

    def test_history_refreshes_old_cache_with_message_times_and_per_turn_resets(self):
        path = self.sessions / "rollout-2026-11-20T15-00-00-thread-1.jsonl"
        context, final = self.final_records()
        user = self.record("event_msg", {"type": "user_message", "message": "原消息"})
        user["timestamp"] = "2026-11-20T23:59:43.123Z"
        item = self.record("event_msg", {"type": "item_completed", "turn_id": "turn-1",
                                         "item": {"type": "userMessage", "text": "原消息全文"}})
        item["timestamp"] = "2026-11-20T23:59:44.567Z"
        done = self.done()
        done["timestamp"] = "2026-11-21T00:00:09.123Z"
        ended = int(fixtures.module.event_time(done))
        both = self.quota(resets_at=ended + 300)
        both["payload"]["rate_limits"]["secondary"]["resets_at"] = ended + 3600
        stale = self.quota(resets_at=ended - 1)
        later_context, later_final = self.final_records("turn-2")
        later_done = self.done("turn-2")
        later_done["timestamp"] = "2026-11-21T00:00:39.000Z"
        self.write(path, self.meta(), context, user, item, stale, both, final, done,
                   later_context, stale, later_final, later_done, with_final=False)

        stat = path.stat()
        legacy = f"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"
        route = "/v1/thread?thread_id=thread-1"
        page = json.loads(self.request(route + "&revision=" + legacy)[1])
        first, later = page["turns"]
        self.assertEqual(first["messages"][0], {"kind": "user", "text": "原消息全文",
                                               "sent_at_ms": int(fixtures.module.event_time(user) * 1000)})
        self.assertEqual(first["completed_at_ms"], int(fixtures.module.event_time(done) * 1000))
        self.assertEqual(first["quota_reset_ms"], {"300": (ended + 300) * 1000,
                                                  "10080": (ended + 3600) * 1000})
        self.assertEqual(later["quota_reset_ms"], {})
        self.assertEqual(first["quota_remaining_percent"], {"300": 75, "10080": 71})
        self.assertEqual(later["quota_remaining_percent"], {"10080": 71})
        self.assertEqual(json.loads(self.request(route + "&revision=" + page["revision"])[1]),
                         {"unchanged": True})

    def test_attachment_only_reply_passes_images_and_files_to_original_codex_turn(self):
        path = self.sessions / "rollout-attachments-thread-1.jsonl"
        self.write(path, self.meta())
        photo = b"\xff\xd8\xfffixture"
        file_data = b"fixture\n" * 12000
        request = {"thread_id": "thread-1", "request_id": str(uuid.uuid4()), "text": "", "attachments": [
            {"name": "photo.jpg", "kind": "image", "mime": "image/jpeg",
             "data": base64.b64encode(photo).decode()},
            {"name": "notes.txt", "kind": "file", "mime": "text/plain",
             "data": base64.b64encode(file_data).decode()},
        ]}
        headers = {"Content-Type": "application/json"}
        with patch("codex_ipc.start_turn", return_value=("accepted", "new-turn")) as start:
            for _ in range(2):
                status, body = self.request("/v1/reply", json.dumps(request), headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "accepted", "turn_id": "new-turn"})
            start.assert_called_once()
            self.assertEqual(start.call_args.args[1], "thread-1")
            directory = self.config["state_dir"] / "attachments" / request["request_id"]
            self.assertIn(str(directory / "2-notes.txt"), start.call_args.args[3])
            image, = start.call_args.kwargs["images"]
            self.assertEqual(image["type"], "localImage")
            self.assertEqual(Path(image["path"]).read_bytes(), photo)
            self.assertEqual((directory / "2-notes.txt").read_bytes(), file_data)
            request["attachments"][1]["data"] = base64.b64encode(b"changed").decode()
            self.assertEqual(self.request("/v1/reply", json.dumps(request), headers=headers)[0], 400)
            request["request_id"] = str(uuid.uuid4())
            request["attachments"][1]["name"] = "../../notes.txt"
            self.assertEqual(self.request("/v1/reply", json.dumps(request), headers=headers)[0], 400)

    def test_network_error_and_quota_remain_distinct_in_terminal_history(self):
        path = self.sessions / "rollout-endings-thread-1.jsonl"
        self.write(path, self.meta())
        reset = int(time.time()) + 3600
        for turn_id, error, hint in (("quota-turn", "usage_limit_exceeded", None),
                                     ("network-turn", "other", "stream_disconnected"),
                                     ("retry-turn", None, "network_error")):
            self.write(path, self.record("turn_context", {"turn_id": turn_id}))
            if turn_id == "quota-turn":
                quota = self.quota(resets_at=reset)
                quota["payload"]["rate_limits"]["secondary"]["resets_at"] = reset + 60
                self.write(path, quota)
            if hint:
                self.write(path, self.record("event_msg", {"type": "error", "turn_id": turn_id,
                                                           "codex_error_info": hint}))
            done = self.done(turn_id)
            if error:
                done["payload"]["error"] = {"codex_error_info": error}
            self.write(path, done)
        status, body = self.request("/v1/thread?thread_id=thread-1")
        self.assertEqual(status, 200)
        turns = json.loads(body)["turns"]
        self.assertEqual([t["status"] for t in turns], ["quota", "network", "completed"])
        self.assertEqual(turns[0]["quota_reset_ms"], {"300": reset * 1000, "10080": (reset + 60) * 1000})

    def test_reply_routes_once_to_delivered_original_and_waits_for_new_turn(self):
        path, event, _ = self.delivered_conversation()
        headers = {"Content-Type": "application/json"}
        request = {"event_id": event, "request_id": str(uuid.uuid4()), "text": "继续原对话"}
        with patch("codex_ipc.start_turn", return_value=("accepted", "new-turn")) as start:
            body = json.dumps(request)
            self.assertEqual(self.request("/v1/reply", body)[0], 403)
            self.assertEqual(self.request("/v1/reply", body, headers={**headers, "Origin": "https://example.invalid"})[0], 403)
            self.assertEqual(json.loads(self.request("/v1/reply", body, headers=headers)[1]),
                             {"status": "accepted", "turn_id": "new-turn"})
            self.assertEqual(json.loads(self.request("/v1/reply", body, headers=headers)[1])["status"], "accepted")
            self.assertEqual(self.request("/v1/reply", json.dumps({**request, "text": "different"}), headers=headers)[0], 400)
            start.assert_called_once_with(self.base / "ipc" / "ipc.sock", "thread-1",
                                          request["request_id"], request["text"])
            later = json.dumps({**request, "request_id": str(uuid.uuid4())})
            self.assertEqual(json.loads(self.request("/v1/reply", later, headers=headers)[1])["status"], "busy")
            self.write(path, self.record("turn_context", {"turn_id": "new-turn"}))
            self.assertEqual(json.loads(self.request("/v1/reply", later, headers=headers)[1])["status"], "busy")

    def test_thread_list_and_full_history_include_older_desktop_prompts(self):
        archive = self.base / "archived_sessions"
        archive.mkdir()
        active = self.sessions / "rollout-2026-11-20T15-00-00-thread-1.jsonl"
        archived = archive / "rollout-2026-11-19T15-00-00-archived.jsonl"
        other = self.sessions / "rollout-2026-11-17T15-00-00-unsupported.jsonl"
        index = self.base / "session_index.jsonl"
        index.write_text('{"id":"thread-1","thread_name":"电脑上的会话"}\n', encoding="utf-8")
        for ident, path in (("thread-1", active), ("archived", archived)):
            self.write(path, self.meta(ident=ident))
            for prompt, turn_id in (("第一次从电脑发送", "first"), ("第二次从电脑发送", "second")):
                context, final = self.final_records(turn_id)
                self.write(path, context,
                           self.record("event_msg", {"type": "user_message", "message": prompt}),
                           final, self.done(turn_id), with_final=False)
        self.write(other, self.meta(ident="unsupported", source={"subagent": {}}))

        status, body = self.request("/v1/threads")
        self.assertEqual(status, 200)
        threads = {entry["id"]: entry for entry in json.loads(body)["threads"]}
        self.assertEqual(set(threads), {"thread-1", "archived"})
        self.assertEqual(threads["thread-1"]["title"], "电脑上的会话")
        self.assertEqual(self.request("/v1/threads", headers={"Origin": "https://example.invalid"})[0], 403)
        for ident in threads:
            status, body = self.request("/v1/thread?" + urlencode({"thread_id": ident}))
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual([turn["id"] for turn in data["turns"]], ["first", "second"])
            self.assertEqual([turn["messages"][0]["text"] for turn in data["turns"]],
                             ["第一次从电脑发送", "第二次从电脑发送"])
            delta = urlencode({"thread_id": ident, "revision": data["revision"]})
            self.assertEqual(json.loads(self.request("/v1/thread?" + delta)[1]), {"unchanged": True})
        self.assertEqual(self.request("/v1/thread?thread_id=unsupported")[0], 404)

    def test_catalog_projects_use_manual_assignment_then_the_longest_directory(self):
        base = str(self.base)
        with sqlite3.connect(self.config["project_manager_db"]) as db:
            db.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE roots(path TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE assignments(thread_id TEXT PRIMARY KEY, project_id TEXT);
                """)
            db.executemany("INSERT INTO projects VALUES (?, ?)",
                           [("a", "总项目"), ("b", "子目录项目"), ("c", "手动选择")])
            db.executemany("INSERT INTO roots VALUES (?, ?)",
                           [(base + "/work", "a"), (base + "/work/sub", "b"), (base + "/custom", "c")])
            db.execute("INSERT INTO assignments VALUES (?, ?)", ("manual", "c"))
        for ident, cwd in (("manual", base + "/work/sub/deep"), ("nested", base + "/work/sub/deep"),
                           ("parent", base + "/work/other"), ("unmatched", base + "/workbench")):
            meta = self.meta(ident=ident)
            meta["payload"]["cwd"] = cwd
            self.write(self.sessions / f"rollout-2026-11-20T15-00-00-{ident}.jsonl", meta)
        status, body = self.request("/v1/threads")
        self.assertEqual(status, 200)
        self.assertEqual({entry["id"]: entry["project"] for entry in json.loads(body)["threads"]},
                         {"manual": "手动选择", "nested": "子目录项目", "parent": "总项目", "unmatched": "未归类"})

    def test_thread_pagination_stays_on_the_same_history_when_another_turn_arrives(self):
        path = self.sessions / "rollout-2026-11-20T15-00-00-thread-1.jsonl"
        self.write(path, self.meta())
        for index in range(34):
            self.write(path, self.done(f"turn-{index}"))
        first = json.loads(self.request("/v1/thread?thread_id=thread-1")[1])
        self.assertEqual(first["next_offset"], 32)
        self.write(path, self.done("turn-34"))
        query = urlencode({"thread_id": "thread-1", "revision": first["revision"], "offset": 32})
        last = json.loads(self.request("/v1/thread?" + query)[1])
        self.assertEqual([turn["id"] for turn in last["turns"]], ["turn-32", "turn-33"])
        self.assertEqual(last["next_offset"], -1)

    def test_reply_routes_to_an_indexed_thread_without_a_prior_reminder(self):
        path = self.sessions / "rollout-2026-11-20T15-00-00-thread-1.jsonl"
        self.write(path, self.meta(), self.done())
        request = {"thread_id": "thread-1", "request_id": str(uuid.uuid4()), "text": "在原线程回复"}
        with patch("codex_ipc.start_turn", return_value=("accepted", "new-turn")) as start:
            _, body = self.request("/v1/reply", json.dumps(request), headers={"Content-Type": "application/json"})
        self.assertEqual(json.loads(body)["status"], "accepted")
        start.assert_called_once_with(self.base / "ipc" / "ipc.sock", "thread-1",
                                      request["request_id"], request["text"])

    def test_projects_syncs_desktop_groups_and_archives(self):
        root = self.base / "workspace"
        with sqlite3.connect(self.config["project_manager_db"]) as db:
            db.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE roots(path TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE assignments(thread_id TEXT PRIMARY KEY, project_id TEXT);
                INSERT INTO projects VALUES ('alpha', 'Alpha'), ('other', 'Other');
                INSERT INTO assignments VALUES ('manual', 'other');
            """)
            db.executemany("INSERT INTO roots VALUES (?, ?)", [(str(root), "alpha"),
                           (str(self.base / "other"), "other")])
        archive = self.base / "archived_sessions"
        archive.mkdir()
        meta = self.meta(ident="manual", source="cli")
        meta["payload"]["cwd"] = str(root)
        self.write(archive / "rollout-old-manual.jsonl", meta, self.done())
        indexed = {"active": {"id": "active", "cwd": str(root / "nested"), "name": "新名称", "archived": False},
                   "outside": {"id": "outside", "cwd": str(self.base), "archived": False}}
        with patch.object(self.server.conversations.app_server, "threads", return_value=indexed):
            code, body = self.request("/v1/projects")
            self.assertEqual(code, 200)
            result = json.loads(body)
            self.assertEqual([p["id"] for p in result["projects"]], ["alpha", "other"])
            entries = {entry["id"]: entry for entry in result["threads"]}
            self.assertEqual(entries["active"]["project_id"], "alpha")
            self.assertEqual(entries["active"]["title"], "新名称")
            self.assertEqual(entries["outside"]["project_id"], "")
            self.assertEqual(entries["manual"]["project_id"], "other")
            self.assertTrue(entries["manual"]["archived"])
            self.assertTrue(entries["manual"]["history_revision"])
            self.assertEqual(json.loads(self.request("/v1/projects?revision=" + result["revision"])[1]),
                             {"unchanged": True})
            self.assertEqual(self.request("/v1/projects", headers={"Origin": "https://example.invalid"})[0], 403)

    def test_creates_in_project_or_unassigned_and_retries_without_duplication(self):
        root = self.base / "workspace"
        root.mkdir()
        with sqlite3.connect(self.config["project_manager_db"]) as db:
            db.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE roots(path TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE assignments(thread_id TEXT PRIMARY KEY, project_id TEXT);
                INSERT INTO projects VALUES ('alpha', 'Alpha');
            """)
            db.execute("INSERT INTO roots VALUES (?, 'alpha')", (str(root),))
        headers = {"Content-Type": "application/json"}
        route = "/v1/thread/new"
        for project, thread, cwd in (("alpha", "new-project", root),
                                     ("", "new-unassigned", self.base / "coding-love" / "workspace")):
            def desktop(method, params):
                if method == "thread/start":
                    self.assertEqual(params["cwd"], str(cwd))
                    self.assertEqual(params["sandbox"], "workspace-write")
                    return {"thread": {"id": thread}}
                self.assertEqual(method, "thread/name/set")
                self.write(self.sessions / f"rollout-test-{thread}.jsonl", self.meta(ident=thread))
                return {}
            request = json.dumps({"project_id": project, "request_id": str(uuid.uuid4())})
            with patch.object(self.server.conversations.app_server, "request", side_effect=desktop) as rpc:
                for _ in range(2):
                    code, body = self.request(route, request, headers=headers)
                    self.assertEqual(code, 200)
                    self.assertEqual(json.loads(body), {"status": "created", "thread_id": thread})
                self.assertEqual(rpc.call_count, 2)
            self.assertEqual(self.request(route, request, headers={**headers, "Origin": "https://example.invalid"})[0], 403)
        self.assertTrue(cwd.is_dir())

    def test_native_fallback_preserves_reply_deduplication_and_archives(self):
        self.write(self.sessions / "rollout-test-thread-1.jsonl", self.meta())
        archive = self.base / "archived_sessions"
        archive.mkdir()
        self.write(archive / "rollout-test-archived.jsonl", self.meta(ident="archived"))
        headers = {"Content-Type": "application/json"}
        data = {"request_id": str(uuid.uuid4()), "thread_id": "thread-1", "text": "继续这个项目"}
        app = self.server.conversations.app_server
        with patch("codex_ipc.start_turn", return_value=("desktop_unavailable", None)), \
                patch.object(app, "send", return_value=("accepted", "native-turn")) as submit:
            for _ in range(2):
                result = json.loads(self.request("/v1/reply", json.dumps(data), headers=headers)[1])
                self.assertEqual(result, {"status": "accepted", "turn_id": "native-turn"})
            archived = {**data, "thread_id": "archived", "request_id": str(uuid.uuid4())}
            self.assertEqual(json.loads(self.request("/v1/reply", json.dumps(archived), headers=headers)[1]),
                             {"status": "archived"})
            submit.assert_called_once_with("thread-1", data["request_id"], data["text"], created_on_phone=False)
        with patch.object(app, "request", return_value={}) as rpc:
            result = json.loads(self.request("/v1/thread/unarchive", '{"thread_id":"archived"}', headers=headers)[1])
            self.assertEqual(result, {"status": "active"})
            rpc.assert_called_once_with("thread/unarchive", {"threadId": "archived"})

    def test_reads_and_pages_native_storage_without_a_rollout_file(self):
        app = self.server.conversations.app_server
        row = {"id": "native", "source": "vscode", "updatedAt": 1800000150, "recencyAt": 1800000120}
        turn = {"id": "turn-0", "status": "completed", "startedAt": 1800000000,
                "completedAt": 1800000060, "durationMs": 60000, "items": [
                    {"type": "userMessage", "completedAtMs": 1800000000123,
                     "content": [{"type": "text", "text": "原始提问"}]},
                    {"type": "reasoning", "summary": ["private reasoning"]},
                    {"type": "fileChange", "status": "completed", "changes": [{"path": "sample.py"}]},
                    {"type": "agentMessage", "phase": "final_answer", "text": "修改完成"},
                ]}
        turns = [{**turn, "id": f"turn-{i}"} for i in range(33)]
        with patch.object(app, "read_thread", return_value=row), patch.object(app, "turns", return_value=turns):
            route = "/v1/thread?thread_id=native"
            code, body = self.request(route)
            self.assertEqual(code, 200)
            page = json.loads(body)
            self.assertEqual(page["cache_revision"], "native-1800000150-1800000120")
            self.assertEqual(page["next_offset"], 32)
            first = page["turns"][0]
            self.assertEqual(first["elapsed_ms"], 60000)
            self.assertEqual(first["completed_at_ms"], 1800000060000)
            self.assertEqual(first["files_edited"], 1)
            self.assertEqual(first["messages"], [
                {"kind": "user", "text": "原始提问", "sent_at_ms": 1800000000123},
                {"kind": "assistant", "text": "修改完成"},
            ])
            same = route + "&revision=" + page["revision"]
            self.assertEqual(json.loads(self.request(same)[1]), {
                "unchanged": True, "cache_revision": "native-1800000150-1800000120",
            })
            next_page = json.loads(self.request(same + "&offset=32")[1])
            self.assertEqual([t["id"] for t in next_page["turns"]], ["turn-32"])

    def test_project_move_writes_desktop_assignment_and_can_restore_directory_matching(self):
        root = self.base / "alpha"
        with sqlite3.connect(self.config["project_manager_db"]) as labels:
            labels.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE roots(path TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE assignments(thread_id TEXT PRIMARY KEY, project_id TEXT);
                INSERT INTO projects VALUES ('alpha', 'Alpha'), ('beta', 'Beta');
            """)
            labels.executemany("INSERT INTO roots VALUES (?, ?)", [
                (str(root), "alpha"), (str(self.base / "beta"), "beta"),
            ])
        meta = self.meta()
        meta["payload"]["cwd"] = str(root)
        self.write(self.sessions / "rollout-test-thread-1.jsonl", meta)
        route = "/v1/thread/move"
        headers = {"Content-Type": "application/json"}
        move = {"thread_id": "thread-1", "project_id": "beta"}
        result = json.loads(self.request(route, json.dumps(move), headers=headers)[1])
        self.assertEqual(result, {"status": "moved", "project_id": "beta", "project": "Beta"})
        with sqlite3.connect(self.config["project_manager_db"]) as labels:
            self.assertEqual(labels.execute("SELECT * FROM assignments").fetchall(), [("thread-1", "beta")])
        result = json.loads(self.request(route, json.dumps({**move, "project_id": ""}), headers=headers)[1])
        self.assertEqual(result, {"status": "moved", "project_id": "alpha", "project": "Alpha"})
        self.assertEqual(self.request(route, json.dumps(move), headers={**headers, "Origin": "https://example.test"})[0], 403)

        rename = {"project_id": "alpha", "name": "  New   Alpha  "}
        route = "/v1/project/rename"
        self.assertEqual(self.request(route, json.dumps(rename), headers={**headers, "Origin": "https://example.test"})[0], 403)
        result = json.loads(self.request(route, json.dumps(rename), headers=headers)[1])
        self.assertEqual(result, {"status": "renamed", "name": "New Alpha"})
        with sqlite3.connect(self.config["project_manager_db"]) as labels:
            self.assertEqual(labels.execute("SELECT name FROM projects WHERE id='alpha'").fetchone(), ("New Alpha",))
            self.assertEqual(labels.execute("SELECT project_id FROM roots WHERE path=?", (str(root),)).fetchone(), ("alpha",))
        catalog = json.loads(self.request("/v1/threads")[1])["threads"]
        self.assertEqual([(row["project_id"], row["project"]) for row in catalog], [("alpha", "New Alpha")])

    def test_archives_via_codex_once_and_protects_live_turn(self):
        path = self.sessions / "rollout-test-thread-1.jsonl"
        archive = self.base / "archived_sessions"
        archive.mkdir()
        self.write(path, self.meta(), self.record("turn_context", {"turn_id": "turn-1"}))
        headers = {"Content-Type": "application/json"}
        data = '{"thread_id":"thread-1"}'
        route = "/v1/thread/archive"
        app = self.server.conversations.app_server
        with patch.object(app, "request") as rpc:
            self.assertEqual(json.loads(self.request(route, data, headers=headers)[1]), {"status": "busy"})
            rpc.assert_not_called()
        self.write(path, self.done())
        with patch.object(app, "request", side_effect=lambda *_: path.rename(archive / path.name)) as rpc:
            for _ in range(2):
                self.assertEqual(json.loads(self.request(route, data, headers=headers)[1]), {"status": "archived"})
            rpc.assert_called_once_with("thread/archive", {"threadId": "thread-1"})

    def test_forks_through_selected_turn_and_preserves_manual_project_on_retry(self):
        with sqlite3.connect(self.config["project_manager_db"]) as labels:
            labels.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE roots(path TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE assignments(thread_id TEXT PRIMARY KEY, project_id TEXT);
                INSERT INTO projects VALUES ('beta', 'Beta');
                INSERT INTO assignments VALUES ('thread-1', 'beta');
            """)
            labels.execute("INSERT INTO roots VALUES (?, 'beta')", (str(self.base / "other"),))
        path = self.sessions / "rollout-test-thread-1.jsonl"
        self.write(path, self.meta(), self.done(), self.done("turn-2"),
                   self.record("turn_context", {"turn_id": "running"}))
        headers = {"Content-Type": "application/json"}
        route = "/v1/thread/fork"
        fork = {"thread_id": "thread-1", "turn_id": "turn-1", "request_id": str(uuid.uuid4())}
        app = self.server.conversations.app_server
        with patch.object(app, "request", side_effect=[{"thread": {"id": "branch"}}, {}]) as rpc:
            for _ in range(2):
                code, body = self.request(route, json.dumps(fork), headers=headers)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body), {"status": "created", "thread_id": "branch",
                                                    "project_id": "beta", "project": "Beta"})
            self.assertEqual(rpc.call_count, 2)
            self.assertEqual(rpc.call_args_list[0].args, ("thread/fork", {
                "threadId": "thread-1", "lastTurnId": "turn-1", "excludeTurns": True,
                "deferGoalContinuation": True, "threadSource": "coding-love",
            }))
        with sqlite3.connect(self.config["project_manager_db"]) as labels:
            self.assertEqual(labels.execute("SELECT project_id FROM assignments WHERE thread_id='branch'").fetchone(),
                             ("beta",))
        busy_fork = {**fork, "turn_id": "running", "request_id": str(uuid.uuid4())}
        self.assertEqual(json.loads(self.request(route, json.dumps(busy_fork), headers=headers)[1]),
                         {"status": "unavailable"})
        self.assertEqual(self.request(route, json.dumps({**fork, "turn_id": "turn-2"}), headers=headers)[0], 400)

    def test_fork_history_reads_inherited_turns_and_keeps_their_logged_quota(self):
        parent = self.sessions / "rollout-test-thread-1.jsonl"
        branch = self.sessions / "rollout-test-branch.jsonl"
        context, final = self.final_records()
        done = self.done()
        reset = int(fixtures.module.event_time(done)) + 600
        self.write(parent, self.meta(), context, self.quota(resets_at=reset), final, done,
                   self.done("turn-2"), with_final=False)
        meta = self.meta(ident="branch")
        meta["payload"]["forked_from_id"] = "thread-1"
        self.write(branch, meta)
        app = self.server.conversations.app_server
        with patch.object(app, "read_thread", return_value={"id": "branch", "updatedAt": reset}), \
                patch.object(app, "turns", return_value=[{"id": "turn-1", "status": "completed", "items": []}]):
            code, body = self.request("/v1/thread?thread_id=branch")
            self.assertEqual(code, 200)
            page = json.loads(body)
            self.assertTrue(page["revision"].startswith("native-"))
            self.assertEqual([t["id"] for t in page["turns"]], ["turn-1"])
            self.assertEqual(page["turns"][0]["quota_reset_ms"], {"300": reset * 1000})

    def test_model_list_and_selected_model_are_tied_to_the_reply_request(self):
        path = self.sessions / "rollout-test-thread-1.jsonl"
        self.write(path, self.meta())
        choices = [{"id": "recorded-model", "label": "Recorded Model", "efforts": ["low", "high"], "default": True}]
        headers = {"Content-Type": "application/json"}
        request = {"thread_id": "thread-1", "request_id": str(uuid.uuid4()), "text": "继续",
                   "model": "recorded-model", "effort": "high"}
        with patch.object(self.server.conversations.app_server, "models", return_value=choices), \
                patch.object(self.server.conversations.app_server, "read_thread", return_value={}), \
                patch("codex_ipc.start_turn", return_value=("accepted", "new-turn")) as start:
            status, body = self.request("/v1/models?thread_id=thread-1")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"models": choices, "default_efforts": ["low", "high"]})
            self.assertEqual(self.request("/v1/models?thread_id=thread-1", headers={
                "Origin": "https://example.invalid",
            })[0], 403)
            unavailable = {**request, "model": "unavailable-model"}
            _, refused = self.request("/v1/reply", json.dumps(unavailable), headers=headers)
            self.assertEqual(json.loads(refused), {"status": "model_unavailable"})
            _, refused = self.request("/v1/reply", json.dumps({**request, "effort": "max"}), headers=headers)
            self.assertEqual(json.loads(refused), {"status": "effort_unavailable"})
            for _ in range(2):
                code, result = self.request("/v1/reply", json.dumps(request), headers=headers)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(result), {"status": "accepted", "turn_id": "new-turn"})
            code, _ = self.request("/v1/reply", json.dumps({**request, "effort": "low"}), headers=headers)
            self.assertEqual(code, 400)
            start.assert_called_once_with(self.base / "ipc" / "ipc.sock", "thread-1",
                                          request["request_id"], request["text"], model="recorded-model", effort="high")

    def test_interrupt_requires_paired_request_and_never_retries_unknown_desktop_result(self):
        route = "/v1/thread/interrupt"
        data = json.dumps({"thread_id": "thread-1", "turn_id": "expected-turn"})
        headers = {"Content-Type": "application/json"}
        app = self.server.conversations.app_server
        with patch("codex_ipc.interrupt_turn", side_effect=["unknown", "desktop_unavailable"]) as owner, \
                patch.object(app, "interrupt", return_value="requested") as fallback:
            self.assertEqual(self.request(route, data, headers={**headers, "Origin": "https://example.test"})[0], 403)
            owner.assert_not_called()
            self.assertEqual(json.loads(self.request(route, data, headers=headers)[1]), {"status": "unknown"})
            fallback.assert_not_called()
            self.assertEqual(json.loads(self.request(route, data, headers=headers)[1]), {"status": "requested"})
            fallback.assert_called_once_with("thread-1", "expected-turn")

    def test_history_includes_model_only_for_turns_with_a_recorded_model(self):
        path = self.sessions / "rollout-test-thread-1.jsonl"
        context, final = self.final_records()
        context["payload"]["model"] = "original-model"
        reroute = self.record("event_msg", {"type": "model_reroute", "turn_id": "turn-1",
                                           "to_model": "effective-model"})
        self.write(path, self.meta(), context, reroute, final, self.done(), with_final=False)
        self.write(path, self.done("older"))
        stat = path.stat()
        legacy = f"2-{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"
        _, body = self.request("/v1/thread?thread_id=thread-1&revision=" + legacy)
        page = json.loads(body)
        self.assertEqual(page["turns"][0]["model"], "effective-model")
        self.assertNotIn("model", page["turns"][1])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TailscaleChecks(name) for name in TailscaleChecks.__dict__ if name.startswith("test_"))
