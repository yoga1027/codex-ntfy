"""Serve queued Codex reminders to one Tailscale peer."""
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from account_usage import account_usage
from notification_queue import deadline
from reply_attachments import MAX_REPLY_BODY
from phone_conversations import PhoneConversations
from codex_app_server import AppServerError
from codex_conversation import valid_thread_id

LOG = logging.getLogger("codex-ntfy")
TAILSCALE_RANGE = ipaddress.IPv4Network("100.64.0.0/10")
OFFERED = "tailscale:offered"
POSTED = "tailscale:posted"


def validate_config(settings):
    if not isinstance(settings, dict):
        raise ValueError("Tailscale transport requires a tailscale settings object")
    try:
        listen = ipaddress.IPv4Address(settings.get("listen_address"))
        peer = ipaddress.IPv4Address(settings.get("peer_address"))
    except ipaddress.AddressValueError:
        raise ValueError("Set tailscale.listen_address and tailscale.peer_address to your Tailscale IPv4 addresses") from None
    if listen == peer or listen not in TAILSCALE_RANGE or peer not in TAILSCALE_RANGE:
        raise ValueError("Use distinct Tailscale IPv4 addresses for the computer and phone")
    port = settings.get("port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid Tailscale receiver port")


class ReceiverServer(ThreadingHTTPServer):
    def __init__(self, config, *, interface="tailscale0", wait_seconds=20):
        self.config = config
        settings = config["tailscale"]
        self.peer = settings["peer_address"]
        self.wait_seconds = wait_seconds
        self.stopping = threading.Event()
        self.phone_polling = threading.Event()
        self.conversations = PhoneConversations(config)
        super().__init__((settings["listen_address"], settings["port"]), ReceiverRequest,
                         bind_and_activate=False)
        try:
            if interface is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                       interface.encode() + b"\0")
            self.server_bind()
            self.server_activate()
        except OSError:
            self.server_close()
            raise

    def verify_request(self, request, address):
        return address[0] == self.peer

    def server_close(self):
        self.conversations.claude.close()
        self.conversations.app_server.close()
        super().server_close()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def handle_error(self, request, address):
        LOG.warning("Tailscale receiver request was interrupted")

    def database(self):
        db = sqlite3.connect(self.config["state_dir"] / "state.sqlite3", timeout=3)
        db.row_factory = sqlite3.Row
        return closing(db)

    def next_reminder(self, db):
        now = time.time()
        cutoff = now - self.config["max_notification_age_seconds"]
        row = db.execute("""
            SELECT event_key, completed_at, payload, message_id FROM notifications
            WHERE sent_at IS NULL AND expired_at IS NULL
              AND completed_at>=? AND next_attempt<=?
              AND (message_id IS NULL OR message_id=?)
            ORDER BY completed_at, event_key LIMIT 1
        """, (cutoff, now, OFFERED)).fetchone()
        if row is None:
            return None
        if row["message_id"] is None:
            with db:
                changed = db.execute("""
                    UPDATE notifications SET message_id=?
                    WHERE event_key=? AND message_id IS NULL AND sent_at IS NULL AND expired_at IS NULL
                """, (OFFERED, row["event_key"])).rowcount
            if not changed:
                return None
        payload = json.loads(row["payload"])
        reminder = {
            "event_id": row["event_key"],
            "expires_at_ms": int(deadline(self.config, row["completed_at"]) * 1000),
            "title": payload["title"],
            "message": payload["message"],
        }
        if "summary" in payload:
            reminder["summary"] = payload["summary"]
        return reminder

    def acknowledge(self, event_id):
        now = time.time()
        cutoff = now - self.config["max_notification_age_seconds"]
        with self.database() as db, db:
            changed = db.execute("""
                UPDATE notifications SET sent_at=?, message_id=?
                WHERE event_key=? AND message_id=? AND sent_at IS NULL
                  AND expired_at IS NULL AND completed_at>=?
            """, (now, POSTED, event_id, OFFERED, cutoff)).rowcount
        if changed:
            LOG.info("Tailscale phone confirmed local notification posting")


