#!/usr/bin/python3
"""Global completion command, Codex notify adapter, and local stdio MCP tool."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

# Also work when launched through ~/.local/bin/ai-notify (a symlink).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_ntfy import DEFAULT_CONFIG, TurnState, load_config
from notification_queue import APPS, enqueue, valid_id


def codex_application(config, thread_id):
    """Verify root-session metadata; never notify for a subagent."""
    valid_id(thread_id)
    for path in config["sessions_dir"].rglob("rollout-*.jsonl"):
        if thread_id not in path.name:
            continue
        try:
            with path.open("rb") as stream:
                record = json.loads(stream.readline(1024 * 1024))
            meta = record.get("payload", {})
            if record.get("type") != "session_meta" or meta.get("id") != thread_id:
                continue
            source = meta.get("source")
            if not isinstance(source, str) or source not in config.get("sources", ["vscode"]):
                return None
            if source == "cli":
                return "codex"
            if meta.get("originator") in {"codex_work_desktop", "Codex Desktop"}:
                return "chatgpt"
            return "vscode"
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def codex_hook(config, data):
    if not isinstance(data, dict) or data.get("type") != "agent-turn-complete":
        return
    thread_id, turn_id = data.get("thread-id"), data.get("turn-id")
    # Without identifiers, the rollout watcher remains the completion source.
    if not thread_id or not turn_id:
        print("ai-notify: missing event identifiers; using rollout watcher", file=sys.stderr)
        return
    app = codex_application(config, thread_id)
    if app is not None:
        completion = confirmed_completion(config, thread_id, turn_id)
        if completion is not None:
            enqueue(config, app, turn_id, thread_id=thread_id, completed_at=completion.completed_at,
                    windows=completion.windows, quota_interrupted=completion.quota_interrupted,
                    reset_times=completion.reset_times)


def confirmed_completion(config, thread_id, turn_id):
    """Verify the same terminal evidence as the rollout watcher."""
    for path in config['sessions_dir'].rglob('rollout-*.jsonl'):
        if thread_id not in path.name:
            continue
        turn = TurnState()
        try:
            with path.open('rb') as stream:
                first = json.loads(stream.readline())
                if first.get('type') != 'session_meta' or first.get('payload', {}).get('id') != thread_id:
                    continue
                turn.observe(first)
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    turn.observe(record)
                    if turn.session_id != thread_id:
                        continue
                    completion = turn.completion(
                        record, allow_quota_interruption=config.get("transport") == "tailscale")
                    if completion is not None and completion.turn_id == turn_id:
                        return completion
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return None


EXTERNAL_APPS = [app for app in APPS if app not in {"vscode", "codex"}]
TOOL = {
    "name": "notify_task_complete",
    "description": "Use once only after the final reply has been fully emitted and the root conversation turn has ended. Never call before the final reply or for intermediate steps, subagents, or Codex sessions already watched automatically. Queue a fixed reminder, valid for 5 minutes. Reuse event_id for retries. Queued does not mean received by the phone.",
    "inputSchema": {
        "type": "object", "properties": {
            "app": {"type": "string", "enum": EXTERNAL_APPS},
            "event_id": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "Unique identifier for this conversation turn; keep it unchanged on retry."},
        }, "required": ["app", "event_id"], "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
}


def mcp_result(config, method, params):
    if method == "initialize":
        supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
        version = params.get("protocolVersion")
        return {"protocolVersion": version if version in supported else "2025-11-25",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ai-notify", "version": "1.0.0"},
                "instructions": TOOL["description"]}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            raise LookupError("Unknown tool")
        args = params.get("arguments", {})
        try:
            if not isinstance(args, dict) or set(args) != {"app", "event_id"}:
                raise ValueError("Expected only app and event_id")
            if args["app"] not in EXTERNAL_APPS:
                raise ValueError("Unsupported external application")
            result = enqueue(config, args["app"], args["event_id"])
            return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}
        except (ValueError, TypeError, OSError, sqlite3.Error) as error:
            return {"content": [{"type": "text", "text": "Could not queue notification: " + type(error).__name__}], "isError": True}
    raise LookupError("Unknown method")


def serve_mcp(config):
    """Newline-delimited JSON-RPC only on stdout; logs must go to stderr."""
    for line in sys.stdin:
        request = None
        try:
            if len(line) > 65536:
                raise ValueError("Request too large")
            request = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON request"}}
        else:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
            elif "id" not in request:
                continue
            else:
                response = {"jsonrpc": "2.0", "id": request["id"]}
                try:
                    params = request.get("params", {})
                    if not isinstance(params, dict):
                        raise ValueError("Invalid parameters")
                    response["result"] = mcp_result(config, request["method"], params)
                except LookupError:
                    response["error"] = {"code": -32601, "message": "Method or tool not found"}
                except (ValueError, TypeError):
                    response["error"] = {"code": -32602, "message": "Invalid parameters"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    complete = sub.add_parser("complete", help="Queue a fixed reminder for an external client")
    complete.add_argument("--app", choices=APPS, default="generic")
    complete.add_argument("--event-id", default=None)
    complete.add_argument("--thread-id", default=None, help="Actual Codex thread ID, for shared deduplication")
    hook = sub.add_parser("codex-hook", help="Receive Codex notify's final JSON argument")
    hook.add_argument("payload")
    sub.add_parser("test", help="Queue a connection test through the background service")
    sub.add_parser("mcp", help="Run the local stdio MCP completion tool")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.command == "codex-hook":
            codex_hook(config, json.loads(args.payload))
        elif args.command == "mcp":
            serve_mcp(config)
        elif args.command == "test":
            print(json.dumps(enqueue(config, "generic", str(uuid.uuid4()), test=True)))
        else:
            app = args.app
            completion = None
            if args.thread_id:
                app = codex_application(config, args.thread_id)
                if app is None:
                    raise ValueError("Not a watched root session")
                if not args.event_id:
                    raise ValueError('A real completed turn ID is required')
                completion = confirmed_completion(config, args.thread_id, args.event_id)
                if completion is None:
                    raise ValueError('This turn has no verified terminal record')
            result = enqueue(config, app, args.event_id or str(uuid.uuid4()), args.thread_id,
                             completed_at=completion.completed_at if completion else None,
                             windows=completion.windows if completion else None,
                             quota_interrupted=completion.quota_interrupted if completion else False,
                             reset_times=completion.reset_times if completion else None)
            print(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        # Hook failure must not alter the conversation; watcher remains active.
        print("ai-notify: " + type(error).__name__, file=sys.stderr)
        return 0 if args.command == "codex-hook" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
