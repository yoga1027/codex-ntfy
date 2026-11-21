#!/usr/bin/env python3
"""Send one OPPO notification to a single registered device."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oppo_sender import CHANNEL_ID, authenticate, read_credentials, unicast


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="本机私有 JSON 凭据文件")
    parser.add_argument("--category", help="OPPO 已批准的消息类别，用于请求锁屏提醒")
    args = parser.parse_args()

    stage = "读取凭据"
    try:
        key, secret, regid = read_credentials(args.config)
        stage = "鉴权"
        auth_token = authenticate(key, secret)

        stage = "单设备推送"
        test_id = uuid4().hex
        now = datetime.now(timezone(timedelta(hours=8)))
        notification = {
            "app_message_id": test_id,
            "channel_id": CHANNEL_ID,
            "title": f"Codex 推送测试 {test_id[:8]}",
            "content": f"锁屏收件测试 {now:%H:%M:%S}（北京时间）",
            "off_line": False,
        }
        if args.category:
            notification.update(category=args.category, notify_level=2)
        print(f"测试编号 {test_id[:8]}，提交时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）", flush=True)
        result = unicast(regid, notification, auth_token)
    except HTTPError as error:
        parser.exit(1, f"{stage}失败，HTTP {error.code}。请先在 OPPO 控制台核对记录。\n")
    except (URLError, TimeoutError):
        parser.exit(1, f"{stage}时网络异常。请先在 OPPO 控制台核对记录。\n")
    except (json.JSONDecodeError, OSError, EOFError):
        parser.exit(1, f"{stage}时无法读取数据。\n")
    except ValueError as error:
        parser.exit(1, f"{stage}失败，{error}。\n")

    print(f"OPPO 已接受请求，messageId={result.get('messageId') or '未返回'}。")
    print("请核对手机上的通知编号与锁屏展示。")


if __name__ == "__main__":
    main()
