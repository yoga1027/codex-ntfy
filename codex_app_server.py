"""Use the computer's local Codex App Server for project history and turns."""
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import threading
import time

from reply_attachments import codex_input


SOURCE_KINDS = ["vscode", "cli", "appServer", "exec", "unknown"]


class AppServerError(Exception):
    pass


class CodexAppServer:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.request_id = 0

    def close(self):
        with self.lock:
            if self.process is not None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.process.stdin.close()
                self.process.stdout.close()
                self.process = None

    def request(self, method, params):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.start()
            return self.call(method, params)

    def start(self):
        if self.process is not None:
            self.process.stdin.close()
            self.process.stdout.close()
        codex_home = self.config["sessions_dir"].parent
        executable = self.config.get("codex_executable") or shutil.which("codex")
        if not executable:
            executable = str(Path.home() / ".local" / "bin" / "codex")
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"],
            cwd=Path.home(), env={**os.environ, "CODEX_HOME": str(codex_home)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
        )
        self.process = process
        self.responses = Queue()
        threading.Thread(target=self.read, args=(process, self.responses), daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "coding_love", "version": "0.10"},
                "capabilities": {"experimentalApi": True},
            })
            self.write(process, {"method": "initialized"})
        except (OSError, AppServerError):
            process.terminate()
            raise

    def write(self, process, message):
        with self.write_lock:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

    def read(self, process, responses):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict) or "id" not in message:
                    continue
                if "method" in message:
                    # The phone composer does not handle interactive permission prompts.
                    self.write(process, {"id": message["id"], "error": {
                        "code": -32601, "message": "This client does not handle interactive requests",
                    }})
                else:
                    responses.put(message)
        except (OSError, ValueError):
            pass
        finally:
            responses.put(None)

    def call(self, method, params):
        self.request_id += 1
        ident = self.request_id
        self.write(self.process, {"id": ident, "method": method, "params": params})
        # A foreground quota read also waits for the separate reset-card detail lookup.
        until = time.monotonic() + (20 if method == "account/rateLimits/read" else 8)
        while True:
            try:
                response = self.responses.get(timeout=max(0, until - time.monotonic()))
            except Empty:
                raise TimeoutError("Codex App Server timed out") from None
            if response is None:
                raise OSError("Codex App Server disconnected")
            if response.get("id") == ident:
                if isinstance(response.get("result"), dict):
                    return response["result"]
                raise AppServerError(method)

    def threads(self):
        threads = {}
        for archived in (True, False):
            cursor = None
            seen = set()
            while True:
                page = self.request("thread/list", {
                    "cursor": cursor, "limit": 100, "sortKey": "created_at", "archived": archived,
                    "sourceKinds": SOURCE_KINDS, "modelProviders": [], "useStateDbOnly": True,
                })
                if not isinstance(page.get("data"), list):
                    raise AppServerError("Invalid thread list")
                for row in page["data"]:
                    if isinstance(row, dict) and isinstance(row.get("id"), str) and not row.get("ephemeral"):
                        threads[row["id"]] = {**row, "archived": archived}
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise AppServerError("Invalid thread cursor")
                seen.add(cursor)
        return threads

    def read_thread(self, thread):
        result = self.request("thread/read", {"threadId": thread, "includeTurns": False})
        row = result.get("thread")
        if (not isinstance(row, dict) or row.get("id") != thread or row.get("ephemeral") is True
                or row.get("source") not in SOURCE_KINDS):
            raise AppServerError("Invalid thread")
        return row

    def models(self):
        models = {}
        cursor = None
        seen = set()
        while True:
            page = self.request("model/list", {"limit": 100, "includeHidden": False, "cursor": cursor})
            if not isinstance(page.get("data"), list):
                raise AppServerError("Invalid model list")
            for row in page["data"]:
                if not isinstance(row, dict) or row.get("hidden"):
                    continue
                value = row.get("model")
                label = row.get("displayName")
                if isinstance(value, str) and value:
                    levels = [choice["reasoningEffort"] for choice in row.get("supportedReasoningEfforts", [])
                              if isinstance(choice, dict) and isinstance(choice.get("reasoningEffort"), str)]
                    models[value] = {"id": value, "label": label if isinstance(label, str) and label else value,
                                     "efforts": levels, "default": row.get("isDefault") is True}
            cursor = page.get("nextCursor")
            if cursor is None:
                return list(models.values())
            if not isinstance(cursor, str) or cursor in seen:
                raise AppServerError("Invalid model list cursor")
            seen.add(cursor)

    def turns(self, thread):
        cursor = None
        seen = set()
        while True:
            page = self.request("thread/turns/list", {
                "threadId": thread, "cursor": cursor, "limit": 32,
                "sortDirection": "asc", "itemsView": "full",
            })
            if not isinstance(page.get("data"), list):
                raise AppServerError("Invalid turn page")
            yield from page["data"]
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise AppServerError("Invalid turn cursor")
            seen.add(cursor)

    def interrupt(self, thread, turn):
        try:
            self.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            return "requested"
        except AppServerError:
            return "unavailable"
        except OSError:
            return "unknown"

    def send(self, thread, request_id, text, *, created_on_phone, model=None, effort=None, images=()):
        try:
            self.request("thread/unsubscribe", {"threadId": thread})
            params = {"threadId": thread, "excludeTurns": True}
            if created_on_phone:
                params["sandbox"] = "workspace-write"
            result = self.request("thread/resume", params)
            if (result.get("thread") or {}).get("id") != thread:
                return "unavailable", None
        except (OSError, AppServerError):
            return "desktop_unavailable", None
        try:
            params = {
                "threadId": thread, "clientUserMessageId": request_id,
                "input": codex_input(text, images),
            }
            if model is not None:
                params["model"] = model
            if effort is not None:
                params["effort"] = effort
            result = self.request("turn/start", params)
        except AppServerError:
            return "unavailable", None
        except OSError:
            return "unknown", None
        turn = (result.get("turn") or {}).get("id")
        return ("accepted", turn) if isinstance(turn, str) and turn else ("unknown", None)
