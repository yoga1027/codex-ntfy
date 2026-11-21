"""Offline checks for OPPO queue dispatch and submission deadlines."""
import json
from unittest.mock import patch
import unittest

import notification_queue as queue
import oppo_sender
import test_codex_ntfy as fixtures


class OppoChecks(fixtures.BehaviorChecks):
    def test_oppo_retries_same_single_device_message_without_qq(self):
        self.config["transport"] = "oppo"
        credentials = self.base / "oppo.json"
        credentials.write_text(json.dumps({
            "app_key": "fixture-app", "master_secret": "fixture-secret", "registration_id": "one-device",
        }))
        self.config["oppo_credentials_file"] = credentials
        self.start()
        quota = "Codex 额度 5 小时剩余 75% · 7 天剩余 71%"

        with patch("oppo_sender.time.time", return_value=1000):
            queue.enqueue(self.config, "vscode", "old", completed_at=990)
            queue.enqueue(self.config, "vscode", "new", completed_at=995, quota=quota)
            with self.watcher.db:
                self.watcher.db.execute("UPDATE notifications SET message_id='qq:attempting' WHERE completed_at=990")

        with patch("oppo_sender.post", side_effect=[
            {"auth_token": "fixture-token"}, TimeoutError,
            {"auth_token": "fixture-token"}, {"messageId": "server-id"},
        ]) as post, patch("qq_sender.publish") as qq:
            with patch("oppo_sender.time.time", return_value=1000):
                self.watcher.deliver_one()
            with patch("oppo_sender.time.time", return_value=1010):
                self.watcher.deliver_one()
            qq.assert_not_called()

        first_request, second_request = post.call_args_list[1::2]
        first = json.loads(first_request.args[1]["message"])
        self.assertEqual(first, json.loads(second_request.args[1]["message"]))
        self.assertEqual(first["target_type"], 2)
        self.assertEqual(first["target_value"], "one-device")
        notification = first["notification"]
        self.assertEqual(notification["channel_id"], oppo_sender.CHANNEL_ID)
        self.assertEqual(notification["style"], 1)
        self.assertEqual(notification["title"], "VS Code Codex 已完成 · 1970-01-01 08:16:35（北京时间）")
        self.assertEqual(notification["content"], quota)
        self.assertFalse(notification["off_line"])
        rows = {row["completed_at"]: row for row in self.jobs()}
        self.assertEqual(rows[990]["message_id"], "qq:attempting")
        self.assertEqual(rows[995]["message_id"], "oppo:server-id")

    def test_standard_notification_preserves_status_without_quota(self):
        for test in (False, True):
            with self.subTest(test=test):
                payload = queue.completion_payload(self.config, 995, test=test)
                title, content = oppo_sender.standard_text(payload)
                self.assertLessEqual(len(title), 50)
                self.assertLessEqual(len(content), 50)
                self.assertIn("08:16:35（北京时间）", title)
                expected = "通知连接测试" if test else "本轮对话已完成"
                self.assertIn(expected, content)

    def test_authentication_crossing_deadline_skips_unicast(self):
        with patch("oppo_sender.read_credentials", return_value=("app", "secret", "device")), \
                patch("oppo_sender.authenticate", return_value="token") as authenticate, \
                patch("oppo_sender.unicast") as unicast, \
                patch("oppo_sender.time.time", side_effect=[299, 301]):
            self.config["oppo_credentials_file"] = self.base / "oppo.json"
            payload = {"title": "Codex", "message": "本轮已完成"}
            with self.assertRaises(TimeoutError):
                oppo_sender.publish(self.config, payload, "event", expires_at=300)
            authenticate.assert_called_once_with("app", "secret", timeout=1)
            unicast.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(OppoChecks(name) for name in OppoChecks.__dict__ if name.startswith("test_"))
