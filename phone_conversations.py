"""Conversation access for the paired phone, anchored to a delivered reminder."""
from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import codex_ipc
from claude_sessions import CLAUDE_HISTORY_VERSION, ClaudeSessions, PROJECT_PREFIX, read_claude, session_id as claude_id
from codex_app_server import AppServerError, CodexAppServer
from codex_conversation import (
    HISTORY_VERSION, find_rollout, fork_parent, history_revision, iter_rollouts, native_turns,
    read_conversation, split_event_id, valid_thread_id,
)
from notification_queue import conversation_names, open_database, valid_id
from project_labels import ProjectLabels
from reply_attachments import parse_attachments, prepare_attachments
from turn_changes import Conflict, read_text, replay, summary

HISTORY_REVISION = re.compile(r"(?:([0-9]{1,3})-)?([0-9a-f]{1,20})-([0-9a-f]{1,20})-([0-9a-f]{1,20})")


def native_sync_key(row, stat=None):
    revision = f"native-{row.get('updatedAt', 0)}-{row.get('recencyAt', 0)}"
    return revision if stat is None else revision + "-" + history_revision(stat)


@lru_cache(maxsize=4)
def visible_history(path, revision, byte_limit):
    return read_conversation(path, byte_limit=byte_limit)


class PhoneConversations:
    def __init__(self, config):
        self.config = config
        self.socket_path = config["sessions_dir"].parent / "ipc" / "ipc.sock"
        self.submit_lock = threading.Lock()
        self.app_server = CodexAppServer(config)
        self.claude = ClaudeSessions(config)
        self.native_pages = OrderedDict()
        self.native_pages_lock = threading.Lock()
        self.model_catalogs = OrderedDict()
        self.model_lock = threading.Lock()

    def models(self, db, thread, *, cwd=None):
        is_claude = claude_id(thread) is not None
        if is_claude and cwd is None:
            cwd = self.claude.workdir(self.claude.draft(db, thread), thread, self.claude.path(thread) is not None)
            if cwd is None:
                raise OSError("Claude workspace unavailable")
        key = ("claude", cwd) if is_claude else ("codex", None)
        with self.model_lock:
            cached = self.model_catalogs.get(key)
            if cached is not None and time.monotonic() - cached[0] < 45:
                return cached[1]
            options = self.claude.models(cwd) if is_claude else self.app_server.models()
            self.model_catalogs[key] = (time.monotonic(), options)
            self.model_catalogs.move_to_end(key)
            if len(self.model_catalogs) > 16:
                self.model_catalogs.popitem(last=False)
            return options

    def effort_options(self, thread, options, model=None):
        if model is None and not claude_id(thread):
            try:
                model = self.app_server.read_thread(thread).get("model")
            except (OSError, AppServerError):
                pass
        choice = next((option for option in options if option["id"] == model), None)
        if choice is None:
            choice = next((option for option in options if option.get("default")), {})
        return choice.get("efforts", [])

    def model_menu(self, db, thread):
        choices = self.models(db, thread)
        menu = {"models": [item for item in choices if item["id"]]}
        if any(item.get("efforts") for item in choices):
            menu["default_efforts"] = self.effort_options(thread, choices)
        return menu

    def selection_error(self, db, thread, model, effort, *, cwd=None):
        if model is None and effort is None:
            return None
        try:
            choices = self.models(db, thread, cwd=cwd)
        except (OSError, AppServerError):
            return "model_unavailable" if model is not None else "effort_unavailable"
        if model is not None and not any(option["id"] == model for option in choices):
            return "model_unavailable"
        if effort is not None and effort not in self.effort_options(thread, choices, model):
            return "effort_unavailable"
        return None

    def anchor(self, db, event_id):
        identifiers = split_event_id(event_id)
        if identifiers is None or db.execute("""
                SELECT 1 FROM notifications
                WHERE event_key=? AND message_id='tailscale:posted' AND sent_at IS NOT NULL
                """, (event_id,)).fetchone() is None:
            return None
        thread, turn = identifiers
        path = find_rollout(self.config, thread)
        return (path, thread, turn) if path is not None else None

    def snapshot(self, db, event_id, revision):
        anchor = self.anchor(db, event_id)
        if anchor is None:
            return None
        path, _, turn = anchor
        try:
            stat = path.stat()
            current = history_revision(stat)
            if revision == current:
                return {"unchanged": True}
            turns = read_conversation(path, turn)
        except (OSError, ValueError):
            return None
        if not turns:
            return None
        return {"revision": current, "turns": [entry.public() for entry in turns]}

    def catalog(self, db):
        names = conversation_names(self.config)
        projects = ProjectLabels(self.config)
        activity = {}
        for record in db.execute("""
                SELECT event_key, completed_at, payload FROM notifications
                WHERE event_key NOT LIKE 'external:%' ORDER BY completed_at DESC
                """):
            parsed = split_event_id(record["event_key"])
            if parsed is not None and parsed[0] not in activity:
                payload = json.loads(record["payload"])
                activity[parsed[0]] = (int(record["completed_at"] * 1000),
                                       payload.get("summary") or payload.get("message", ""))
        threads = {}
        for ident, path, cwd in iter_rollouts(self.config):
            if ident in threads:
                continue
            try:
                timestamp = path.stat().st_mtime_ns // 1_000_000
            except OSError:
                continue
            timestamp, preview = activity.get(ident, (timestamp, ""))
            threads[ident] = {"id": ident, "title": names.get(ident, "未命名对话"),
                              "project_id": projects.project_id(ident, cwd),
                              "project": projects.label(ident, cwd),
                              "archived": not path.is_relative_to(self.config["sessions_dir"]),
                              "activity_at": timestamp, "preview": preview[:200]}
        _, claude_threads = self.claude.catalog(db)
        for entry in claude_threads:
            _, entry["preview"] = activity.get(entry["id"], (0, ""))
            threads[entry["id"]] = entry
        return {"threads": sorted(threads.values(), key=lambda entry: (-entry["activity_at"], entry["id"]))}

    def projects(self, revision):
        projects = ProjectLabels(self.config)
        indexed = self.app_server.threads()
        sessions = self.config["sessions_dir"]
        names = conversation_names(self.config)
        rollouts = {}
        for ident, path, cwd in iter_rollouts(self.config, all_sources=True):
            rollouts.setdefault(ident, (path, cwd))

        entries = []
        for ident in indexed.keys() | rollouts.keys():
            if not valid_thread_id(ident):
                continue
            row = indexed.get(ident, {})
            transcript_revision = native_sync_key(row) if row else ""
            activity = int((max(row.get(key) or 0 for key in ("recencyAt", "updatedAt", "createdAt"))) * 1000)
            cwd = row.get("cwd")
            archived = row.get("archived", False)
            if ident in rollouts:
                path, cwd = rollouts[ident]
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if fork_parent(path):
                    transcript_revision = native_sync_key(row, stat)
                else:
                    transcript_revision = history_revision(stat)
                activity = max(activity, stat.st_mtime_ns // 1_000_000)
                archived = not path.is_relative_to(sessions)
            project = projects.project_id(ident, cwd)
            title = next((" ".join(value.split())[:140] for value in
                          (row.get("name"), names.get(ident), row.get("title"), row.get("preview"))
                          if isinstance(value, str) and value.strip()), "未命名对话")
            entries.append({"id": ident, "title": title, "project_id": project,
                            "project": projects.names.get(project, "未归类"),
                            "archived": archived, "activity_at": activity,
                            "history_revision": transcript_revision})
        with closing(open_database(self.config)) as db:
            claude_projects, claude_threads = self.claude.catalog(db)
        project_rows = [{"id": ident, "name": name} for ident, name in projects.names.items()]
        result = {
            "projects": sorted(project_rows + claude_projects,
                               key=lambda row: (row["name"].casefold(), row["id"])),
            "threads": sorted(entries + claude_threads, key=lambda row: (-row["activity_at"], row["id"])),
        }
        version = hashlib.sha256(json.dumps(result, ensure_ascii=False).encode()).hexdigest()
        return {"unchanged": True} if revision == version else {**result, "revision": version}

    def create(self, db, data):
        if not isinstance(data, dict) or set(data) != {"project_id", "request_id"}:
            raise ValueError("Invalid new conversation")
        project, request_id = data["project_id"], data["request_id"]
        if not isinstance(project, str) or len(project) > 128 or not valid_request_id(request_id):
            raise ValueError("Invalid new conversation identifiers")
        with self.submit_lock:
            if project.startswith(PROJECT_PREFIX):
                return self.claude.create(db, project, request_id)
            pending = db.execute("SELECT project_id, thread_id FROM phone_threads WHERE request_id=?",
                                 (request_id,)).fetchone()
            if pending is not None and project != pending["project_id"]:
                raise ValueError("Creation ID was reused")
            thread = pending["thread_id"] if pending is not None else None
            if thread and find_rollout(self.config, thread, all_sources=True) is not None:
                return {"status": "created", "thread_id": thread}

            projects = ProjectLabels(self.config)
            if project:
                workdir = next((Path(root).resolve() for root in projects.project_roots.get(project, ())
                                if Path(root).is_dir()), None)
                if workdir is None or projects.project_id(None, str(workdir)) != project:
                    return {"status": "project_unavailable"}
            else:
                workdir = self.config["sessions_dir"].parent / "coding-love" / "workspace"
                if projects.project_id(None, str(workdir)):
                    return {"status": "project_unavailable"}
                workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                if thread is not None:
                    # A timed-out name write can be retried while the original empty thread is loaded.
                    try:
                        self.app_server.request("thread/name/set", {"threadId": thread, "name": "新对话"})
                        return {"status": "created", "thread_id": thread}
                    except AppServerError:
                        if find_rollout(self.config, thread, all_sources=True) is not None:
                            return {"status": "created", "thread_id": thread}
                started = self.app_server.request("thread/start", {
                    "cwd": str(workdir), "sandbox": "workspace-write", "threadSource": "coding-love",
                })
                thread = (started.get("thread") or {}).get("id")
                if not valid_thread_id(thread):
                    return {"status": "desktop_unavailable"}
                with db:
                    db.execute("INSERT OR REPLACE INTO phone_threads VALUES (?, ?, ?)",
                               (request_id, project, thread))
                # Naming a new empty thread materializes the native rollout before the first prompt.
                self.app_server.request("thread/name/set", {"threadId": thread, "name": "新对话"})
                return {"status": "created", "thread_id": thread}
            except (AppServerError, OSError):
                return {"status": "desktop_unavailable"}

    def thread_page(self, thread_id, revision, offset):
        if not valid_thread_id(thread_id):
            return None
        is_claude = claude_id(thread_id) is not None
        path = self.claude.path(thread_id) if is_claude else find_rollout(self.config, thread_id, all_sources=True)
        if is_claude and path is None:
            with closing(open_database(self.config)) as db:
                saved = self.claude.draft(db, thread_id)
            if saved is None or not saved["cwd"]:
                return None
            draft_revision = "draft-" + thread_id
            if revision == draft_revision:
                return {"unchanged": True}
            return self.page([], draft_revision, saved["created_ms"] * 1000, 0)
        if path is None or (not is_claude and fork_parent(path)):
            return self.native_thread_page(thread_id, revision, offset, path)
        stat = path.stat()
        usage_time, usage, stops = self.claude.usage(thread_id) if is_claude else (0, {}, {})
        current_version = CLAUDE_HISTORY_VERSION if is_claude else HISTORY_VERSION
        current = history_revision(stat, max(stat.st_mtime_ns, usage_time), version=current_version)
        if offset is None:
            if revision == current:
                return {"unchanged": True}
            revision = current
            offset = 0
        match = HISTORY_REVISION.fullmatch(revision)
        if match is None:
            raise ValueError("Invalid history revision")
        version, *parts = match.groups()
        if version != current_version:
            return {"changed": True}
        inode, size, modified = (int(part, 16) for part in parts)
        if inode != stat.st_ino or size > stat.st_size or (size == stat.st_size and revision != current):
            return {"changed": True}
        turns = read_claude(path, revision, size) if is_claude else visible_history(path, revision, size)
        if usage:
            turns = [replace(turn, windows=usage[turn.id]) if turn.id in usage else turn for turn in turns]
        if stops:
            turns = list(turns)
            for index, turn in enumerate(turns):
                if turn.id not in stops:
                    continue
                status, ended_ns = stops[turn.id]
                ended = ended_ns / 1_000_000_000
                elapsed = max(0, int((ended - turn.started_at) * 1000)) if turn.started_at is not None else turn.elapsed_ms
                turns[index] = replace(turn, status=status, completed_at=ended, elapsed_ms=elapsed)
        return self.page(turns, revision, modified // 1000, offset)

    def native_history(self, thread_id, path):
        turns = native_turns(self.app_server.turns(thread_id))
        logs = {}
        checked = {thread_id}
        # Forked rollouts contain subsequent events. Codex's history store supplies the inherited prefix.
        while path is not None:
            for turn in read_conversation(path):
                logs.setdefault(turn.id, turn)
            parent = fork_parent(path)
            if parent is None or parent in checked:
                break
            checked.add(parent)
            path = find_rollout(self.config, parent, all_sources=True)
        return [logs.get(turn.id, turn) for turn in turns]

    def native_thread_page(self, thread_id, revision, offset, path=None):
        if offset is None:
            try:
                row = self.app_server.read_thread(thread_id)
                turns = self.native_history(thread_id, path)
                stat = path.stat() if path is not None else None
            except (OSError, AppServerError):
                return None
            encoded = json.dumps([turn.public() for turn in turns], ensure_ascii=False)
            current = "native-" + hashlib.sha256(encoded.encode()).hexdigest()
            sync_key = native_sync_key(row, stat)
            if revision == current:
                return {"unchanged": True, "cache_revision": sync_key}
            modified = int((row.get("updatedAt") or row.get("createdAt") or 0) * 1_000_000)
            if stat is not None:
                modified = max(modified, stat.st_mtime_ns // 1000)
            snapshot = turns, modified, sync_key
            key = (thread_id, current)
            with self.native_pages_lock:
                self.native_pages[key] = snapshot
                self.native_pages.move_to_end(key)
                if len(self.native_pages) > 4:
                    self.native_pages.popitem(last=False)
            revision, offset = current, 0
        else:
            with self.native_pages_lock:
                snapshot = self.native_pages.get((thread_id, revision))
            if snapshot is None:
                return {"changed": True}
        turns, modified, sync_key = snapshot
        return {**self.page(turns, revision, modified, offset), "cache_revision": sync_key}

    @staticmethod
    def page(turns, revision, modified, offset):
        page = []
        used = 0
        for index in range(offset, min(len(turns), offset + 32)):
            turn = turns[index].public()
            length = len(json.dumps(turn, ensure_ascii=False).encode("utf-8"))
            if page and used + length > 384 * 1024:
                break
            used += length
            page.append(turn)
        next_offset = offset + len(page)
        return {"revision": revision, "modified_at": modified, "turns": page,
                "next_offset": next_offset if next_offset < len(turns) else -1}

    def set_archive(self, data, *, archived):
        if not isinstance(data, dict) or set(data) != {"thread_id"} or not valid_thread_id(data["thread_id"]):
            raise ValueError("Invalid thread ID")
        thread = data["thread_id"]
        with self.submit_lock:
            if claude_id(thread) is not None:
                return self.claude.archive(thread, archived)
            path = find_rollout(self.config, thread, all_sources=True)
            try:
                if path is None:
                    row = self.app_server.threads().get(thread)
                    if row is None:
                        return {"status": "unavailable"}
                    current = row["archived"]
                else:
                    current = not path.is_relative_to(self.config["sessions_dir"])
                if current != archived:
                    if archived:
                        turns = native_turns(self.app_server.turns(thread)) if path is None else read_conversation(path)
                        if turns and turns[-1].status == "running":
                            return {"status": "busy"}
                    method = "thread/archive" if archived else "thread/unarchive"
                    self.app_server.request(method, {"threadId": thread})
            except (OSError, AppServerError):
                return {"status": "desktop_unavailable"}
            return {"status": "archived" if archived else "active"}

    def thread_location(self, thread):
        for _, path, cwd in iter_rollouts(self.config, thread, all_sources=True):
            return path, cwd
        return None, self.app_server.read_thread(thread).get("cwd")

    def workspace(self, thread):
        if claude_id(thread) is None:
            return self.thread_location(thread)
        path = self.claude.path(thread)
        with closing(open_database(self.config)) as db:
            saved = self.claude.draft(db, thread)
        return path, self.claude.workdir(saved, thread, path is not None)

    def workspace_file(self, cwd, name):
        if not isinstance(name, str) or not 0 < len(name) <= 4096 or "\0" in name:
            raise ValueError("无效文件路径")
        root = Path(cwd).resolve() if cwd else None
        requested = Path(name)
        if not requested.is_absolute():
            if root is None:
                raise Conflict("会话工作目录不可用")
            requested = root / requested
        path = requested.resolve()
        if path != requested or not path.parent.is_dir():
            raise Conflict("文件路径不可用")
        home = Path.home()
        repo = next((parent for parent in path.parents if (parent / ".git").exists()), None)
        in_workspace = (root is not None and root not in (Path("/"), home, home.parent)
                        and path.is_relative_to(repo if repo and root.is_relative_to(repo) else root))
        in_home_repo = repo is not None and repo != home and repo.is_relative_to(home)
        if not (in_workspace or in_home_repo):
            raise Conflict("文件不在会话工作目录或电脑上的项目仓库内")
        relative = path.relative_to(repo if repo and path.is_relative_to(repo) else root)
        if in_home_repo and any(part.startswith(".") for part in repo.relative_to(home).parts):
            raise Conflict("该目录属于电脑的本地配置")
        if any(part in {".git", ".ssh", ".gnupg", ".codex", ".claude", ".state"} or part.startswith(".env")
               for part in relative.parts):
            raise Conflict("该文件属于电脑的本地配置")
        return path

    def file_preview(self, thread, name, line):
        if not valid_thread_id(thread) or type(line) is not int or not 1 <= line <= 100_000_000:
            raise ValueError("无效文件链接")
        try:
            _, cwd = self.workspace(thread)
            path = self.workspace_file(cwd, name)
            text = read_text(path)
            if text is None:
                return {"status": "missing"}
            lines = text.splitlines()
            total = max(1, len(lines))
            line = min(total, line)
            start = max(1, line - 15)
            excerpt = lines[start - 1:start + 104]
            return {"status": "ok", "path": str(path), "start": start, "line": line, "total": total,
                    "lines": [value[:4000] for value in excerpt]}
        except Conflict as error:
            return {"status": "unavailable", "message": str(error)}
        except (OSError, AppServerError, RuntimeError):
            return {"status": "unavailable"}

    def change_file(self, db, data):
        if not isinstance(data, dict) or set(data) != {"thread_id", "turn_id", "path", "action"}:
            raise ValueError("无效文件操作")
        thread, turn_id, name, action = (data[key] for key in ("thread_id", "turn_id", "path", "action"))
        if not valid_thread_id(thread) or action not in ("undo", "redo"):
            raise ValueError("无效文件操作")
        valid_id(turn_id)
        if not isinstance(name, str):
            raise ValueError("无效文件路径")
        with self.submit_lock:
            try:
                path, cwd = self.workspace(thread)
                if claude_id(thread) is not None:
                    if self.claude.running(thread):
                        return {"status": "busy"}
                    turns = read_claude(path, history_revision(path.stat())) if path else []
                elif path is None or fork_parent(path):
                    turns = self.native_history(thread, path)
                else:
                    turns = read_conversation(path)
                if turns and turns[-1].status == "running":
                    return {"status": "busy"}
                turn = next((item for item in turns if item.id == turn_id), None)
                if turn is None or name not in turn.files:
                    return {"status": "unavailable"}
                edits = turn.changes(name)
                later = turns[turns.index(turn) + 1:]
                target = self.workspace_file(cwd, name)
                if not summary(name, edits)["replayable"]:
                    return {"status": "unavailable"}
                if any(target == (Path(cwd or "/") / filename).resolve()
                       for item in later for filename in item.files):
                    raise Conflict("同一对话的后续轮次也修改了该文件")
                replay(db, thread, turn_id, str(target), target, edits, action)
                return {"status": "undone" if action == "undo" else "applied"}
            except Conflict as error:
                return {"status": "conflict", "message": str(error)}
            except (OSError, AppServerError, RuntimeError):
                return {"status": "unavailable"}

    def move(self, data):
        if (not isinstance(data, dict) or set(data) != {"thread_id", "project_id"}
                or not valid_thread_id(data["thread_id"]) or not isinstance(data["project_id"], str)
                or len(data["project_id"]) > 128):
            raise ValueError("Invalid project assignment")
        thread, project = data["thread_id"], data["project_id"]
        if claude_id(thread) is not None or project.startswith(PROJECT_PREFIX):
            return {"status": "unavailable"}
        with self.submit_lock:
            try:
                _, cwd = self.thread_location(thread)
            except (OSError, AppServerError):
                return {"status": "unavailable"}
            projects = ProjectLabels(self.config)
            try:
                if not projects.assign(thread, project):
                    return {"status": "project_unavailable"}
            except sqlite3.Error:
                return {"status": "project_unavailable"}
            effective = project or projects.project_id(None, cwd)
            return {"status": "moved", "project_id": effective,
                    "project": projects.names.get(effective, "未归类")}

    def rename(self, data):
        if (not isinstance(data, dict) or set(data) != {"thread_id", "title"}
                or not valid_thread_id(data["thread_id"]) or not isinstance(data["title"], str)
                or not 0 < len(data["title"]) <= 120):
            raise ValueError("Invalid conversation name")
        thread, title = data["thread_id"], " ".join(data["title"].split())
        if not title or any(ord(char) < 32 for char in title):
            raise ValueError("Invalid conversation name")
        with self.submit_lock:
            if claude_id(thread) is not None:
                return self.claude.rename(thread, title)
            try:
                self.thread_location(thread)
                self.app_server.request("thread/name/set", {"threadId": thread, "name": title})
            except (OSError, AppServerError):
                return {"status": "unavailable"}
            return {"status": "renamed", "title": title}

    def rename_project(self, data):
        if (not isinstance(data, dict) or set(data) != {"project_id", "name"}
                or not isinstance(data["project_id"], str) or not 0 < len(data["project_id"]) <= 128
                or not isinstance(data["name"], str) or not 0 < len(data["name"]) <= 80):
            raise ValueError("Invalid project name")
        project, name = data["project_id"], " ".join(data["name"].split())
        if not name or any(ord(char) < 32 for char in name):
            raise ValueError("Invalid project name")
        if project.startswith(PROJECT_PREFIX):
            return {"status": "unavailable"}
        with self.submit_lock:
            try:
                if not ProjectLabels(self.config).rename(project, name):
                    return {"status": "unavailable"}
            except (OSError, sqlite3.Error):
                return {"status": "unavailable"}
        return {"status": "renamed", "name": name}

    def interrupt(self, data):
        if (not isinstance(data, dict) or set(data) != {"thread_id", "turn_id"}
                or not valid_thread_id(data["thread_id"])):
            raise ValueError("Invalid interruption")
        thread, turn = data["thread_id"], valid_id(data["turn_id"])
        with self.submit_lock:
            if claude_id(thread) is not None:
                return {"status": self.claude.interrupt(thread, turn)}
            status = codex_ipc.interrupt_turn(self.socket_path, thread, turn)
            if status == "desktop_unavailable":
                status = self.app_server.interrupt(thread, turn)
            return {"status": status}

    def fork(self, db, data):
        if (not isinstance(data, dict) or set(data) != {"thread_id", "turn_id", "request_id"}
                or not valid_thread_id(data["thread_id"]) or not valid_request_id(data["request_id"])):
            raise ValueError("Invalid fork")
        parent, turn_id, request = data["thread_id"], valid_id(data["turn_id"]), data["request_id"]
        with self.submit_lock:
            if claude_id(parent) is not None:
                return self.claude.fork(db, parent, turn_id, request)
            projects = ProjectLabels(self.config)
            prior = db.execute("SELECT * FROM phone_forks WHERE request_id=?", (request,)).fetchone()
            if prior is not None:
                if parent != prior["parent_id"] or turn_id != prior["turn_id"]:
                    raise ValueError("Fork ID was reused")
                child, project = prior["child_id"], prior["project_id"]
                if child is None:
                    return {"status": "unknown"}
                if prior["finished"]:
                    return fork_result(child, project, projects)
            else:
                try:
                    path, cwd = self.thread_location(parent)
                    turns = (self.native_history(parent, path) if path is None or fork_parent(path)
                             else read_conversation(path))
                except (OSError, AppServerError):
                    return {"status": "unavailable"}
                if not any(turn.id == turn_id and turn.status != "running" for turn in turns):
                    return {"status": "unavailable"}
                project = projects.project_id(parent, cwd)
                with db:
                    db.execute("INSERT INTO phone_forks (request_id, parent_id, turn_id, project_id) VALUES (?, ?, ?, ?)",
                               (request, parent, turn_id, project))
                try:
                    forked = self.app_server.request("thread/fork", {
                        "threadId": parent, "lastTurnId": turn_id, "excludeTurns": True,
                        "deferGoalContinuation": True, "threadSource": "coding-love",
                    })
                except AppServerError:
                    with db:
                        db.execute("DELETE FROM phone_forks WHERE request_id=?", (request,))
                    return {"status": "unavailable"}
                except OSError:
                    return {"status": "unknown"}
                child = (forked.get("thread") or {}).get("id")
                if not valid_thread_id(child) or child == parent:
                    return {"status": "unknown"}
                with db:
                    db.execute("UPDATE phone_forks SET child_id=? WHERE request_id=?", (child, request))
                title = conversation_names(self.config).get(parent, "Codex")
                try:
                    self.app_server.request("thread/name/set", {"threadId": child, "name": title[:70] + "（分支）"})
                except (OSError, AppServerError):
                    pass
            try:
                if project and not projects.assign(child, project):
                    return {"status": "project_unavailable"}
            except sqlite3.Error:
                return {"status": "project_unavailable"}
            with db:
                db.execute("UPDATE phone_forks SET finished=1 WHERE request_id=?", (request,))
            return fork_result(child, project, projects)

    def reply(self, db, data):
        if not isinstance(data, dict) or set(data) - {"model", "effort", "attachments"} not in (
                {"event_id", "request_id", "text"}, {"thread_id", "request_id", "text"}):
            raise ValueError("Invalid reply")
        request_id, text = data["request_id"], data["text"]
        thread_id = data.get("thread_id")
        if "thread_id" in data and not valid_thread_id(thread_id):
            raise ValueError("Invalid thread ID")
        target_key = "thread:" + thread_id if thread_id is not None else data["event_id"]
        if not valid_request_id(request_id):
            raise ValueError("Invalid request ID")
        attachments = parse_attachments(data.get("attachments", []))
        if not isinstance(text, str) or len(text) > 12000 or (not text.strip() and not attachments):
            raise ValueError("Invalid reply text")
        model = data.get("model")
        if model is not None and (not isinstance(model, str) or not model or len(model) > 200
                                  or any(ord(char) < 32 for char in model)):
            raise ValueError("Invalid model")
        effort = data.get("effort")
        if effort is not None and (not isinstance(effort, str) or re.fullmatch(r"[a-z]{1,24}", effort) is None):
            raise ValueError("Invalid effort")
        overrides = {} if model is None else {"model": model}
        payload = text if model is None else model + "\0" + text
        if effort is not None:
            overrides["effort"] = effort
            payload = "\0" + json.dumps([model, effort, text], ensure_ascii=False)
        if attachments:
            payload = "\0" + json.dumps([model, effort, text, [item.identity() for item in attachments]], ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.submit_lock:
            previous = db.execute("""
                SELECT event_key, fingerprint, status, turn_id FROM phone_replies WHERE request_id=?
                """, (request_id,)).fetchone()
            if previous is not None:
                if previous["event_key"] != target_key or previous["fingerprint"] != fingerprint:
                    raise ValueError("Reply ID was reused")
                status = "unknown" if previous["status"] == "sending" else previous["status"]
                return {"status": status, "turn_id": previous["turn_id"]}
            if claude_id(thread_id) is not None:
                status, cwd, existing = self.claude.prepare(db, thread_id)
                if status != "ready":
                    return {"status": status}
                error = self.selection_error(db, thread_id, model, effort, cwd=cwd)
                if error:
                    return {"status": error}
                prompt, input_options = prepare_attachments(self.config, request_id, text, attachments, claude=True)
                turn_id = str(uuid.uuid4())
                with db:
                    db.execute("""
                        INSERT INTO phone_replies
                        (request_id, event_key, thread_id, fingerprint, created_at, status, turn_id)
                        VALUES (?, ?, ?, ?, ?, 'sending', ?)
                    """, (request_id, target_key, thread_id, fingerprint, time.time(), turn_id))
                draft = self.claude.draft(db, thread_id) if not existing else None
                status, turn_id = self.claude.start(thread_id, turn_id, prompt, cwd, existing,
                                                    draft["title"] if draft is not None else None, **overrides, **input_options)
                with db:
                    db.execute("UPDATE phone_replies SET status=?, turn_id=? WHERE request_id=?",
                               (status, turn_id, request_id))
                return {"status": status, "turn_id": turn_id}
            if thread_id is None:
                anchor = self.anchor(db, target_key)
                if anchor is None:
                    return {"status": "unavailable"}
                path, thread_id, turn = anchor
            else:
                path, turn = find_rollout(self.config, thread_id, all_sources=True), None
            try:
                if path is None:
                    row = self.app_server.threads().get(thread_id)
                    if row is None:
                        return {"status": "unavailable"}
                    if row["archived"]:
                        return {"status": "archived"}
                    turns = native_turns(self.app_server.turns(thread_id))
                else:
                    if not path.is_relative_to(self.config["sessions_dir"]):
                        return {"status": "archived"}
                    turns = (self.native_history(thread_id, path) if turn is None and fork_parent(path)
                             else read_conversation(path, turn))
            except (OSError, ValueError, AppServerError):
                return {"status": "unavailable"}
            unobserved = db.execute("""
                SELECT turn_id FROM phone_replies
                WHERE thread_id=? AND status IN ('accepted', 'unknown', 'sending') AND created_at>=?
                ORDER BY created_at DESC LIMIT 1
                """, (thread_id, time.time() - 60)).fetchone()
            if (turns and turns[-1].status == "running") or (
                    unobserved is not None and not any(t.id == unobserved["turn_id"] for t in turns)):
                return {"status": "busy"}
            error = self.selection_error(db, thread_id, model, effort)
            if error:
                return {"status": error}

            prompt, input_options = prepare_attachments(self.config, request_id, text, attachments)
            with db:
                db.execute("""
                    INSERT INTO phone_replies
                    (request_id, event_key, thread_id, fingerprint, created_at, status)
                    VALUES (?, ?, ?, ?, ?, 'sending')
                    """, (request_id, target_key, thread_id, fingerprint, time.time()))
            status, new_turn = codex_ipc.start_turn(self.socket_path, thread_id, request_id, prompt, **overrides, **input_options)
            if status == "desktop_unavailable":
                mobile = db.execute("""
                    SELECT 1 FROM phone_threads WHERE thread_id=?
                    UNION ALL SELECT 1 FROM phone_forks WHERE child_id=?
                    """, (thread_id, thread_id)).fetchone()
                status, new_turn = self.app_server.send(thread_id, request_id, prompt,
                                                        created_on_phone=mobile is not None, **overrides, **input_options)
                if status == "accepted" and mobile is not None and not turns:
                    title = (" ".join(text.split()) or attachments[0].name)[:80]
                    try:
                        self.app_server.request("thread/name/set", {"threadId": thread_id, "name": title})
                    except (AppServerError, OSError):
                        pass
            with db:
                db.execute("UPDATE phone_replies SET status=?, turn_id=? WHERE request_id=?",
                           (status, new_turn, request_id))
            return {"status": status, "turn_id": new_turn}


def valid_request_id(value):
    return isinstance(value, str) and str(uuid.UUID(value, version=4)) == value


def fork_result(child, project, projects):
    return {"status": "created", "thread_id": child, "project_id": project,
            "project": projects.names.get(project, "未归类")}