class ReceiverRequest(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def reply_json(self, data):
        self.reply(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def phone_origin(self):
        address, port = self.server.server_address
        return self.headers.get("Host") == f"{address}:{port}" and self.headers.get("Origin") is None

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, b"Tailscale link OK\n")
            return
        if urlsplit(self.path).path == "/v1/thread/file":
            if not self.phone_origin():
                self.reply(403)
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                result = self.server.conversations.file_preview(query.get("thread_id", [""])[0],
                                                                query.get("path", [""])[0],
                                                                int(query.get("line", ["1"])[0]))
            except ValueError:
                self.reply(400)
                return
            self.reply_json(result)
            return
        if self.path == "/v1/account/usage":
            if not self.phone_origin():
                self.reply(403)
                return
            self.reply_json(account_usage(self.server.conversations))
            return
        if urlsplit(self.path).path == "/v1/models":
            if not self.phone_origin():
                self.reply(403)
                return
            thread = parse_qs(urlsplit(self.path).query).get("thread_id", [""])[0]
            if not valid_thread_id(thread):
                self.reply(400)
                return
            try:
                with self.server.database() as db:
                    self.reply_json(self.server.conversations.model_menu(db, thread))
            except (OSError, AppServerError):
                self.reply(503)
            return
        if urlsplit(self.path).path == "/v1/projects":
            if not self.phone_origin():
                self.reply(403)
                return
            revision = parse_qs(urlsplit(self.path).query).get("revision", [""])[0]
            try:
                result = self.server.conversations.projects(revision)
            except (AppServerError, OSError):
                self.reply(503)
                return
            self.reply_json(result)
            return
        if self.path.startswith("/v1/conversation?"):
            if not self.phone_origin():
                self.reply(403)
                return
            query = parse_qs(urlsplit(self.path).query)
            event_id = query.get("event_id", [""])[0]
            revision = query.get("revision", [""])[0]
            with self.server.database() as db:
                snapshot = self.server.conversations.snapshot(db, event_id, revision)
            if snapshot is None:
                self.reply(404)
            else:
                self.reply_json(snapshot)
            return
        if self.path == "/v1/threads" or self.path.startswith("/v1/thread?"):
            if not self.phone_origin():
                self.reply(403)
                return
            try:
                if self.path == "/v1/threads":
                    with self.server.database() as db:
                        result = self.server.conversations.catalog(db)
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    thread_id = query.get("thread_id", [""])[0]
                    revision = query.get("revision", [""])[0]
                    offset = int(query["offset"][0]) if "offset" in query else None
                    if offset is not None and offset < 1:
                        raise ValueError("Invalid history offset")
                    result = self.server.conversations.thread_page(thread_id, revision, offset)
            except ValueError:
                self.reply(400)
                return
            except OSError:
                self.reply(404)
                return
            if result is None:
                self.reply(404)
            else:
                self.reply_json(result)
            return
        if self.path != "/v1/reminders":
            self.reply(404)
            return
        if not self.server.phone_polling.is_set():
            self.server.phone_polling.set()
            LOG.info("Tailscale phone connected to the reminder queue")
        end = time.monotonic() + self.server.wait_seconds
        with self.server.database() as db:
            while not self.server.stopping.is_set():
                reminder = self.server.next_reminder(db)
                if reminder is not None:
                    body = json.dumps(reminder, ensure_ascii=False).encode("utf-8")
                    self.reply(200, body, "application/json; charset=utf-8")
                    return
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self.server.stopping.wait(min(1, remaining))
        self.reply(204)

    def do_POST(self):
        if self.path in ("/v1/reply", "/v1/thread/new", "/v1/thread/unarchive",
                         "/v1/thread/archive", "/v1/thread/move", "/v1/thread/fork", "/v1/thread/rename",
                         "/v1/thread/interrupt", "/v1/project/rename", "/v1/thread/file/change"):
            if not self.phone_origin() or self.headers.get_content_type() != "application/json":
                self.reply(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = MAX_REPLY_BODY if self.path == "/v1/reply" else 65536
                if not 0 < length <= limit:
                    raise ValueError("Invalid length")
                data = json.loads(self.rfile.read(length))
                with self.server.database() as db:
                    if self.path == "/v1/thread/new":
                        result = self.server.conversations.create(db, data)
                    elif self.path in ("/v1/thread/unarchive", "/v1/thread/archive"):
                        result = self.server.conversations.set_archive(data, archived=self.path.endswith("/archive"))
                    elif self.path == "/v1/thread/move":
                        result = self.server.conversations.move(data)
                    elif self.path == "/v1/thread/fork":
                        result = self.server.conversations.fork(db, data)
                    elif self.path == "/v1/thread/rename":
                        result = self.server.conversations.rename(data)
                    elif self.path == "/v1/project/rename":
                        result = self.server.conversations.rename_project(data)
                    elif self.path == "/v1/thread/file/change":
                        result = self.server.conversations.change_file(db, data)
                    elif self.path == "/v1/thread/interrupt":
                        result = self.server.conversations.interrupt(data)
                    else:
                        result = self.server.conversations.reply(db, data)
            except (ValueError, OSError):
                self.reply(400)
                return
            self.reply_json(result)
            return
        if self.path != "/v1/ack":
            self.reply(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid length")
            body = json.loads(self.rfile.read(length))
            event_id = body.get("event_id") if isinstance(body, dict) else None
            if not isinstance(event_id, str) or not 0 < len(event_id) <= 500:
                raise ValueError("Invalid event ID")
        except (ValueError, OSError):
            self.reply(400)
            return
        self.server.acknowledge(event_id)
        self.reply(204)


@contextmanager
def run_receiver(config):
    with ReceiverServer(config) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        LOG.info("Tailscale reminder receiver listening on %s:%s", *server.server_address)
        try:
            yield
        finally:
            server.stopping.set()
            server.shutdown()
            worker.join()
