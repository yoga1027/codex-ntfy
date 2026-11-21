"""Send text to the VS Code Codex window that already owns the local thread."""
import json
import os
import socket
import stat
import struct
import time
import uuid

from reply_attachments import codex_input


class IpcConnection:
    def __init__(self, sock):
        self.sock = sock
        self.client_id = None

    def read(self, length, deadline):
        data = bytearray()
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex IPC timed out")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise EOFError("Codex IPC disconnected")
            data.extend(chunk)
        return data

    def request(self, method, version, params, target=None, timeout=6):
        request_id = str(uuid.uuid4())
        request = {
            "type": "request", "requestId": request_id, "version": version,
            "method": method, "params": params, "timeoutMs": int(timeout * 1000),
        }
        if self.client_id is not None:
            request["sourceClientId"] = self.client_id
        if target is not None:
            request["targetClientId"] = target
        data = json.dumps(request).encode("utf-8")
        self.sock.settimeout(timeout)
        self.sock.sendall(struct.pack("<I", len(data)) + data)
        deadline = time.monotonic() + timeout + 1
        while True:
            length, = struct.unpack("<I", self.read(4, deadline))
            if not 0 < length <= 16 * 1024 * 1024:
                raise ValueError("Invalid Codex IPC frame size")
            reply = json.loads(self.read(length, deadline))
            if isinstance(reply, dict) and reply.get("type") == "response" and reply.get("requestId") == request_id:
                return reply

    def find_owner(self, thread_id):
        initial = self.request("initialize", 0, {"clientType": "codex-ntfy"})
        result = initial.get("result") or {}
        self.client_id = result.get("clientId")
        if not self.client_id:
            return None
        response = self.request("thread-owner-discovery", 1, {"hostId": "local", "conversationId": thread_id})
        if response.get("resultType") == "success":
            return response.get("handledByClientId")
        return None


def owner_request(socket_path, thread_id, method, version, params):
    """Send a request to the VS Code window that owns the thread."""
    dispatched = False
    try:
        info = socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return "desktop_unavailable", None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(4)
            sock.connect(str(socket_path))
            _, owner_uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if owner_uid != os.getuid():
                return "desktop_unavailable", None
            client = IpcConnection(sock)
            owner = client.find_owner(thread_id)
            if not owner:
                return "desktop_unavailable", None
            dispatched = True
            response = client.request(method, version, {"conversationId": thread_id, **params},
                                      target=owner, timeout=18)
            if response.get("error") == "no-client-found":
                return "desktop_unavailable", None
            if response.get("resultType") == "success":
                return "accepted", (response.get("result") or {}).get("result") or {}
    except (OSError, EOFError, ValueError, AttributeError):
        if not dispatched:
            return "desktop_unavailable", None
    return "unknown", None


def start_turn(socket_path, thread_id, request_id, text, model=None, effort=None, images=()):
    request = {"threadId": thread_id, "clientUserMessageId": request_id,
               "input": codex_input(text, images)}
    if model is not None:
        request["model"] = model
    if effort is not None:
        request["effort"] = effort
    status, result = owner_request(socket_path, thread_id, "thread-follower-start-turn", 2, {
        "turnStart": {"request": request, "context": {"inheritThreadSettings": True}},
    })
    if status != "accepted":
        return status, None
    turn_id = (result.get("turn") or {}).get("id")
    return ("accepted", turn_id) if isinstance(turn_id, str) and turn_id else ("unknown", None)


def interrupt_turn(socket_path, thread_id, turn_id):
    status, result = owner_request(socket_path, thread_id, "thread-follower-interrupt-turn", 4, {
        "mode": "user-stop", "expectedTurnId": turn_id,
    })
    if status != "accepted":
        return status
    return "requested" if result.get("interruptedTurnId") == turn_id else "not_running"
