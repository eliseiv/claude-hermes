"""Global (project-independent) server-side tool handlers (ADR-026).

Unlike SiteToolHandlers (project-scoped site.*, ADR-011), these handlers are NOT tied to a
WebsiteService/project — they execute in the chat tool-loop without an external_project_id and
are offered to Claude in every turn (including «чистый чат» with no project, ADR-022).

Currently a single tool: ``time.now`` (ADR-026 §6). It returns the current date/time via an
injectable ``Clock`` provider (determinism for qa, ADR-026 §8 / 06-testing-strategy) — never a
direct ``datetime.now()``. The result always carries a UTC set (``utc``/``unix``/``weekday``);
a valid IANA ``tz`` additionally yields ``local``/``timezone``. An invalid/unknown/over-long tz
degrades to a ``ToolExecution.error("invalid_timezone", ...)`` (the turn survives, ADR-026 §6) —
never a raised exception.

The same ``ToolExecution`` contract as SiteToolHandlers is reused (single tool-result contract for
the orchestrator). Only the frozen dataclass is imported from website.tools — no website
infrastructure is instantiated here (ADR-026 §5).
"""

from __future__ import annotations

import datetime
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.chat.tools import TIME_NOW_TZ_MAX_LENGTH, TOOL_TIME_NOW
from app.website.tools import ToolExecution

# English weekday names by UTC date (Monday..Sunday), ADR-026 §6.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@runtime_checkable
class Clock(Protocol):
    """Injectable source of the current time (ADR-026 §8).

    ``now()`` MUST return a timezone-aware UTC ``datetime``. The default implementation is
    ``SystemClock``; tests inject a ``FixedClock`` for determinism.
    """

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Default Clock: the real wall-clock time in UTC (ADR-026 §8)."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class GlobalToolHandlers:
    """Dispatch + handlers for global server-side tools (ADR-026).

    Project-independent: no WebsiteService, no external_project_id, no session-context args. Time is
    taken from the injected ``Clock`` (default ``SystemClock``).
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else SystemClock()

    async def execute(self, *, tool_name: str, args: dict[str, Any]) -> ToolExecution:
        """Execute a global server-side tool. Returns a ToolExecution (result or error envelope)."""
        if tool_name == TOOL_TIME_NOW:
            return self._time_now(args)
        # Unknown global tool name — should never happen (validated upstream against the registry).
        return ToolExecution.error("unknown_tool", f"unknown global server-side tool: {tool_name}")

    def _time_now(self, args: dict[str, Any]) -> ToolExecution:
        now_utc = self._clock.now()
        # Defensive: a Clock contract violation (naive/non-UTC) would corrupt the offsets; normalize
        # to UTC so utc/unix/weekday are always correct (ADR-026 §6 — UTC set independent of tz).
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=datetime.UTC)
        else:
            now_utc = now_utc.astimezone(datetime.UTC)

        result: dict[str, Any] = {
            "utc": now_utc.isoformat(),
            # Integer Unix timestamp in seconds (UTC), ADR-026 §6.
            "unix": int(now_utc.timestamp()),
            "weekday": _WEEKDAYS[now_utc.weekday()],
        }

        tz_raw = args.get("tz")
        if tz_raw is None:
            # No tz → UTC-only set (timezone/local omitted), ADR-026 §6.
            return ToolExecution.ok(result)

        tz_name = str(tz_raw)
        # Q-026-1: length cap (≤ 64) enforced here so an over-long tz degrades to invalid_timezone
        # (a tool-result error, the turn survives) rather than 422-ing the turn (ADR-026 §6).
        if len(tz_name) > TIME_NOW_TZ_MAX_LENGTH:
            return ToolExecution.error(
                "invalid_timezone",
                f"timezone name exceeds the {TIME_NOW_TZ_MAX_LENGTH}-character limit",
            )
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # Unknown/unparseable IANA name, missing tz database in the image (TD-019), or a
            # filesystem-hostile name when a tz database IS present (ZoneInfo treats the name as a
            # path and the OS rejects it, e.g. OSError Errno 22) → invalid_timezone tool-result
            # error; the UTC set is still available, the turn survives (ADR-026 §6).
            return ToolExecution.error(
                "invalid_timezone", f"unknown or unavailable timezone: {tz_name}"
            )

        local_dt = now_utc.astimezone(zone)
        # Normalized IANA name (key(zone) is the canonical name passed to ZoneInfo), ADR-026 §6.
        result["timezone"] = str(zone.key)
        result["local"] = local_dt.isoformat()
        return ToolExecution.ok(result)
