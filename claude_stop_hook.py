#!/usr/bin/python3
"""Queue a reminder for a root Claude Code turn observed by its native Stop hook."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

from claude_sessions import ClaudeSessions, PREFIX, queue_completion, read_claude
from codex_ntfy import DEFAULT_CONFIG, load_config
from codex_conversation import history_revision


def stop(config, event):
    if not isinstance(event, dict) or event.get("agent_id") or event.get("stop_hook_active"):
        return
    kind = event.get("hook_event_name")
    quota = kind == "StopFailure" and event.get("error") == "rate_limit"
    if kind != "Stop" and not quota:
        return
    raw = event.get("session_id")
    transcript = event.get("transcript_path")
    if not isinstance(raw, str) or not isinstance(transcript, str):
        return
    claude = ClaudeSessions(config)
    thread = PREFIX + raw
    path = claude.path(thread)
    if path is None or path.resolve() != Path(transcript).expanduser().resolve():
        return
    turns = read_claude(path, history_revision(path.stat()))
    if not turns:
        return
    if quota:
        turn = turns[-1]
        ended = turn.completed_at or time.time()
        windows = claude.record_failure(thread, turn.id, "quota", ended=ended)
        queue_completion(config, claude, thread, turn, ended=ended, quota=True, windows=windows)
        return
    final = event.get("last_assistant_message")
    if not isinstance(final, str) or not final.strip():
        return
    for turn in reversed(turns[-3:]):
        replies = [m["text"] for m in turn.messages if m["kind"] == "assistant"]
        if replies and replies[-1].strip() == final.strip():
            queue_completion(config, claude, thread, turn)
            return


def install(config):
    path = Path(config["claude_home"]).expanduser() / "settings.json"
    previous = path.read_text(encoding="utf-8") if path.exists() else "{}"
    settings = json.loads(previous)
    hooks = settings.setdefault("hooks", {})
    executable = str(Path(__file__).resolve())
    command = "/usr/bin/python3 " + shlex.quote(executable)
    changed = False
    for hook, match in (("Stop", ""), ("StopFailure", "rate_limit")):
        entries = hooks.setdefault(hook, [])
        if any(h.get("command") == command for entry in entries for h in entry.get("hooks", [])):
            continue
        entries.append({"matcher": match, "hooks": [{"type": "command", "command": command, "async": True}]})
        changed = True
    if changed:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            pending = Path(output.name)
            json.dump(settings, output, ensure_ascii=False, indent=2)
            output.write("\n")
        try:
            if path.exists() and path.read_text(encoding="utf-8") != previous:
                raise OSError("Claude settings changed during installation")
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    print("Claude completion hooks installed" if changed else "Claude completion hooks already installed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--install"]:
        install(load_config(DEFAULT_CONFIG))
    elif not os.environ.get("CODING_LOVE_CLAUDE_TURN"):
        try:
            stop(load_config(DEFAULT_CONFIG), json.load(sys.stdin))
        except (OSError, ValueError, TypeError):
            pass
