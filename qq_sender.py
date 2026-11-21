"""Guarded Linux X11 QQ device-chat sender. Never targets an ordinary friend."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time


class QQUnavailable(OSError):
    pass


class QQUncertain(ValueError):
    """Send was attempted; manual inspection is required before retrying."""


def check_deadline(expires_at):
    if expires_at is not None and time.time() > expires_at:
        raise QQUnavailable('Reminder expired; no send performed')


def check_editor_deadline(composer, api, message, expires_at):
    # Clean up only the exact reminder inserted by this call, never a user draft.
    if expires_at is not None and time.time() > expires_at:
        if composer.getState().contains(api.STATE_FOCUSED) and text(composer) == message:
            run('xdotool', 'key', '--clearmodifiers', 'ctrl+a', 'BackSpace')
        check_deadline(expires_at)


def run(*args, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise QQUnavailable("GUI command failed: " + args[0])
    return result.stdout.strip()


def copyq(script):
    return run("copyq", "eval", "-", input=script)


def walk(node, depth=0):
    if depth > 28:
        return
    yield node
    for child in node:
        if child:
            yield from walk(child, depth + 1)


def choose(nodes, predicate):
    matches = [n for n in nodes if predicate(n)]
    if len(matches) != 1:
        raise QQUnavailable("Required QQ control is missing or ambiguous")
    return matches[0]


def press(node):
    action = node.queryAction()
    for i in range(action.nActions):
        if action.getName(i) in ("press", "check", "clickAncestor"):
            if action.doAction(i):
                return
    raise QQUnavailable("QQ control cannot be activated")


def text(node):
    value = node.queryText().getText(0, -1).strip()
    if '\ufffc' in value:
        return '\n'.join(n.name for n in walk(node) if n.getRoleName() == 'static').strip()
    return value


def device_chat(app, api):
    if not any(n.getRoleName() == "push button" and n.name.startswith("我的设备 ") for n in walk(app)):
        contacts = choose(walk(app), lambda n: n.getRoleName() == "toggle button" and n.name == "联系人")
        press(contacts)
        time.sleep(.25)
    group = choose(walk(app), lambda n: n.getRoleName() == "push button" and n.name.startswith("我的设备 "))
    container = group.parent
    phones = [n for n in walk(container) if n.getRoleName() == "static" and n.name == "我的手机"]
    if not phones:
        press(group)
        time.sleep(.25)
        phones = [n for n in walk(container) if n.getRoleName() == "static" and n.name == "我的手机"]
    phone = choose(phones, lambda n: True)
    extent = phone.queryComponent().getExtents(api.DESKTOP_COORDS)
    active = run("xdotool", "getactivewindow")
    if run("xdotool", "getwindowpid", active) != str(app.get_process_id()):
        raise QQUnavailable("QQ lost focus during device selection")
    # QQ's device row requires a double click; derive coordinates from the
    # verified built-in device group, rather than a screen-size assumption.
    run("xdotool", "mousemove", str(extent.x + extent.width // 2), str(extent.y + extent.height // 2),
        "click", "--repeat", "2", "--delay", "100", "1")
    time.sleep(.35)
    return controls(app, api)


def controls(app, api):
    header = choose(walk(app), lambda n: n.getRoleName() == "push button" and n.name == "我的手机")
    frame = header
    while frame.getRoleName() != "frame":
        frame = frame.parent
    # Device chat has its own named header and a restricted file-transfer toolbar.
    nodes = list(walk(frame))
    composer = choose(nodes, lambda n: n.getRoleName() == "section" and n.getState().contains(api.STATE_EDITABLE))
    toolbar = choose(nodes, lambda n: n.getRoleName() == "tool bar" and n.name == "会话")
    # QQ may include the configured shortcut in an accessible button name.
    # Normalize only screenshot controls; the device toolbar still must match.
    button_names = set()
    for n in walk(toolbar):
        if n.getRoleName() != 'push button':
            continue
        name = n.name
        match = re.fullmatch(r'截图(?: (?:Ctrl|Alt|Shift|Meta|Super)(?: \+ (?:Ctrl|Alt|Shift|Meta|Super))* \+ [A-Z0-9])?(弹出菜单)?', name)
        if match:
            name = '截图' + (match.group(1) or '')
        button_names.add(name)
    if button_names != {"截图", "截图弹出菜单", "文件", "聊天记录"}:
        raise QQUnavailable("Device chat identity check failed")
    send = choose(nodes, lambda n: n.getRoleName() == "push button" and n.name == "发送")
    messages = choose(nodes, lambda n: n.getRoleName() == "embedded" and n.name == "消息列表")
    return composer, send, messages


def message_text(payload, event_key):
    marker = hashlib.sha256(event_key.encode()).hexdigest()[:16]
    return payload['title'] + "\n" + payload['message'] + "\n[AI-" + marker + "]"


def contains_message(messages, message):
    # Inspect only the already verified device chat; never log chat contents.
    parts = []
    for n in walk(messages):
        if n.getRoleName() == "static":
            parts.append(n.name)
    return message in "\n".join(parts) or all(line in parts for line in message.splitlines())


def publish(config, payload, event_key, verify_only=False, expires_at=None):
    check_deadline(expires_at)
    import pyatspi as api
    if os.environ.get("XDG_SESSION_TYPE", "x11") != "x11":
        raise QQUnavailable("QQ automation currently requires X11")
    locked = run("gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                 "--object-path", "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive")
    if "true" in locked:
        raise QQUnavailable("Desktop is locked; reminder remains queued")
    app = choose(api.Registry.getDesktop(0), lambda n: n and n.name.lower() == "qq")
    pid = app.get_process_id()
    if Path(f"/proc/{pid}/exe").resolve() != Path("/opt/QQ/qq"):
        raise QQUnavailable("Unexpected QQ executable")
    windows = [line.split(None, 5) for line in run("wmctrl", "-lp", "-x").splitlines()]
    window = choose(windows, lambda w: len(w) == 6 and w[2] == str(pid) and w[3].lower() == "qq.qq" and w[5] == "QQ")[0]
    original = run("xdotool", "getactivewindow")
    mouse = dict(line.split('=', 1) for line in run("xdotool", "getmouselocation", "--shell").splitlines())
    old_clipboard = None
    message = message_text(payload, event_key)
    attempted = False
    try:
        run("xdotool", "windowactivate", "--sync", window)
        composer, send, messages = device_chat(app, api)
        if contains_message(messages, message):
            return "qq:" + hashlib.sha256(event_key.encode()).hexdigest()[:16]
        if verify_only:
            raise QQUncertain("Previous QQ attempt requires manual inspection; no resend performed")
        if text(composer):
            raise QQUnavailable("Device chat contains a draft; preserved without sending")
        check_deadline(expires_at)
        old_clipboard = copyq("var o={}; str(clipboard('?')).split(String.fromCharCode(10)).forEach(function(m){if(m && ['TIMESTAMP','TARGETS','SAVE_TARGETS','MULTIPLE'].indexOf(m)<0)o[m]=toBase64(clipboard(m));}); JSON.stringify(o)")
        copyq("copy(" + json.dumps(message, ensure_ascii=True) + ")")
        if not composer.queryComponent().grabFocus():
            raise QQUnavailable("Cannot focus device message editor")
        time.sleep(.15)
        controls(app, api)
        if (int(run("xdotool", "getactivewindow")) != int(window, 16)
                or not composer.getState().contains(api.STATE_FOCUSED)):
            raise QQUnavailable("QQ lost input focus before paste")
        run("xdotool", "key", "--clearmodifiers", "ctrl+v")
        time.sleep(.25)
        composer, send, messages = controls(app, api)
        if text(composer) != message:
            raise QQUnavailable("QQ paste could not be verified; draft preserved")
        if int(run("xdotool", "getactivewindow")) != int(window, 16):
            raise QQUnavailable("QQ lost focus before send; draft preserved")
        # If expiry occurs while preparing the editor, remove only the exact
        # reminder we just inserted. Preserve all pre-existing/user drafts.
        check_editor_deadline(composer, api, message, expires_at)
        attempted = True
        press(send)
        for _ in range(20):
            time.sleep(.15)
            composer, _, messages = controls(app, api)
            if not text(composer) and contains_message(messages, message):
                return "qq:" + hashlib.sha256(event_key.encode()).hexdigest()[:16]
        raise QQUncertain("QQ send outcome is uncertain; inspect device chat before retry")
    except QQUncertain:
        raise
    except Exception as error:
        if attempted:
            raise QQUncertain("QQ send outcome is uncertain") from error
        if isinstance(error, QQUnavailable):
            raise
        raise QQUnavailable("QQ UI is unavailable or has changed") from error
    finally:
        if old_clipboard is not None:
            try:
                copyq("if(str(clipboard('text/plain'))===" + json.dumps(message) + "){var b=JSON.parse(" + json.dumps(old_clipboard) + ");var o={};for(var m in b)o[m]=fromBase64(b[m]);copy(o);}")
            except QQUnavailable:
                pass
        try:
            if int(run("xdotool", "getactivewindow")) == int(window, 16):
                run("xdotool", "mousemove", mouse['X'], mouse['Y'])
                run("xdotool", "windowactivate", "--sync", original)
        except QQUnavailable:
            pass
