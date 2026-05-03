"""Shared datetime helpers for timezone-aware formatting."""
import zoneinfo as _zi
from datetime import datetime as _dt


def parse_iso_dt(s: str) -> _dt:
    """Parse an ISO-8601 datetime string; treats naive values as UTC."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.fromisoformat(s)
    except ValueError:
        dt = _dt.fromisoformat(s[:19])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zi.ZoneInfo("UTC"))
    return dt


def to_local_hhmm(iso: str, timezone: str) -> str:
    """ISO datetime → HH:MM in the given IANA timezone."""
    try:
        return parse_iso_dt(iso).astimezone(_zi.ZoneInfo(timezone)).strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else "??"


def to_local_date(iso: str, timezone: str) -> str:
    """ISO datetime → YYYY-MM-DD in the given IANA timezone."""
    try:
        return parse_iso_dt(iso).astimezone(_zi.ZoneInfo(timezone)).strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]
