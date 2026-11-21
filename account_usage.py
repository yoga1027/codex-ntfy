"""Read the two desktop clients' account limits without starting a turn."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from codex_app_server import AppServerError
from notification_queue import open_database


def window(used, reset, now):
    if isinstance(reset, str):
        try:
            reset = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError, OSError):
            reset = None
    if type(reset) is not int or reset <= 0:
        reset = None
    if reset is not None and reset <= now:
        return None
    if type(used) not in (int, float) or not 0 <= used <= 100:
        return None
    return {"remaining": round(100 - used, 2), "reset": reset}


def codex_limits(data):
    bucket = (data.get("rateLimitsByLimitId") or {}).get("codex") or data.get("rateLimits") or {}
    result = {}
    now = time.time()
    if bucket.get("limitId") in (None, "codex"):
        for name in ("primary", "secondary"):
            item = bucket.get(name) or {}
            if item.get("windowDurationMins") in (300, 10080):
                result[str(item["windowDurationMins"])] = window(item.get("usedPercent"), item.get("resetsAt"), now)
    cards = data.get("rateLimitResetCredits")
    if isinstance(cards, dict) and type(cards.get("availableCount")) is int:
        result["card_count"] = cards["availableCount"]
        if isinstance(cards.get("credits"), list):
            result["expiries"] = [credit.get("expiresAt") for credit in cards["credits"]
                                  if isinstance(credit, dict) and credit.get("status") == "available"]
    return result


def cached_codex_usage(phone):
    account = phone.app_server.request("account/read", {"refreshToken": False})
    account_id = (account.get("workspaceRouting") or {}).get("chatgptAccountId")
    if not account.get("account") or not account_id:
        return {"unavailable": True}
    with closing(open_database(phone.config)) as db:
        row = db.execute("SELECT observed_at, snapshot FROM codex_account_usage WHERE account_id=?",
                         (account_id,)).fetchone()
    if row is None:
        return {"unavailable": True}
    now = time.time()
    age = now - row["observed_at"]
    saved = json.loads(row["snapshot"])
    result = {}
    for minutes in (300, 10080):
        item = saved.get(str(minutes))
        if item and 0 <= age < minutes * 60 and (item["reset"] is None or now < item["reset"]):
            result[str(minutes)] = item
    if "card_count" in saved and 0 <= age < 300 * 60:
        expiries = saved.get("expiries", [])
        # A partial detail list cannot establish a new balance after a known card expires.
        if all(expiry is None or expiry > now for expiry in expiries):
            result["card_count"] = saved["card_count"]
            if "expiries" in saved:
                result["expiries"] = expiries
    return {**result, "observed_at": row["observed_at"]} if result else {"unavailable": True}


def codex_usage(phone):
    try:
        data = phone.app_server.request("account/rateLimits/read", {})
    except (OSError, AppServerError):
        return cached_codex_usage(phone)
    result = codex_limits(data)
    account_id = data.get("accountId")
    if account_id and result:
        try:
            with closing(open_database(phone.config)) as db, db:
                db.execute("INSERT OR REPLACE INTO codex_account_usage VALUES (?, ?, ?)",
                           (account_id, int(time.time()), json.dumps(result)))
        except sqlite3.Error:
            pass  # The live read is still usable when the optional snapshot cannot be saved.
    return result


def claude_snapshot(home):
    if home is None:
        return {}
    path = (home.parent if home == Path.home() / ".claude" else home) / ".claude.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    snapshot = config.get("cachedUsageUtilization")
    account = config.get("oauthAccount")
    if (isinstance(snapshot, dict) and isinstance(account, dict) and account.get("accountUuid")
            and account["accountUuid"] == snapshot.get("accountUuid")
            and isinstance(snapshot.get("utilization"), dict) and type(snapshot.get("fetchedAtMs")) is int):
        return snapshot
    return {}


def claude_usage(claude):
    try:
        response = claude.control_info(claude.home, "get_usage")
    except OSError:
        response = {}
    if response.get("rate_limits_available") is False:
        return {"unavailable": True}
    live = response.get("rate_limits")
    snapshot = claude_snapshot(claude.home)
    cached = snapshot.get("utilization", {})
    keys = (("five_hour", 300), ("seven_day", 10080))
    use_cached = not isinstance(live, dict) or not any(live.get(key) for key, _ in keys)
    data = cached if use_cached else live
    now = time.time()
    observed = snapshot.get("fetchedAtMs", 0) // 1000
    from_snapshot = use_cached or all(data.get(key) == cached.get(key) for key, _ in keys)
    result = {}
    for key, minutes in keys:
        item = data.get(key)
        if from_snapshot and not 0 <= now - observed < minutes * 60:
            continue
        if isinstance(item, dict):
            result[str(minutes)] = window(item.get("utilization"), item.get("resets_at"), now)
    if not any(result.values()):
        return {"unavailable": True}
    if from_snapshot:
        result["observed_at"] = observed
    return result


def account_usage(phone):
    with ThreadPoolExecutor(max_workers=2) as pool:
        queries = {"codex": pool.submit(codex_usage, phone),
                   "claude": pool.submit(claude_usage, phone.claude)}
        result = {}
        for name, future in queries.items():
            try:
                result[name] = future.result()
            except (OSError, ValueError, sqlite3.Error, AppServerError):
                result[name] = {"unavailable": True}
    return {**result, "queried_at": int(time.time())}
