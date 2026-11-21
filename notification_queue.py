"""Shared durable outbox. Producers never perform network I/O."""
import datetime as dt
import json
import math
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

APPS = {
    "vscode": "VS Code Codex", "codex": "Codex", "chatgpt": "ChatGPT",
    "zcodium": "zcodium", "token-plan": "词元计划", "claude": "Claude Code",
    "generic": "AI",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    event_key TEXT PRIMARY KEY, completed_at REAL NOT NULL,
    payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, sent_at REAL, message_id TEXT, expired_at REAL
);
CREATE TABLE IF NOT EXISTS phone_replies (
    request_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, thread_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL, turn_id TEXT
);
CREATE TABLE IF NOT EXISTS phone_threads (
    request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, thread_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS phone_forks (
    request_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    project_id TEXT NOT NULL, child_id TEXT UNIQUE, finished INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claude_phone (
    thread_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, cwd TEXT, project_id TEXT,
    title TEXT, created_ms INTEGER, archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS phone_edits (
    thread_id TEXT, turn_id TEXT, path TEXT, before_hash TEXT, after_hash TEXT, mode INTEGER,
    PRIMARY KEY (thread_id, turn_id, path)
);
CREATE TABLE IF NOT EXISTS codex_account_usage (
    account_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_usage (
    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, observed_ns INTEGER NOT NULL, windows TEXT NOT NULL,
    PRIMARY KEY (thread_id, turn_id)
);
CREATE TABLE IF NOT EXISTS claude_turn_failures (
    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL,
    ended_ns INTEGER NOT NULL, observed_ns INTEGER NOT NULL,
    PRIMARY KEY (thread_id, turn_id)
);
CREATE TABLE IF NOT EXISTS claude_interruptions (
    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, ended_ns INTEGER NOT NULL,
    PRIMARY KEY (thread_id, turn_id)
);
"""


def open_database(config):
    config["state_dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(config["state_dir"] / "state.sqlite3", timeout=3)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if 'expired_at' not in {row['name'] for row in db.execute('PRAGMA table_info(notifications)')}:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if 'expired_at' not in {row['name'] for row in db.execute('PRAGMA table_info(notifications)')}:
                db.execute('ALTER TABLE notifications ADD COLUMN expired_at REAL')
    return db


def deadline(config, completed_at):
    return completed_at + config.get('max_notification_age_seconds', 300)


def expire_pending(db, config, now=None):
    now = time.time() if now is None else now
    with db:
        return db.execute('UPDATE notifications SET expired_at=? WHERE sent_at IS NULL AND expired_at IS NULL AND completed_at<?',
                          (now, now-config.get('max_notification_age_seconds', 300))).rowcount


def conversation_names(config):
    names = {}
    index = config["sessions_dir"].parent / "session_index.jsonl"
    try:
        with index.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    value = record.get("thread_name")
                    if isinstance(value, str):
                        name = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
                        if name:
                            names[record["id"]] = name[:79] + "…" if len(name) > 80 else name
    except OSError:
        pass
    return names


def conversation_name(config, thread_id):
    return conversation_names(config).get(thread_id, "未命名对话")


def quota_text(windows):
    labels = []
    for minutes, remaining in windows.items():
        if minutes % 1440 == 0:
            label = f"{minutes // 1440} 天"
        elif minutes % 60 == 0:
            label = f"{minutes // 60} 小时"
        else:
            label = f"{minutes} 分钟"
        labels.append(f"{label}剩余 {remaining:g}%")
    return "Codex 额度 " + " · ".join(labels) if labels else None


def completion_payload(config, completed_at, app="vscode", test=False, *, quota=None,
                       thread_id=None, windows=None, quota_interrupted=False, reset_times=None,
                       thread_title=None):
    label = APPS[app]
    when = dt.datetime.fromtimestamp(completed_at, ZoneInfo("Asia/Shanghai"))
    payload = {"topic": config["topic"], "priority": config["priority"], "tags": ["bell"]}
    if config.get("transport") == "tailscale" and thread_id and windows is not None and not test:
        inline = []
        detailed = []
        shows_reset = False
        for minutes, window_label in ((300, "5h"), (10080, "7天")):
            percent = windows.get(minutes)
            remaining = "—" if percent is None else f"{percent:g}%"
            short = f"{window_label}{remaining}"
            full = f"{window_label} {remaining}"
            reset = (reset_times or {}).get(minutes)
            if percent is not None and type(reset) is int and reset > completed_at:
                try:
                    reset_at = dt.datetime.fromtimestamp(reset, when.tzinfo)
                except (ValueError, OverflowError, OSError):
                    reset_at = None
                if reset_at is not None:
                    short_time = f"{reset_at:%H:%M}" if reset_at.date() == when.date() else f"{reset_at:%m/%d %H:%M}"
                    short += f"({short_time})"
                    full += f" · 重置 {reset_at:%m-%d %H:%M}"
                    shows_reset = True
            inline.append(short)
            detailed.append(full)
        status = "额度不足中断" if quota_interrupted else "对话完成"
        short_status = "额度中断" if quota_interrupted else "完成"
        details = ("\n" if shows_reset else " · ").join(detailed)
        payload.update(
            title=f"{'claude' if app == 'claude' else 'codex'} {thread_title or conversation_name(config, thread_id)}",
            message=f"{status}\n结束 {when:%Y-%m-%d %H:%M:%S}（北京时间）\n{details}",
            summary=f"{short_status} · {when:%Y-%m-%d %H:%M:%S} · {' '.join(inline)}",
        )
        return payload

    if windows is not None:
        quota = quota_text(windows)
    text = ("通知连接测试：电脑已成功发送消息。" if test else
            f"{label} 本轮对话已完成，请查看最终回复。")
    if quota and not test:
        text += "\n" + quota
    payload.update(
        title=label + (" - Test" if test else ""),
        message=text + "\n时间：" + when.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）",
    )
    return payload


def valid_id(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 200
            or any(ord(char) < 32 for char in value) or ":" in value):
        raise ValueError("Event identifiers must be 1-200 characters without colons or control characters")
    return value


def enqueue(config, app, event_id, thread_id=None, completed_at=None, test=False, *, quota=None,
            windows=None, quota_interrupted=False, reset_times=None, thread_title=None):
    if app not in APPS:
        raise ValueError("Unknown application")
    valid_id(event_id)
    if thread_id is not None:
        valid_id(thread_id)
        if app not in {"codex", "vscode", "chatgpt"} and not (app == "claude" and thread_id.startswith("claude-")):
            raise ValueError("Invalid application for a local conversation")
        key = thread_id + ":" + event_id
    else:
        key = "external:" + app + ":" + event_id
    completed_at = time.time() if completed_at is None else completed_at
    if not isinstance(completed_at, (int, float)) or not math.isfinite(completed_at):
        raise ValueError('Invalid completion timestamp')
    now = time.time()
    expired = now > deadline(config, completed_at)
    payload = completion_payload(config, completed_at, app, test=test, quota=quota,
                                 thread_id=thread_id, windows=windows, quota_interrupted=quota_interrupted,
                                 reset_times=reset_times, thread_title=thread_title)
    db = open_database(config)
    try:
        expire_pending(db, config, now)
        with db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO notifications (event_key, completed_at, payload, expired_at) VALUES (?, ?, ?, ?)",
                (key, completed_at, json.dumps(payload, ensure_ascii=False), now if expired else None),
            ).rowcount
        expired = db.execute('SELECT expired_at IS NOT NULL FROM notifications WHERE event_key=?', (key,)).fetchone()[0] == 1
        return {"queued": bool(inserted) and not expired, "expired": expired, "duplicate": not bool(inserted), "event_id": event_id,
                "delivery": "background-service", "phone_received": "unknown"}
    finally:
        db.close()
