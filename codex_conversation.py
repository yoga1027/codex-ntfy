"""Read the visible portion of a notified Codex conversation from its local rollout."""
from dataclasses import dataclass, field
import json
import re

from turn_changes import codex_edit, summary
from codex_events import event_time, quota_windows
from codex_app_server import SOURCE_KINDS
from notification_queue import valid_id

INTERNAL_CITATION = re.compile(r"<oai-mem-citation>.*?</oai-mem-citation>", re.DOTALL)
THREAD_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
HISTORY_VERSION = "7"


def history_revision(stat, modified_ns=None, *, version=HISTORY_VERSION):
    changed = stat.st_mtime_ns if modified_ns is None else modified_ns
    return f"{version}-{stat.st_ino:x}-{stat.st_size:x}-{changed:x}"


def terminal_status(error):
    value = re.sub(r"[_ -]", "", str(error).lower())
    if any(code in value for code in ("usagelimitexceeded", "ratelimitexceeded", "ratelimiterror", "ratelimit", "outofcredits",
                                      "insufficientquota", "usagequotaexceeded", "notenoughcredits")):
        return "quota"
    if any(code in value for code in ("connectionfailed", "streamdisconnected", "network", "connectionerror",
                                      "connectionreset", "econnreset", "connectionrefused", "timedout",
                                      "timeout", "failedtofetch", "connectionclosed", "connectionterminated",
                                      "econnrefused", "enetunreach", "sockethangup")):
        return "network"
    return "failed"


@dataclass
class Turn:
    id: str
    status: str = "running"
    elapsed_ms: int | None = None
    started_at: int | float | None = None
    completed_at: float | None = None
    model: str | None = None
    windows: list = field(default_factory=list)
    files: set = field(default_factory=set)
    edits: dict = field(default_factory=dict)
    item_edits: dict = field(default_factory=dict)
    error_hint: str | None = None
    messages: list = field(default_factory=list)
    last_source: str | None = None

    def add_text(self, kind, text, source, *, sent_at=None):
        if not isinstance(text, str):
            return
        text = INTERNAL_CITATION.sub("", text).strip()
        if not text:
            return
        message = {"kind": kind, "text": text}
        if sent_at is not None:
            message["sent_at_ms"] = int(sent_at * 1000)
        if self.messages and kind == self.messages[-1]["kind"] and self.last_source != source:
            previous = self.messages[-1]
            # A rollout can contain both the visible event and its retained response item.
            if text.startswith(previous["text"]) or previous["text"].startswith(text):
                if len(text) > len(previous["text"]):
                    previous["text"] = text
                if sent_at is not None and (source == "event" or "sent_at_ms" not in previous):
                    previous["sent_at_ms"] = message["sent_at_ms"]
                self.last_source = source
                return
        self.messages.append(message)
        self.last_source = source

    def finish(self, record):
        payload = record["payload"]
        self.completed_at = event_time(record)
        if payload["type"] == "turn_aborted":
            self.status = "interrupted"
        elif payload.get("error"):
            error = payload["error"]
            self.status = terminal_status(error)
            if self.status == "failed" and self.error_hint:
                self.status = self.error_hint
        else:
            self.status = "completed"
        elapsed = payload.get("duration_ms")
        if type(elapsed) is int and elapsed >= 0:
            self.elapsed_ms = elapsed
        else:
            ended = payload.get("completed_at", self.completed_at)
            if type(ended) in (int, float) and type(self.started_at) in (int, float) and ended >= self.started_at:
                self.elapsed_ms = int((ended - self.started_at) * 1000)
        if not any(message["kind"] == "assistant" for message in self.messages):
            self.add_text("assistant", payload.get("last_agent_message"), "terminal")

    def changes(self, path):
        return self.edits.get(path) or self.item_edits.get(path, [])

    def public(self):
        windows = {str(minutes): (remaining, reset) for minutes, remaining, reset in self.windows
                   if minutes in (300, 10080)
                   and (reset is None or self.completed_at is None or reset > self.completed_at)}
        data = {
            "id": self.id,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "completed_at_ms": int(self.completed_at * 1000) if self.completed_at is not None else None,
            "files_edited": len(self.files),
            "files": [summary(path, self.changes(path)) for path in sorted(self.files)],
            "quota_remaining_percent": {key: round(remaining, 2) for key, (remaining, _) in windows.items()},
            "quota_reset_ms": {key: reset * 1000 for key, (_, reset) in windows.items() if reset is not None},
            "messages": self.messages,
        }
        if self.model is not None:
            data["model"] = self.model
        return data


def valid_thread_id(thread):
    return isinstance(thread, str) and thread != "external" and THREAD_ID.fullmatch(thread) is not None


def split_event_id(event_id):
    if not isinstance(event_id, str) or len(event_id) > 500:
        return None
    thread, sep, turn = event_id.partition(":")
    if not sep or not valid_thread_id(thread):
        return None
    try:
        valid_id(turn)
    except ValueError:
        return None
    return thread, turn


def iter_rollouts(config, thread=None, *, all_sources=False):
    if thread is not None and not valid_thread_id(thread):
        return
    sessions = config["sessions_dir"]
    sources = SOURCE_KINDS if all_sources else config.get("sources", ["vscode"])
    pattern = "rollout-*.jsonl" if thread is None else f"rollout-*-{thread}.jsonl"
    for directory in (sessions, sessions.parent / "archived_sessions"):
        for path in directory.rglob(pattern):
            try:
                with path.open("rb") as stream:
                    meta = json.loads(stream.readline(1024 * 1024))
                payload = meta.get("payload", {})
                source = payload.get("source")
                ident = payload.get("id")
                if (meta.get("type") == "session_meta" and valid_thread_id(ident)
                        and (thread is None or ident == thread)
                        and isinstance(source, str) and source in sources):
                    yield ident, path, payload.get("cwd")
            except (OSError, ValueError, AttributeError):
                continue


