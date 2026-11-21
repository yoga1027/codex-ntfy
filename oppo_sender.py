"""Send a notification through OPPO PUSH to one registered device."""
from getpass import getpass
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.push.oppomobile.com/server/v1"
CHANNEL_ID = "codex_opush_test"


class OppoError(ValueError):
    """An OPPO failure whose message can be logged without server content."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(path, fields, auth_token=None, *, timeout=15):
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    if auth_token is not None:
        headers["auth_token"] = auth_token
    request = Request(f"{API}/{path}", urlencode(fields).encode(), headers, method="POST")
    with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise OppoError("OPPO 响应缺少有效返回码")
    if result["code"] != 0:
        raise OppoError(f"OPPO 返回码 {result['code']}")
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def read_credentials(config):
    fields = ("app_key", "master_secret", "registration_id")
    if config is None:
        labels = ("AppKey", "MasterSecret（服务端）", "RegistrationID")
        values = {field: getpass(f"{label}: ").strip() for field, label in zip(fields, labels)}
    else:
        values = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or any(
        not isinstance(values.get(field), str) or not values[field].strip() for field in fields
    ):
        raise OppoError("需要 AppKey、MasterSecret 和 RegistrationID")
    key, secret, regid = (values[field].strip() for field in fields)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", regid):
        raise OppoError("RegistrationID 必须是单台设备的注册 ID")
    return key, secret, regid


def authenticate(key, secret, *, timeout=15):
    timestamp = str(time.time_ns() // 1_000_000)
    signature = sha256(f"{key}{timestamp}{secret}".encode()).hexdigest()
    token = post("auth", {"app_key": key, "timestamp": timestamp, "sign": signature},
                 timeout=timeout).get("auth_token")
    if not isinstance(token, str) or not token:
        raise OppoError("OPPO 未返回 auth_token")
    return token


def unicast(regid, notification, token, *, timeout=15):
    message = {"target_type": 2, "target_value": regid, "notification": notification}
    return post("message/notification/unicast",
                {"message": json.dumps(message, ensure_ascii=False)}, token, timeout=timeout)


def remaining_timeout(config, expires_at):
    remaining = expires_at - time.time()
    if remaining <= 0:
        raise TimeoutError("Completion reminder expired")
    return min(config["timeout_seconds"], remaining)


def standard_text(payload):
    label = payload["title"]
    summary, *details = payload["message"].splitlines()
    if details and details[-1].startswith("时间："):
        timestamp = details.pop().removeprefix("时间：")
        summary = summary.removeprefix(label + " ")
        if summary.startswith("本轮对话已完成"):
            label += " 已完成"
        label += f" · {timestamp}"
    # OPPO's standard notification style allows 50 characters in each field.
    content = " ".join(details) if details else summary
    return label[:50], content[:50]


def publish(config, payload, event_key, *, expires_at):
    key, secret, regid = read_credentials(config["oppo_credentials_file"])
    title, content = standard_text(payload)
    notification = {
        "app_message_id": sha256(event_key.encode()).hexdigest()[:32],
        "channel_id": CHANNEL_ID,
        "style": 1,
        "title": title,
        "content": content,
        "off_line": False,
    }
    token = authenticate(key, secret, timeout=remaining_timeout(config, expires_at))
    # Authentication can consume the remaining delivery window.
    result = unicast(regid, notification, token, timeout=remaining_timeout(config, expires_at))
    return "oppo:" + str(result.get("messageId") or notification["app_message_id"])
