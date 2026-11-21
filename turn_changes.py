"""File changes recorded by a turn. Only replay matched text from successful edits."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

MAX_TEXT = 8 * 1024 * 1024
HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Conflict(ValueError):
    pass


@dataclass
class Edit:
    kind: str
    content: str | None = None
    hunks: list | None = None
    moved: bool = False

    def totals(self):
        if self.kind in {"add", "delete"} and self.content is not None:
            size = len(self.content.splitlines())
            return (size, 0) if self.kind == "add" else (0, size)
        if self.hunks:
            lines = [line for _, _, block in self.hunks for line in block]
            return sum(line.startswith("+") for line in lines), sum(line.startswith("-") for line in lines)
        return None

    def apply(self, source, reverse):
        if self.moved:
            raise Conflict("此记录包含路径迁移，无法单独撤回")
        if self.kind in {"add", "delete"} and self.content is not None:
            create = (self.kind == "add") != reverse
            if source != (None if create else self.content):
                raise Conflict("文件内容或存在状态已改变")
            return self.content if create else None
        if not self.hunks or source is None:
            raise Conflict("缺少可重放的补丁或文件")
        lines = source.splitlines(keepends=True)
        blocks = []
        last = 0
        for old, new, diff in self.hunks:
            minus, plus = ("+", "-") if reverse else ("-", "+")
            expected = [s[1:] for s in diff if s[0] in (" ", minus)]
            replacement = [s[1:] for s in diff if s[0] in (" ", plus)]
            start = new if reverse else old
            if start < last or start > len(lines) or lines[start:start+len(expected)] != expected:
                raise Conflict("补丁与电脑上的文件内容冲突")
            # A context-free insertion cannot identify its original location after later edits.
            if not expected and lines:
                raise Conflict("补丁缺少定位上下文")
            blocks.extend(lines[last:start])
            blocks.extend(replacement)
            last = start + len(expected)
        return "".join(blocks + lines[last:])


def parse_hunks(diff):
    if not isinstance(diff, str):
        return None
    hunks = []
    block = None
    for line in diff.splitlines(keepends=True):
        header = HEADER.match(line)
        if header:
            old, n_old, new, n_new = header.groups()
            old_count, new_count = int(n_old or 1), int(n_new or 1)
            block = []
            hunks.append((int(old) - bool(old_count), int(new) - bool(new_count), block, old_count, new_count))
        elif block is not None:
            if line.startswith("\\ No newline at end of file") and block:
                block[-1] = block[-1].removesuffix("\n")
            elif line[:1] in (" ", "+", "-"):
                block.append(line)
            else:
                return None
    for _, _, block, old, new in hunks:
        if old != sum(s[0] != "+" for s in block) or new != sum(s[0] != "-" for s in block):
            return None
    return [h[:3] for h in hunks] or None


def codex_edit(data):
    if not isinstance(data, dict):
        return Edit("unknown")
    kind = data.get("kind") or data
    if not isinstance(kind, dict):
        return Edit("unknown")
    key = kind.get("type")
    content = data.get("content", data.get("diff")) if key in ("add", "delete") else None
    return Edit(key, content if isinstance(content, str) else None,
                parse_hunks(data.get("unified_diff", data.get("diff"))), bool(kind.get("move_path")))


def claude_edit(result):
    if not isinstance(result, dict):
        return Edit("unknown")
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return Edit("add", result["content"])
    hunks = []
    for h in result.get("structuredPatch") or []:
        if not isinstance(h, dict) or not all(type(h.get(k)) is int for k in ("oldStart","oldLines","newStart","newLines")):
            return Edit("unknown")
        lines = h.get("lines")
        if not isinstance(lines, list) or not all(isinstance(s, str) for s in lines):
            return Edit("unknown")
        header = "@@ -{oldStart},{oldLines} +{newStart},{newLines} @@\n".format(**h)
        parsed = parse_hunks(header + "".join(s + "\n" for s in lines))
        if not parsed:
            return Edit("unknown")
        hunks.extend(parsed)
    return Edit("update", hunks=hunks)


def summary(path, edits):
    counts = [edit.totals() for edit in edits]
    known = bool(counts) and all(count is not None for count in counts)
    first_hunk = next((edit.hunks[0] for edit in edits if edit.hunks), None)
    return {"path": path, "line": first_hunk[1] + 1 if first_hunk else 1,
            "added": sum(c[0] for c in counts) if known else None,
            "removed": sum(c[1] for c in counts) if known else None,
            "replayable": known and not any(e.moved for e in edits)}


def digest(text):
    return "-" if text is None else hashlib.sha256(text.encode()).hexdigest()


def read_text(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEXT:
            raise Conflict("仅支持 8 MiB 以内的文本文件")
        data = stream.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise Conflict("仅支持 8 MiB 以内的文本文件")
    if b"\0" in data:
        raise Conflict("二进制文件无法显示为文本")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise Conflict("文件不是 UTF-8 文本") from None


def replay(db, thread, turn, name, path, edits, action):
    original = read_text(path)
    current_hash = digest(original)
    key = (thread, turn, name)
    receipt = db.execute("SELECT before_hash, after_hash, mode FROM phone_edits "
                         "WHERE thread_id=? AND turn_id=? AND path=?", key).fetchone()
    if receipt:
        before, after, mode = receipt
        target_hash, source_hash = (before, after) if action == "undo" else (after, before)
        if current_hash == target_hash:
            return
        if current_hash != source_hash:
            raise Conflict("文件已有后续修改")
    elif action == "redo":
        raise Conflict("请先撤回这次修改")
    else:
        mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o644
    target = original
    for edit in reversed(edits) if action == "undo" else edits:
        target = edit.apply(target, action == "undo")
    if receipt and digest(target) != target_hash:
        raise Conflict("历史补丁已改变")
    if not receipt:
        # Persist the expected pair before changing the workspace so an interrupted request is retryable.
        with db:
            db.execute("INSERT INTO phone_edits VALUES (?, ?, ?, ?, ?, ?)",
                       (*key, digest(target), current_hash, mode))
    if read_text(path) != original:
        raise Conflict("文件在操作期间发生变化")
    if target is None:
        path.unlink()
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        tmp = Path(out.name)
        try:
            out.write(target.encode())
            out.flush()
            os.fchmod(out.fileno(), mode)
            if original is None:
                os.link(tmp, path)
            else:
                if read_text(path) != original:
                    raise Conflict("文件在操作期间发生变化")
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
