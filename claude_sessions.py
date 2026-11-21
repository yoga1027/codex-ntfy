"""Local Claude Code session history and supported Agent SDK operations."""
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from codex_conversation import Turn, history_revision, terminal_status
from turn_changes import claude_edit
from codex_events import event_time
from notification_queue import enqueue, open_database

PREFIX = "claude-"
PROJECT_PREFIX = "claude-project-"
UNASSIGNED = PROJECT_PREFIX + "unassigned"
CLAUDE_HISTORY_VERSION = "6"
SDK_LOCK = threading.RLock()
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


def session_id(thread):
    if not isinstance(thread, str) or not thread.startswith(PREFIX):
        return None
    raw = thread[len(PREFIX):]
    try:
        return raw if str(uuid.UUID(raw)) == raw else None
    except ValueError:
        return None


def project_id(cwd):
    if not cwd:
        return UNASSIGNED
    return PROJECT_PREFIX + hashlib.sha256(os.fsencode(os.path.realpath(cwd))).hexdigest()[:24]


def clean_title(text):
    return " ".join(text.split())[:120]


@dataclass
class ClaudeTurn(Turn):
    last_message_id: str | None = None


@dataclass
class ClaudeRun:
    process: subprocess.Popen
    turn: str
    interrupt_id: str | None = None
    finishing: bool = False


def text_blocks(content):
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content if isinstance(part, dict)
                     and part.get("type") == "text" and isinstance(part.get("text"), str))


@lru_cache(maxsize=12)
def read_claude(path, revision="", byte_limit=None):
    turns = []
    current = None
    edits = {}
    last_visible_at = None
    with path.open("rb") as stream:
        while byte_limit is None or stream.tell() < byte_limit:
            line = stream.readline() if byte_limit is None else stream.readline(byte_limit - stream.tell())
            if not line.endswith(b"\n"):
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get("isSidechain") or record.get("isMeta"):
                continue
            ident = record.get("uuid")
            kind = record.get("type")
            message = record.get("message")
            content = message.get("content", []) if isinstance(message, dict) else []
            blocks = content if isinstance(content, list) else []
            if kind == "user":
                if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks):
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                            path_changed = edits.pop(block.get("tool_use_id"), None)
                            if path_changed is not None and current is not None:
                                current.files.add(path_changed)
                                current.edits.setdefault(path_changed, []).append(claude_edit(record.get("toolUseResult")))
                    continue
                text = text_blocks(content if isinstance(content, (str, list)) else [])
                if (not isinstance(ident, str) or not text.strip() or text.lstrip().startswith(
                        ("<local-command", "<command-name>", "[Request interrupted"))):
                    continue
                if current is not None and current.status == "running":
                    current.status = "interrupted"
                    current.completed_at = last_visible_at
                timestamp = event_time(record)
                current = ClaudeTurn(ident, started_at=timestamp)
                current.add_text("user", text, "event", sent_at=timestamp)
                turns.append(current)
                edits.clear()
                last_visible_at = None
            elif kind == "assistant" and current is not None and isinstance(message, dict):
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                        inputs = block.get("input")
                        if isinstance(inputs, dict):
                            filename = inputs.get("file_path") or inputs.get("notebook_path")
                            if isinstance(filename, str) and isinstance(block.get("id"), str):
                                edits[block["id"]] = filename
                text = text_blocks(content if isinstance(content, (str, list)) else [])
                if text.strip():
                    timestamp = event_time(record)
                    stop = message.get("stop_reason")
                    error = record.get("isApiErrorMessage")
                    final = stop in {"end_turn", "stop_sequence", "max_tokens"} or error
                    current.add_text("assistant" if final else "progress", text, "event")
                    current.last_message_id = ident if isinstance(ident, str) else None
                    last_visible_at = timestamp
                    if final:
                        quota = str(record.get("error", "")).lower() in {"rate_limit", "rate_limit_error"}
                        current.status = "quota" if quota else (terminal_status([record.get("error"), text]) if error else "completed")
                        current.completed_at = timestamp
            elif kind == "system" and current is not None and record.get("subtype") == "turn_duration":
                duration = record.get("durationMs")
                if type(duration) is int and duration >= 0:
                    current.elapsed_ms = duration
                if current.status == "running" and current.last_message_id:
                    current.messages[-1]["kind"] = "assistant"
                    current.status = "completed"
                    current.completed_at = last_visible_at or event_time(record)
    for turn in turns:
        if turn.elapsed_ms is None and turn.completed_at is not None and turn.started_at is not None:
            turn.elapsed_ms = max(0, round((turn.completed_at - turn.started_at) * 1000))
    return turns


