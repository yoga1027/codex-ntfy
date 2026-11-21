#!/usr/bin/env python3
"""Send a phone reminder when a local Codex root turn completes.

Uses the installed Codex rollout format (event_msg/task_complete). Conversation
names come from the local session index.
"""
import argparse
from contextlib import nullcontext
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
from typing import NamedTuple
import urllib.error
import urllib.request
import oppo_sender
import tailscale_receiver
from codex_events import event_time, quota_windows
from notification_queue import completion_payload, open_database, deadline, expire_pending

LOG = logging.getLogger("codex-ntfy")
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def load_config(path):
    path = Path(path).resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Missing {path}. Copy config.example.json to config.json and configure your transport.") from None
    config.setdefault("topic", "")
    for key in ["sessions_dir", "state_dir"]:
        value = Path(config[key]).expanduser()
        config[key] = value if value.is_absolute() else path.parent / value
    config.setdefault("poll_seconds", 1)
    config.setdefault("timeout_seconds", 10)
    config.setdefault("priority", 4)
    config.setdefault("sources", ["vscode"])
    config.setdefault("transport", "ntfy")
    config.setdefault('max_notification_age_seconds', 300)
    if (not isinstance(config['max_notification_age_seconds'], (int, float))
            or not 0 < config['max_notification_age_seconds'] <= 300):
        raise ValueError('max_notification_age_seconds must be between 0 and 300')
    if config["transport"] not in {"ntfy", "qq", "oppo", "tailscale"}:
        raise ValueError("transport must be ntfy, qq, oppo, or tailscale")
    if config["transport"] == "ntfy":
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", config["topic"]):
            raise ValueError("Set your own ntfy topic in config.json (1–64 letters, digits, hyphens or underscores)")
        if not config["server"].startswith(("https://", "http://")):
            raise ValueError("ntfy server must use HTTP or HTTPS")
    if config["transport"] == "oppo":
        credentials = Path(config["oppo_credentials_file"]).expanduser()
        config["oppo_credentials_file"] = path.parent / credentials
    if config["transport"] == "tailscale":
        tailscale_receiver.validate_config(config.get("tailscale"))
    if (not isinstance(config["sources"], list) or not config["sources"]
            or any(source not in {"vscode", "cli"} for source in config["sources"])):
        raise ValueError("sources must contain vscode and/or cli")
    if not 0 < config["poll_seconds"] <= 60:
        raise ValueError("poll_seconds must be between 0 and 60")
    if not 0 < config["timeout_seconds"] <= 60:
        raise ValueError("timeout_seconds must be between 0 and 60")
    if config["priority"] not in range(1, 6):
        raise ValueError("priority must be between 1 and 5")
    return config


def message_payload(config, completed_at, test=False, app="vscode"):
    return completion_payload(config, completed_at, app=app, test=test)


def publish(config, payload):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        config["server"].rstrip("/") + "/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
        result = json.loads(response.read())
    if (result.get("event") != "message" or result.get("topic") != config["topic"]
            or not isinstance(result.get("id"), str)):
        raise ValueError("ntfy did not acknowledge a message for the configured topic")
    return result["id"]


def complete_line_offset(stream):
    """Keep a partial last line available for parsing when Codex finishes it."""
    end = stream.seek(0, os.SEEK_END)
    position = end
    while position > 0:
        start = max(0, position - 8192)
        stream.seek(start)
        block = stream.read(position - start)
        newline = block.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


def is_final_reply(record):
    payload = record.get('payload', {})
    content = payload.get('content')
    return (record.get('type') == 'response_item' and payload.get('type') == 'message'
            and payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer'
            and isinstance(content, list)
            and any(isinstance(part, dict) and isinstance(part.get('text'), str) and part['text'].strip()
                    for part in content))