def find_rollout(config, thread, *, all_sources=False):
    return next((path for _, path, _ in iter_rollouts(config, thread, all_sources=all_sources)), None)


def fork_parent(path):
    try:
        with path.open("rb") as stream:
            ident = json.loads(stream.readline(1024 * 1024)).get("payload", {}).get("forked_from_id")
        return ident if valid_thread_id(ident) else None
    except (OSError, ValueError, AttributeError):
        return None


def response_text(content):
    return "\n".join(item["text"] for item in content
                     if isinstance(item, dict) and item.get("type") in {"text", "output_text", "input_text"}
                     and isinstance(item.get("text"), str)) if isinstance(content, list) else ""


def observe_item(turn, item, *, sent_at=None):
    kind = item.get("type")
    # Internal rollouts use tagged turn items, while some editions include the v2 shape.
    if kind is None and len(item) == 1:
        kind, item = next(iter(item.items()))
        if not isinstance(item, dict):
            return
    if kind in {"agentMessage", "agent_message", "AgentMessage"}:
        text = item.get("text") or response_text(item.get("content"))
        phase = item.get("phase")
        turn.add_text("progress" if phase == "commentary" else "assistant", text, "item")
    elif kind in {"userMessage", "user_message", "UserMessage"}:
        turn.add_text("user", item.get("text") or response_text(item.get("content")), "item",
                      sent_at=sent_at)
    elif kind in {"fileChange", "file_change", "FileChange"} and item.get("status") == "completed":
        for change in item.get("changes", []):
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                turn.files.add(change["path"])
                turn.item_edits.setdefault(change["path"], []).append(codex_edit(change))


def read_conversation(path, anchor=None, *, byte_limit=None):
    turns = []
    current = None
    found = anchor is None
    with path.open("rb") as stream:
        while byte_limit is None or stream.tell() < byte_limit:
            line = stream.readline() if byte_limit is None else stream.readline(byte_limit - stream.tell())
            if not line.endswith(b"\n"):
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            kind, payload = record.get("type"), record["payload"]
            event = payload.get("type") if kind == "event_msg" else None
            ident = payload.get("turn_id")
            if event == "task_started" or kind == "turn_context":
                if not isinstance(ident, str) or not ident:
                    continue
                found |= ident == anchor
                if found and (current is None or current.id != ident):
                    current = Turn(ident)
                    turns.append(current)
                if current is not None and current.id == ident:
                    if current.started_at is None:
                        current.started_at = payload.get("started_at")
                    if kind == "turn_context" and isinstance(payload.get("model"), str):
                        current.model = payload["model"]
                continue
            if current is None or current.status != "running":
                continue
            if event in {"task_complete", "turn_aborted"} and ident == current.id:
                current.finish(record)
            elif event in {"model_reroute", "model_rerouted"} and ident in (None, current.id):
                model = payload.get("to_model")
                if isinstance(model, str) and model:
                    current.model = model
            elif event == "error" and ident in (None, current.id):
                hint = terminal_status(payload)
                if hint in ("network", "quota"):
                    current.error_hint = hint
            elif event == "token_count":
                windows = quota_windows(payload.get("rate_limits"))
                if windows:
                    current.windows = windows
            elif event == "agent_message":
                current.add_text("progress" if payload.get("phase") == "commentary" else "assistant",
                                 payload.get("message"), "event")
            elif event == "user_message":
                current.add_text("user", payload.get("message"), "event", sent_at=event_time(record))
            elif event == "patch_apply_end" and ident == current.id and payload.get("success") is True:
                changes = payload.get("changes")
                if isinstance(changes, dict):
                    current.files.update(changes)
                    for filename, change in changes.items():
                        current.edits.setdefault(filename, []).append(codex_edit(change))
            elif event == "item_completed" and ident == current.id and isinstance(payload.get("item"), dict):
                observe_item(current, payload["item"], sent_at=event_time(record))
            elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
                phase = payload.get("phase")
                if phase in (None, "commentary", "final_answer"):
                    current.add_text("progress" if phase == "commentary" else "assistant",
                                     response_text(payload.get("content")), "response")
    if anchor is not None and (not turns or turns[0].status == "running"):
        return []
    # The chosen reminder shows the AI's output; later turns show both sides of the exchange.
    if anchor is not None:
        turns[0].messages = [message for message in turns[0].messages if message["kind"] != "user"]
    return turns


def native_turns(rows):
    """Convert Codex's paginated item history to the same visible transcript as rollouts."""
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        turn = Turn(row["id"])
        turn.status = {
            "completed": "completed", "interrupted": "interrupted", "failed": "failed",
        }.get(row.get("status"), "running")
        error = row.get("error")
        if error and turn.status == "failed":
            turn.status = terminal_status(error)
        turn.elapsed_ms = row.get("durationMs")
        turn.completed_at = row.get("completedAt")
        if isinstance(row.get("model"), str):
            turn.model = row["model"]
        for item in row.get("items", []):
            if not isinstance(item, dict):
                continue
            sent_at = None
            if item.get("type") == "userMessage":
                recorded = item.get("completedAtMs") or item.get("startedAtMs")
                if isinstance(recorded, int):
                    sent_at = recorded / 1000
                elif isinstance(row.get("startedAt"), int):
                    sent_at = row["startedAt"]
            observe_item(turn, item, sent_at=sent_at)
        result.append(turn)
    return result
