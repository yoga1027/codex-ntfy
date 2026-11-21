"""The desktop owner receives exactly one text turn on the original thread."""
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
import uuid

import codex_ipc


class CodexIpcChecks(unittest.TestCase):
    def test_routes_to_the_discovered_owner_without_overrides(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "ipc.sock"
            listener.bind(str(path))
            listener.listen()
            listener.settimeout(4)
            received = []

            def desktop():
                with listener.accept()[0] as conn, conn.makefile("rb") as stream:
                    conn.settimeout(4)
                    responses = [
                        {"result": {"clientId": "mobile-controller"}},
                        {"handledByClientId": "original-vscode", "result": {"supportsUntrustedAppInput": True}},
                        {"result": {"result": {"turn": {"id": "turn-from-desktop"}}}},
                    ]
                    for response in responses:
                        length, = struct.unpack("<I", stream.read(4))
                        request = json.loads(stream.read(length))
                        received.append(request)
                        response.update(type="response", resultType="success", requestId=request["requestId"])
                        body = json.dumps(response).encode()
                        conn.sendall(struct.pack("<I", len(body)) + body)

            server = threading.Thread(target=desktop)
            server.start()
            request_id = str(uuid.uuid4())
            try:
                result = codex_ipc.start_turn(path, "thread-from-reminder", request_id, "继续当前对话")
            finally:
                server.join(5)
            self.assertEqual(result, ("accepted", "turn-from-desktop"))
            self.assertEqual([(r["method"], r["version"]) for r in received], [
                ("initialize", 0), ("thread-owner-discovery", 1), ("thread-follower-start-turn", 2),
            ])
            self.assertEqual(received[1]["params"], {"hostId": "local", "conversationId": "thread-from-reminder"})
            self.assertEqual(received[2]["targetClientId"], "original-vscode")
            self.assertEqual(received[2]["params"], {
                "conversationId": "thread-from-reminder",
                "turnStart": {
                    "request": {"threadId": "thread-from-reminder", "clientUserMessageId": request_id,
                                "input": [{"type": "text", "text": "继续当前对话", "text_elements": []}]},
                    "context": {"inheritThreadSettings": True},
                },
            })