class TurnState:
    def __init__(self, active=None, final=None, windows=None, *, session_id=None):
        self.active = active
        self.final = final
        self.windows = windows or []
        self.session_id = session_id

    def observe(self, record):
        kind = record.get("type")
        payload = record.get("payload", {})
        event = payload.get("type") if kind == "event_msg" else None
        if kind == "session_meta":
            # VS Code may refresh this metadata after task_started.
            session_id = payload.get("id")
            if session_id != self.session_id:
                self.active, self.final, self.windows = None, None, []
            self.session_id = session_id
        elif kind == "turn_context" or event == "task_started":
            next_turn = payload.get("turn_id")
            if next_turn and str(next_turn) != self.active:
                self.active, self.final, self.windows = str(next_turn), None, []
        elif is_final_reply(record):
            self.final = self.active
        elif event == "turn_aborted":
            self.active, self.final, self.windows = None, None, []
        elif event == "token_count" and self.active:
            windows = quota_windows(payload.get("rate_limits"))
            if windows:
                self.windows = windows

    def completion(self, record, *, allow_quota_interruption):
        payload = record.get("payload", {})
        if record.get("type") != "event_msg" or payload.get("type") != "task_complete":
            return None
        turn_id = payload.get("turn_id") or record.get("timestamp")
        if not turn_id:
            return None
        turn_id = str(turn_id)
        error = payload.get("error")
        quota_interrupted = (
            allow_quota_interruption and turn_id == self.active
            and isinstance(error, dict) and error.get("codex_error_info") == "usage_limit_exceeded"
        )
        if not quota_interrupted and (error is not None or turn_id != self.final):
            return None
        completed_at = event_time(record)
        if completed_at is None:
            return None
        current = [(minutes, remaining, resets_at) for minutes, remaining, resets_at in self.windows
                   if resets_at is None or resets_at > completed_at]
        windows = {minutes: remaining for minutes, remaining, _ in current}
        reset_times = {minutes: resets_at for minutes, _, resets_at in current if resets_at is not None}
        return TurnCompletion(turn_id, completed_at, windows, quota_interrupted, reset_times)


class TurnCompletion(NamedTuple):
    turn_id: str
    completed_at: float
    windows: dict[int, float]
    quota_interrupted: bool
    reset_times: dict[int, int]


