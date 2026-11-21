"""Decode phone attachments and prepare provider-specific message inputs."""
import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 4
MAX_REPLY_BODY = 4 * ((MAX_BYTES + 2) // 3) + 80 * 1024
IMAGE_HEADERS = {
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/gif": (".gif", lambda data: data.startswith((b"GIF87a", b"GIF89a"))),
    "image/webp": (".webp", lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP"),
}


@dataclass(frozen=True)
class Attachment:
    name: str
    kind: str
    mime: str
    data: bytes

    def identity(self):
        return [self.name, self.kind, self.mime, hashlib.sha256(self.data).hexdigest()]


def parse_attachments(value):
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError("Invalid attachments")
    attachments = []
    remaining = MAX_BYTES
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "kind", "mime", "data"}:
            raise ValueError("Invalid attachment")
        name, kind, mime, encoded = (row[key] for key in ("name", "kind", "mime", "data"))
        if (not isinstance(name, str) or not 1 <= len(name) <= 180
                or name in (".", "..") or any(ord(c) < 32 or c in "/\\" for c in name)):
            raise ValueError("Invalid attachment name")
        if (kind not in ("image", "file") or not isinstance(mime, str)
                or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime) or len(mime) > 100
                or not isinstance(encoded, str) or len(encoded) > 4 * ((remaining + 2) // 3)):
            raise ValueError("Invalid attachment metadata")
        data = base64.b64decode(encoded, validate=True)
        remaining -= len(data)
        if not data or remaining < 0 or (kind == "image" and
                                       (mime not in IMAGE_HEADERS or not IMAGE_HEADERS[mime][1](data))):
            raise ValueError("Invalid attachment content")
        attachments.append(Attachment(name, kind, mime, data))
    return attachments


def prepare_attachments(config, request_id, text, attachments, *, claude=False):
    if not attachments:
        return text, {}
    directory = (config["state_dir"] / "attachments" / request_id).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    listing = []
    images = []
    for index, item in enumerate(attachments, 1):
        stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", Path(item.name).stem).strip(".")[:100] or "attachment"
        extension = (IMAGE_HEADERS[item.mime][0] if item.kind == "image"
                     else re.sub(r"[^a-zA-Z0-9_.]", "", Path(item.name).suffix)[:20])
        path = directory / f"{index}-{stem}{extension}"
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as out:
            pending = Path(out.name)
            try:
                out.write(item.data)
                out.flush()
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
        if item.kind == "image":
            if claude:
                images.append({"type": "image", "source": {"type": "base64", "media_type": item.mime,
                                                           "data": base64.b64encode(item.data).decode("ascii")}})
            else:
                images.append({"type": "localImage", "path": str(path)})
            listing.append(f"- 图片 {item.name}")
        else:
            listing.append(f"- 文件 {item.name}（电脑路径 `{path}`）")
    prompt = (text.strip() + "\n\n" if text.strip() else "") + "手机发送的附件\n" + "\n".join(listing)
    return prompt, {"images": images} if images else {}


def codex_input(text, images):
    return [{"type": "text", "text": text, "text_elements": []}, *images]
