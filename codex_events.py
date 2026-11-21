"""Shared timestamps and quota windows from local Codex rollout events."""
import datetime as dt
import math


def event_time(record):
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        try:
            return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    completed = record.get("payload", {}).get("completed_at")
    if type(completed) in (int, float) and math.isfinite(completed):
        return float(completed)
    return None


def quota_windows(rate_limits):
    if not isinstance(rate_limits, dict) or rate_limits.get("limit_id") not in (None, "codex"):
        return []
    windows = []
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        minutes = window.get("window_minutes")
        used = window.get("used_percent")
        if type(minutes) is not int or minutes <= 0 or type(used) not in (int, float) or not 0 <= used <= 100:
            continue
        resets_at = window.get("resets_at")
        windows.append((minutes, 100 - used, resets_at if type(resets_at) is int and resets_at > 0 else None))
    return windows