class Watcher:
    def __init__(self, config):
        self.config = config
        self.root = config["sessions_dir"]
        config["state_dir"].mkdir(parents=True, exist_ok=True)
        self.lock = (config["state_dir"] / "watch.lock").open("a")
        # Avoid double sends if someone also starts the script manually.
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise
        self.db = open_database(config)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, identity TEXT, offset INTEGER,
                session_id TEXT, source TEXT
            );
        """)
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(files)')}
        for name in ('active_turn', 'final_turn', 'quota_windows'):
            if name not in columns:
                self.db.execute('ALTER TABLE files ADD COLUMN ' + name + ' TEXT')
        with self.db:
            if self.db.execute("INSERT OR IGNORE INTO settings VALUES ('session_meta_refresh_migrated', '1')").rowcount:
                # Rebuild cursors that may have lost a live turn under the old
                # metadata handling. Keep their offsets and all queued reminders.
                self.db.execute("UPDATE files SET quota_windows=NULL WHERE active_turn IS NULL")
        existing = self.db.execute("SELECT value FROM settings WHERE key='installed_at'").fetchone()
        self.installed_at = float(existing[0]) if existing else time.time()
        if not existing:
            self.bootstrap()

    def paths(self):
        return sorted(self.root.rglob("rollout-*.jsonl"))

    def bootstrap(self):
        if not self.root.is_dir():
            raise FileNotFoundError("Codex sessions directory is missing: " + str(self.root))
        with self.db:
            for path in self.paths():
                try:
                    with path.open("rb") as stream:
                        stat = os.fstat(stream.fileno())
                        first = stream.readline()
                        meta = {}
                        if first.endswith(b"\n"):
                            try:
                                record = json.loads(first)
                                if record.get("type") == "session_meta":
                                    meta = record.get("payload", {})
                            except (ValueError, UnicodeDecodeError):
                                pass
                        source = meta.get("source")
                        source = source if isinstance(source, str) else ("subagent" if source else None)
                        end = complete_line_offset(stream)
                        stream.seek(0)
                        turn = TurnState()
                        while stream.tell() < end:
                            try:
                                record = json.loads(stream.readline())
                                turn.observe(record)
                            except (ValueError, UnicodeDecodeError):
                                continue
                        self.save_cursor(path, stat, end, meta.get("id"), source, turn)
                except FileNotFoundError:
                    continue
            self.db.execute("INSERT INTO settings VALUES ('installed_at', ?)", (str(self.installed_at),))
        LOG.info("Initialized at current log positions; historical tasks will not be sent")

    def save_cursor(self, path, stat, offset, session_id, source, turn):
        self.db.execute("""
            INSERT OR REPLACE INTO files(path,identity,offset,session_id,source,active_turn,final_turn,quota_windows)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(path), f"{stat.st_dev}:{stat.st_ino}", offset, session_id, source,
            turn.active, turn.final, json.dumps(turn.windows),
        ))

    def scan(self):
        for path in self.paths():
            try:
                self.read_file(path)
            except (FileNotFoundError, PermissionError) as error:
                LOG.warning("Cannot read rollout %s: %s", path.name, type(error).__name__)

    def read_file(self, path):
        cursor = self.db.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            same = cursor and cursor["identity"] == f"{stat.st_dev}:{stat.st_ino}"
            valid = same and cursor["offset"] <= stat.st_size
            offset = cursor["offset"] if valid else 0
            session_id = cursor["session_id"] if valid else None
            source = cursor["source"] if valid else None
            originator = None
            turn = TurnState()
            if valid and offset == stat.st_size:
                return
            if valid:
                try:
                    first = json.loads(stream.readline())
                    if first.get("type") == "session_meta":
                        originator = first.get("payload", {}).get("originator")
                except (ValueError, UnicodeDecodeError):
                    pass
                if cursor["quota_windows"] is None:
                    # Recover an in-flight turn from an old cursor without
                    # enqueueing historical completions.
                    stream.seek(0)
                    while stream.tell() < offset:
                        try:
                            old_record = json.loads(stream.readline())
                            turn.observe(old_record)
                        except (ValueError, UnicodeDecodeError):
                            continue
                else:
                    turn = TurnState(cursor["active_turn"], cursor["final_turn"],
                                     json.loads(cursor["quota_windows"]), session_id=session_id)
            stream.seek(offset)
            with self.db:
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        LOG.warning("Skipped invalid complete JSON line in %s", path.name)
                        continue
                    payload = record.get("payload", {})
                    turn.observe(record)
                    if record.get("type") == "session_meta":
                        session_id = payload.get("id")
                        raw_source = payload.get("source")
                        source = raw_source if isinstance(raw_source, str) else "subagent"
                        originator = payload.get("originator")
                    elif source in self.config.get("sources", ["vscode"]) and session_id:
                        completion = turn.completion(
                            record, allow_quota_interruption=self.config.get("transport") == "tailscale")
                        if completion is None or completion.completed_at < self.installed_at:
                            continue
                        turn_id, completed_at, windows, quota_interrupted, reset_times = completion
                        event_key = session_id + ":" + turn_id
                        app = "codex" if source == "cli" else "vscode"
                        if originator in {"codex_work_desktop", "Codex Desktop"}:
                            app = "chatgpt"
                        message = completion_payload(self.config, completed_at, app=app,
                                                     thread_id=session_id, windows=windows,
                                                     quota_interrupted=quota_interrupted, reset_times=reset_times)
                        inserted = self.db.execute(
                            "INSERT OR IGNORE INTO notifications (event_key, completed_at, payload, expired_at) VALUES (?, ?, ?, ?)",
                            (event_key, completed_at, json.dumps(message, ensure_ascii=False), time.time() if time.time() > deadline(self.config, completed_at) else None),
                        ).rowcount
                        if inserted:
                            LOG.info("Queued %s turn %s %s", app,
                                     "quota interruption" if quota_interrupted else "completion", turn_id)
                self.save_cursor(path, stat, offset, session_id, source, turn)

    def deliver_one(self):
        expired = expire_pending(self.db, self.config)
        if expired:
            LOG.info('Expired %s unsent reminders older than the delivery window', expired)
        transport = self.config.get("transport", "ntfy")
        if transport == "tailscale":
            return  # The phone pulls from the queue through the private receiver.
        attempting = f"{transport}:attempting" if transport in {"qq", "oppo"} else None
        # An interrupted send can only be resolved through its original transport.
        row = self.db.execute("""
            SELECT * FROM notifications
            WHERE sent_at IS NULL AND expired_at IS NULL AND next_attempt<=?
              AND (message_id IS NULL OR message_id=?)
            ORDER BY completed_at LIMIT 1
        """, (time.time(), attempting)).fetchone()
        if not row:
            return
        if time.time() > deadline(self.config, row['completed_at']):
            expire_pending(self.db, self.config)
            return
        try:
            if attempting:
                with self.db:
                    self.db.execute("UPDATE notifications SET message_id=? WHERE event_key=?",
                                    (attempting, row["event_key"]))
            payload = json.loads(row["payload"])
            if transport == "qq":
                import qq_sender
                verify_only = row["message_id"] == attempting
                message_id = qq_sender.publish(self.config, payload, row["event_key"],
                                               verify_only=verify_only, expires_at=deadline(self.config, row["completed_at"]))
            elif transport == "oppo":
                message_id = oppo_sender.publish(self.config, payload, row["event_key"],
                                                 expires_at=deadline(self.config, row["completed_at"]))
            else:
                message_id = publish(self.config, payload)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            if time.time() > deadline(self.config, row['completed_at']):
                expire_pending(self.db, self.config)
                LOG.info('Reminder expired during delivery; no further retry')
                return
            if transport == "qq":
                from qq_sender import QQUncertain
                if isinstance(error, QQUncertain):
                    with self.db:
                        self.db.execute("UPDATE notifications SET next_attempt=1e30 WHERE event_key=?", (row["event_key"],))
                    LOG.error("QQ send outcome uncertain; automatic resend paused for %s; inspect device chat", row["event_key"])
                    return
                with self.db:
                    self.db.execute("UPDATE notifications SET message_id=NULL WHERE event_key=?", (row["event_key"],))
            attempts = row["attempts"] + 1
            delay = min(300, 5 * (2 ** min(attempts, 6)))
            with self.db:
                self.db.execute("UPDATE notifications SET attempts=?, next_attempt=? WHERE event_key=?",
                                (attempts, min(time.time() + delay, deadline(self.config, row['completed_at'])), row["event_key"]))
            # Do not print server response bodies or authentication headers.
            if transport == "qq" or (transport == "oppo" and isinstance(error, oppo_sender.OppoError)):
                reason = str(error)
            elif isinstance(error, urllib.error.HTTPError):
                reason = f"HTTP {error.code}"
            else:
                reason = type(error).__name__
            LOG.warning("%s push failed (%s); retry in %ss", transport, reason, delay)
            return
        with self.db:
            self.db.execute("UPDATE notifications SET sent_at=?, message_id=? WHERE event_key=?",
                            (time.time(), message_id, row["event_key"]))
        outcome = {"qq": "QQ device chat submitted", "oppo": "OPPO accepted", "ntfy": "ntfy accepted"}
        LOG.info("%s completion notification: %s", outcome[transport], message_id)

    def close(self):
        self.db.close()
        self.lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["watch", "test"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    config = load_config(args.config)
    if args.command == "test":
        if config["transport"] in {"qq", "oppo", "tailscale"}:
            import uuid
            from notification_queue import enqueue
            print(json.dumps(enqueue(config, app="vscode", event_id="test-" + str(uuid.uuid4()), test=True), ensure_ascii=False))
            return 0
        message_id = publish(config, message_payload(config, time.time(), test=True))
        print("ntfy accepted test notification:", message_id)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    watcher = Watcher(config)
    LOG.info("Watching Codex root sessions %s; transport=%s poll=%ss",
             config["sources"], config["transport"], config["poll_seconds"])
    try:
        receiver = tailscale_receiver.run_receiver(config) if config["transport"] == "tailscale" else nullcontext()
        with receiver:
            while not stop.is_set():
                watcher.scan()
                watcher.deliver_one()
                stop.wait(config["poll_seconds"])
    finally:
        watcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
