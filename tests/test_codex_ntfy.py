"""Behavior checks using temporary rollouts and a loopback HTTP server."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location(
    "codex_ntfy", Path(__file__).resolve().parents[1] / "codex_ntfy.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BehaviorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.config = {
            "sessions_dir": self.sessions,
            "state_dir": self.base / "state",
            "server": "https://ntfy.sh",
            "topic": "test-topic",
            "priority": 4,
            "timeout_seconds": 3,
        }
        self.watcher = None

    def tearDown(self):
        if self.watcher:
            self.watcher.close()
        self.tmp.cleanup()

    def record(self, kind, payload):
        timestamp = dt.datetime.fromtimestamp(time.time() + 60, dt.timezone.utc).isoformat()
        return {"timestamp": timestamp, "type": kind, "payload": payload}

    def write(self, path, *records, with_final=True):
        """Completion fixtures include the real producer's final-reply records."""
        with path.open("ab") as stream:
            for record in records:
                if with_final and record.get('type') == 'event_msg' and record.get('payload', {}).get('type') == 'task_complete':
                    for final in self.final_records(record['payload']['turn_id']):
                        stream.write(json.dumps(final).encode() + b'\n')
                stream.write(json.dumps(record).encode() + b"\n")

    def final_records(self, turn='turn-1'):
        return [self.record('turn_context', {'turn_id': turn}), self.record('response_item', {
            'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
            'content': [{'type': 'output_text', 'text': 'Fixture final reply'}],
        })]

    def meta(self, source="vscode", ident="thread-1"):
        return self.record("session_meta", {
            "id": ident, "source": source, "originator": "codex_vscode",
        })

    def done(self, turn="turn-1"):
        return self.record("event_msg", {
            "type": "task_complete", "turn_id": turn,
            "completed_at": time.time() + 60,
            "last_agent_message": "PRIVATE CODE SHOULD NOT BE SENT",
        })

    def quota(self, primary=25, secondary=29, *, limit_id="codex", resets_at=None):
        return self.record("event_msg", {
            "type": "token_count",
            "rate_limits": {
                "limit_id": limit_id,
                "limit_name": "PRIVATE",
                "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": resets_at},
                "secondary": {"used_percent": secondary, "window_minutes": 10080},
            },
        })

    def start(self):
        self.watcher = module.Watcher(self.config)

    def jobs(self):
        return list(self.watcher.db.execute("SELECT * FROM notifications"))

    def test_skip_existing_history(self):
        path = self.sessions / "rollout-old.jsonl"
        self.write(path, self.meta(), self.done())
        self.start()
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_completion_once_and_private_payload(self):
        path = self.sessions / "rollout-live.jsonl"
        self.write(path, self.meta())
        self.start()
        self.write(path, self.done(), self.done())
        self.watcher.scan()
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)
        payload = json.loads(self.jobs()[0]["payload"])
        self.assertEqual(set(payload), {"topic", "title", "message", "priority", "tags"})
        self.assertNotIn("PRIVATE", payload["message"])

    def test_ignore_cli_subagents_and_intermediate_events(self):
        self.start()
        for index, source in enumerate(["cli", {"subagent": {"thread_spawn": {"depth": 1}}}]):
            self.write(self.sessions / f"rollout-{index}.jsonl", self.meta(source, str(index)), self.done())
        self.write(self.sessions / "rollout-root.jsonl", self.meta(),
                   self.record("event_msg", {"type": "task_started", "turn_id": "1"}),
                   self.record("event_msg", {"type": "agent_reasoning", "text": "intermediate"}),
                   self.record("event_msg", {"type": "turn_aborted", "turn_id": "1"}))
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_partial_completion_waits_for_newline(self):
        path = self.sessions / "rollout-partial.jsonl"
        self.write(path, self.meta())
        self.start()
        self.write(path, *self.final_records())
        data = json.dumps(self.done()).encode()
        cut = len(data) // 2
        with path.open("ab") as stream:
            stream.write(data[:cut])
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])
        with path.open("ab") as stream:
            stream.write(data[cut:] + b"\n")
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_partial_meta_new_file(self):
        self.start()
        path = self.sessions / "rollout-new.jsonl"
        data = json.dumps(self.meta()).encode()
        path.write_bytes(data[:20])
        self.watcher.scan()
        with path.open("ab") as stream:
            stream.write(data[20:] + b"\n")
        self.write(path, self.done())
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_restart_delivers_pending_without_replay(self):
        path = self.sessions / "rollout-restart.jsonl"
        self.write(path, self.meta())
        self.start()
        self.write(path, self.done())
        self.watcher.scan()
        self.watcher.close()
        self.watcher = None
        self.start()
        self.watcher.scan()
        with patch.object(module, "publish", return_value="server-id") as send:
            self.watcher.deliver_one()
            self.watcher.deliver_one()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]["message_id"], "server-id")

    def test_network_failure_persists_and_backs_off(self):
        self.start()
        self.write(self.sessions / "rollout-retry.jsonl", self.meta(), self.done())
        self.watcher.scan()
        with patch.object(module, "publish", side_effect=TimeoutError):
            self.watcher.deliver_one()
        job = self.jobs()[0]
        self.assertIsNone(job["sent_at"])
        self.assertEqual(job["attempts"], 1)
        self.assertGreater(job["next_attempt"], time.time())
        self.watcher.close()
        self.watcher = None
        self.start()
        self.assertEqual(self.jobs()[0]["attempts"], 1)

    def test_old_imported_completion_is_ignored(self):
        self.start()
        old = self.done()
        old["timestamp"] = "2020-01-01T00:00:00Z"
        old["payload"]["completed_at"] = 1577836800
        self.write(self.sessions / "rollout-imported.jsonl", self.meta(), old)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_second_watcher_cannot_send_duplicates(self):
        self.start()
        with self.assertRaises(BlockingIOError):
            module.Watcher(self.config)

    def test_replaced_log_preserves_event_dedup(self):
        self.start()
        path = self.sessions / "rollout-replaced.jsonl"
        self.write(path, self.meta(), self.done())
        self.watcher.scan()
        replacement = path.with_suffix(".new")
        self.write(replacement, self.meta(), self.done())
        replacement.replace(path)
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_partial_line_at_bootstrap_is_retained(self):
        path = self.sessions / "rollout-bootstrap-partial.jsonl"
        self.write(path, self.meta())
        self.write(path, *self.final_records())
        data = json.dumps(self.done()).encode()
        with path.open("ab") as stream:
            stream.write(data[:30])
        self.start()
        with path.open("ab") as stream:
            stream.write(data[30:] + b"\n")
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_http_json_protocol(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                payload = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                received.append((handler.path, payload))
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps({
                    "event": "message", "topic": payload["topic"], "id": "accepted",
                }).encode())

            def log_message(*args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        config = {**self.config, "server": f"http://127.0.0.1:{server.server_port}"}
        try:
            payload = module.message_payload(config, time.time())
            with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
                self.assertEqual(module.publish(config, payload), "accepted")
            thread.join(3)
            self.assertEqual(received, [("/", payload)])
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