class ClaudeSessions:
    def __init__(self, config):
        self.config = config
        self.home = Path(config["claude_home"]).expanduser().resolve() if config.get("claude_home") else None
        self.processes = {}
        self.process_lock = threading.Lock()

    def sdk(self, name, *args, **kwargs):
        if self.home is None:
            raise OSError("Claude integration is disabled")
        target = str(self.config["state_dir"] / "claude-sdk")
        with SDK_LOCK:
            if target not in sys.path:
                sys.path.insert(0, target)
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
            try:
                sdk = importlib.import_module("claude_agent_sdk")
                return getattr(sdk, name)(*args, **kwargs)
            except ImportError as error:
                raise OSError("Install the Claude Agent SDK") from error
            finally:
                if old is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def path(self, thread):
        raw = session_id(thread)
        if raw is None or self.home is None:
            return None
        directory = self.home / "projects"
        for path in directory.glob(f"*/{raw}.jsonl"):
            if path.is_file() and path.resolve().is_relative_to(directory.resolve()):
                return path
        return None

    def draft(self, db, thread):
        return db.execute("SELECT * FROM claude_phone WHERE thread_id=?", (thread,)).fetchone()

    def project(self, cwd):
        if not cwd or os.path.realpath(cwd) == str(self.home / "coding-love" / "workspace"):
            return UNASSIGNED, "Claude · 未归类"
        return project_id(cwd), project_name(cwd)

    def catalog(self, db):
        if self.home is None:
            return [], []
        rows = {row["thread_id"]: row for row in db.execute("SELECT * FROM claude_phone")}
        usage_time = dict(db.execute("SELECT thread_id, MAX(observed_ns) FROM claude_usage GROUP BY thread_id"))
        interrupted_time = dict(db.execute("""
            SELECT thread_id, MAX(ended_ns) FROM (
                SELECT thread_id, ended_ns FROM claude_interruptions
                UNION ALL SELECT thread_id, observed_ns FROM claude_turn_failures
            ) GROUP BY thread_id
            """))
        try:
            native = self.sdk("list_sessions")
        except (OSError, ValueError):
            native = []
        projects = {UNASSIGNED: "Claude · 未归类"}
        entries = []
        for item in native:
            thread = PREFIX + item.session_id
            if session_id(thread) is None:
                continue
            path = self.path(thread)
            if path is None:
                continue
            details = rows.pop(thread, None)
            try:
                stat = path.stat()
            except OSError:
                continue
            cwd = item.cwd or (details["cwd"] if details is not None else "")
            project, name = self.project(cwd)
            projects[project] = name
            entries.append({"id": thread, "title": clean_title(item.custom_title or item.summary or "新对话"),
                            "project": projects[project], "project_id": project,
                            "activity_at": stat.st_mtime_ns // 1_000_000, "archived": bool(details and details["archived"]),
                            "history_revision": history_revision(stat, max(stat.st_mtime_ns, usage_time.get(thread, 0),
                                                                           interrupted_time.get(thread, 0)),
                                                                 version=CLAUDE_HISTORY_VERSION)})
        for thread, row in rows.items():
            if not row["cwd"] or self.path(thread) is not None:
                continue
            project = row["project_id"]
            projects[project] = "Claude · 未归类" if project == UNASSIGNED else project_name(row["cwd"])
            entries.append({"id": thread, "title": row["title"], "project": projects[project],
                            "project_id": project, "activity_at": row["created_ms"], "archived": bool(row["archived"]),
                            "history_revision": ""})
        return [{"id": ident, "name": name} for ident, name in projects.items()], entries

    def create(self, db, project, request):
        if self.home is None:
            return {"status": "desktop_unavailable"}
        previous = db.execute("SELECT * FROM claude_phone WHERE request_id=?", (request,)).fetchone()
        if previous is not None:
            if previous["project_id"] != project:
                raise ValueError("Creation ID was reused")
            return {"status": "created", "thread_id": previous["thread_id"]}
        if project == UNASSIGNED:
            cwd = str(self.home / "coding-love" / "workspace")
            Path(cwd).mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            try:
                cwd = next((item.cwd for item in self.sdk("list_sessions")
                            if item.cwd and project_id(item.cwd) == project and Path(item.cwd).is_dir()), None)
            except (OSError, ValueError):
                cwd = None
        if not cwd:
            return {"status": "project_unavailable"}
        thread = PREFIX + str(uuid.uuid4())
        with db:
            db.execute("""INSERT INTO claude_phone (thread_id, request_id, cwd, project_id, title, created_ms)
                          VALUES (?, ?, ?, ?, ?, ?)""",
                       (thread, request, cwd, project, "新对话", int(time.time() * 1000)))
        return {"status": "created", "thread_id": thread}

    def rename(self, thread, title):
        try:
            if self.path(thread) is not None:
                self.sdk("rename_session", session_id(thread), title)
            else:
                with closing(open_database(self.config)) as db, db:
                    if not db.execute("UPDATE claude_phone SET title=? WHERE thread_id=?", (title, thread)).rowcount:
                        return {"status": "unavailable"}
        except (OSError, ValueError):
            return {"status": "desktop_unavailable"}
        return {"status": "renamed", "title": title}

    def fork(self, db, thread, turn_id, request):
        previous = db.execute("SELECT * FROM phone_forks WHERE request_id=?", (request,)).fetchone()
        if previous is not None:
            if previous["parent_id"] != thread or previous["turn_id"] != turn_id:
                raise ValueError("Fork ID was reused")
            child = previous["child_id"]
            if child is None:
                return {"status": "unknown"}
        else:
            path = self.path(thread)
            if path is None:
                return {"status": "unavailable"}
            try:
                turn = next((t for t in read_claude(path, history_revision(path.stat()))
                             if t.id == turn_id and t.last_message_id and t.status != "running"), None)
                original = self.sdk("get_session_info", session_id(thread))
                if turn is None or original is None:
                    return {"status": "unavailable"}
                group, _ = self.project(original.cwd)
                title = clean_title(original.custom_title or original.summary or "Claude")[:105] + "（分支）"
                with db:
                    db.execute("""INSERT INTO phone_forks (request_id, parent_id, turn_id, project_id)
                                  VALUES (?, ?, ?, ?)""", (request, thread, turn_id, group))
                child = PREFIX + self.sdk("fork_session", session_id(thread),
                                          up_to_message_id=turn.last_message_id, title=title).session_id
                with db:
                    db.execute("UPDATE phone_forks SET child_id=?, finished=1 WHERE request_id=?", (child, request))
            except (OSError, ValueError):
                return {"status": "unavailable"}
        try:
            info = self.sdk("get_session_info", session_id(child))
        except (OSError, ValueError):
            info = None
        group, label = self.project(info.cwd if info is not None else "")
        return {"status": "created", "thread_id": child, "project_id": group, "project": label}

    def archive(self, thread, archived):
        with closing(open_database(self.config)) as db, db:
            if self.path(thread) is None and self.draft(db, thread) is None:
                return {"status": "unavailable"}
            if self.running(thread):
                return {"status": "busy"}
            db.execute("""INSERT INTO claude_phone (thread_id, archived) VALUES (?, ?)
                          ON CONFLICT(thread_id) DO UPDATE SET archived=excluded.archived""",
                       (thread, int(archived)))
        return {"status": "archived" if archived else "active"}

    def running(self, thread):
        with self.process_lock:
            return thread in self.processes

    def desktop_open(self, thread):
        raw = session_id(thread)
        for marker in (self.home / "sessions").glob("*.json"):
            try:
                entry = json.loads(marker.read_text())
                if entry.get("sessionId") == raw and type(entry.get("pid")) is int:
                    os.kill(entry["pid"], 0)
                    command = Path(f"/proc/{entry['pid']}/comm").read_text().strip().lower()
                    if "claude" in command:
                        return True
            except (OSError, ValueError, AttributeError):
                continue
        return False

    def workdir(self, saved, thread, existing):
        cwd = saved["cwd"] if saved is not None else None
        if existing:
            try:
                info = self.sdk("get_session_info", session_id(thread))
                cwd = (info.cwd or cwd) if info is not None else cwd
            except (OSError, ValueError):
                pass
        return cwd if cwd and Path(cwd).is_dir() else None

    def prepare(self, db, thread):
        if self.home is None:
            return "unavailable", None, False
        saved = self.draft(db, thread)
        if saved is not None and saved["archived"]:
            return "archived", None, False
        existing = self.path(thread) is not None
        if not existing and (saved is None or not saved["cwd"]):
            return "unavailable", None, False
        if self.running(thread):
            return "busy", None, existing
        if existing and self.desktop_open(thread):
            return "desktop_unavailable", None, existing
        cwd = self.workdir(saved, thread, existing)
        if cwd is None:
            return "project_unavailable", None, existing
        return "ready", cwd, existing

    def executable(self):
        return (self.config.get("claude_executable") or shutil.which("claude")
                or str(Path.home() / ".local" / "bin" / "claude"))

    def control_info(self, cwd, subtype):
        if self.home is None:
            raise OSError("Claude integration is disabled")
        requests = [{"subtype": "initialize"}]
        if subtype != "initialize":
            requests.append({"subtype": subtype, "skip_behaviors": True})
        lines = [json.dumps({"type": "control_request", "request_id": item["subtype"], "request": item})
                 for item in requests]
        command = [self.executable(), "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.home)}
        if subtype == "get_usage":
            if self.home == Path.home() / ".claude":
                # The default CLI profile reads ~/.claude.json when no directory override is set.
                env.pop("CLAUDE_CONFIG_DIR")
        else:
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        try:
            result = subprocess.run(command, cwd=cwd, env=env,
                                    input=("\n".join(lines) + "\n").encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12)
        except subprocess.TimeoutExpired as error:
            raise OSError("Claude query timed out") from error
        for line in result.stdout.splitlines():
            try:
                response = json.loads(line).get("response", {})
            except (ValueError, AttributeError):
                continue
            if response.get("request_id") == subtype and response.get("subtype") == "success":
                return response.get("response", {})
        raise OSError("Claude did not return the requested information")

    def models(self, cwd):
        choices = self.control_info(cwd, "initialize").get("models")
        if not isinstance(choices, list):
            raise OSError("Claude did not return its model list")
        models = {}
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            value, label = choice.get("value"), choice.get("displayName")
            if isinstance(value, str) and value:
                efforts = [level for level in choice.get("supportedEffortLevels", []) if isinstance(level, str)]
                key = "" if value == "default" else value
                models[key] = {"id": key, "label": label if isinstance(label, str) and label else value,
                               "efforts": efforts, "default": value == "default"}
        return list(models.values())

    def usage(self, thread):
        with closing(open_database(self.config)) as db:
            rows = db.execute("SELECT turn_id, observed_ns, windows FROM claude_usage WHERE thread_id=?",
                              (thread,)).fetchall()
            endings = db.execute("""
                SELECT turn_id, status, ended_ns, observed_ns FROM claude_turn_failures WHERE thread_id=?
                UNION ALL
                SELECT turn_id, 'interrupted' AS status, ended_ns, ended_ns AS observed_ns
                FROM claude_interruptions WHERE thread_id=?
            """, (thread, thread)).fetchall()
        modified = max(max((r["observed_ns"] for r in rows), default=0),
                       max((r["observed_ns"] for r in endings), default=0))
        return (modified, {r["turn_id"]: json.loads(r["windows"]) for r in rows},
                {r["turn_id"]: (r["status"], r["ended_ns"]) for r in endings})

    def record_failure(self, thread, turn, status, windows=(), *, ended=None):
        failure_time = int(ended * 1_000_000_000) if ended is not None else time.time_ns()
        quota = {window[0]: window for window in windows}
        with closing(open_database(self.config)) as db, db:
            db.execute("INSERT OR IGNORE INTO claude_turn_failures VALUES (?, ?, ?, ?, ?)",
                       (thread, turn, status, failure_time, time.time_ns()))
        if status == "quota" and not all(quota.get(minutes, (0, 0, None))[2] for minutes in (300, 10080)):
            from account_usage import claude_usage

            try:
                usage = claude_usage(self)
            except (OSError, ValueError):
                usage = {}
            for minutes in (300, 10080):
                window = usage.get(str(minutes))
                if window and window["reset"] is not None and not quota.get(minutes, (0, 0, None))[2]:
                    quota[minutes] = (minutes, window["remaining"], window["reset"])
        if quota:
            with closing(open_database(self.config)) as db, db:
                db.execute("INSERT OR REPLACE INTO claude_usage VALUES (?, ?, ?, ?)",
                           (thread, turn, time.time_ns(), json.dumps(list(quota.values()))))
        return quota.values()

    def start(self, thread, turn, text, cwd, existing, title=None, *, model=None, effort=None, images=()):
        raw = session_id(thread)
        command = [self.executable(), "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--permission-mode", "acceptEdits",
                   "--resume" if existing else "--session-id", raw]
        if not existing and title and title != "新对话":
            command += ["--name", title]
        if model is not None:
            command.append("--model=" + model)
        if effort is not None:
            command.append("--effort=" + effort)
        try:
            process = subprocess.Popen(command, cwd=cwd, env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.home),
                                                             "CODING_LOVE_CLAUDE_TURN": turn},
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return "unavailable", None
        content = [{"type": "text", "text": text}, *images] if images else text
        prompt = {"type": "user", "session_id": "", "uuid": turn,
                  "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        run = ClaudeRun(process, turn)
        try:
            self.write(run, prompt)
        except OSError:
            process.terminate()
            return "unavailable", None
        with self.process_lock:
            self.processes[thread] = run
        threading.Thread(target=self.finish, args=(thread, run), daemon=True).start()
        return "accepted", turn

    @staticmethod
    def write(run, message):
        run.process.stdin.write((json.dumps(message) + "\n").encode())
        run.process.stdin.flush()

    def interrupt(self, thread, turn_id):
        with self.process_lock:
            run = self.processes.get(thread)
            if run is None:
                return "unavailable"
            if run.turn != turn_id or run.finishing or run.process.poll() is not None:
                return "not_running"
            if run.interrupt_id is None:
                run.interrupt_id = str(uuid.uuid4())
                try:
                    self.write(run, {"type": "control_request", "request_id": run.interrupt_id,
                                     "request": {"subtype": "interrupt"}})
                except OSError:
                    run.interrupt_id = None
                    return "unknown"
            return "requested"

    def finish(self, thread, run):
        process = run.process
        try:
            final = None
            quota = {}
            rate_limited = False
            interrupted = False
            for line in process.stdout:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("type") == "result":
                    final = item
                    with self.process_lock:
                        run.finishing = True
                        process.stdin.close()
                elif isinstance(item, dict) and item.get("type") == "control_response":
                    response = item.get("response") or {}
                    with self.process_lock:
                        if run.interrupt_id is not None and response.get("request_id") == run.interrupt_id:
                            interrupted = response.get("subtype") == "success"
                            if not interrupted:
                                run.interrupt_id = None
                elif isinstance(item, dict) and item.get("type") == "rate_limit_event":
                    info = item.get("rate_limit_info")
                    if isinstance(info, dict):
                        window = {"five_hour": 300, "seven_day": 10080}.get(info.get("rateLimitType"))
                        used, reset = info.get("utilization"), info.get("resetsAt")
                        if window and type(used) in (int, float) and 0 <= used <= 1:
                            quota[window] = (window, round(100 * (1 - used), 2),
                                             reset if type(reset) is int and reset > 0 else None)
                        rate_limited = info.get("status") == "rejected"
            process.wait()
            if interrupted:
                with closing(open_database(self.config)) as db, db:
                    db.execute("INSERT OR IGNORE INTO claude_interruptions VALUES (?, ?, ?)",
                               (thread, run.turn, time.time_ns()))
                return
            path = self.path(thread)
            if path is None:
                return
            turns = read_claude(path, str(path.stat().st_mtime_ns))
            turn = next((entry for entry in turns if entry.id == run.turn), None)
            if turn is not None and final:
                failed = bool(final.get("is_error"))
                hint = terminal_status([final.get("subtype"), final.get("errors"), final.get("result")]) if failed else None
                status = ("quota" if rate_limited and failed else
                          turn.status if turn.status in ("quota", "network") else hint)
                windows = quota.values()
                if status in ("quota", "network"):
                    windows = self.record_failure(thread, turn.id, status, windows, ended=turn.completed_at)
                elif quota:
                    with closing(open_database(self.config)) as db, db:
                        db.execute("INSERT OR REPLACE INTO claude_usage VALUES (?, ?, ?, ?)",
                                   (thread, turn.id, time.time_ns(), json.dumps(list(windows))))
                if not failed or status == "quota":
                    queue_completion(self.config, self, thread, turn, quota=status == "quota", windows=windows)
        finally:
            process.stdin.close()
            process.stdout.close()
            with self.process_lock:
                self.processes.pop(thread, None)

    def close(self):
        with self.process_lock:
            for run in self.processes.values():
                if run.process.poll() is None:
                    run.process.terminate()


def project_name(cwd):
    if not cwd:
        return "Claude · 未归类"
    return "Claude · " + (Path(cwd).name or cwd)


def queue_completion(config, claude, thread, turn, *, ended=None, quota=False, windows=()):
    if not quota and not any(m["kind"] == "assistant" for m in turn.messages):
        return
    name = "未命名对话"
    try:
        info = claude.sdk("get_session_info", session_id(thread))
        if info is not None:
            name = clean_title(info.custom_title or info.summary or name)
    except (OSError, ValueError):
        pass
    completed_at = ended or turn.completed_at
    if completed_at is not None:
        current = [(minutes, remaining, reset) for minutes, remaining, reset in windows
                   if reset is None or reset > completed_at]
        enqueue(config, "claude", turn.id, thread_id=thread, thread_title=name, completed_at=completed_at,
                windows={minutes: remaining for minutes, remaining, _ in current},
                reset_times={minutes: reset for minutes, _, reset in current if reset is not None},
                quota_interrupted=quota)
