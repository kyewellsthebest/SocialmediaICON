"""When the next reel goes out.

Five a day, one an hour, first at seven in the morning - so the last one lands
at eleven and the page posts through the part of the day people are awake for,
rather than dumping eight at once at whatever time the daily job happened to
fire.

Every decision here is a pure function of a clock reading. Nothing sleeps,
nothing looks at the time on its own, and the timezone is passed in. That is
deliberate: a scheduler that reads the clock itself can only be tested by
waiting, and a bug at 06:59 is one nobody would ever find that way.

**The timezone matters more than it looks.** Railway runs in UTC, so "7am"
means seven in the morning somewhere, and the somewhere has to be said out
loud. Posting at 07:00 UTC when the audience is in Brisbane puts every reel
out at five in the afternoon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str) -> ZoneInfo:
    """The named timezone, or UTC if it is not one this machine knows.

    A typo must not stop the page posting: UTC at the wrong hour is a bad
    schedule, and a crash is no schedule at all.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Window:
    """The posting day: how many, how far apart, and when it opens."""

    start_hour: int = 7
    per_day: int = 5
    every_minutes: int = 60

    @property
    def end_hour(self) -> float:
        """When the last slot falls, for saying out loud rather than for logic."""
        return self.start_hour + (self.per_day - 1) * self.every_minutes / 60


def day_of(moment: datetime, tz: ZoneInfo) -> datetime:
    """Midnight local, for the day `moment` falls in.

    "How many have gone out today" has to mean the viewer's today. Counting
    against a UTC day would reset the tally at ten in the morning for an
    audience in Brisbane, and post ten reels on one afternoon.
    """
    local = moment.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def slots_elapsed(now: datetime, window: Window, tz: ZoneInfo) -> int:
    """How many of today's slots have come round, 0 before the day opens."""
    local = now.astimezone(tz)
    opens = day_of(now, tz).replace(hour=window.start_hour)
    if local < opens:
        return 0
    minutes = (local - opens).total_seconds() / 60
    return min(window.per_day, int(minutes // window.every_minutes) + 1)


def closes(now: datetime, window: Window, tz: ZoneInfo) -> datetime:
    """When today stops posting.

    The last slot plus one gap. Without a close, a service that restarts at
    five in the afternoon with nothing posted would find the day "open" and
    put five reels out through the evening - which is not what "five a day
    from seven" means, and is the opposite of posting when people are awake.
    """
    opens = day_of(now, tz).replace(hour=window.start_hour)
    return opens + timedelta(minutes=window.per_day * window.every_minutes)


def due(
    now: datetime,
    posted_today: int,
    last_post: datetime | None,
    window: Window,
    tz: ZoneInfo,
) -> tuple[bool, str]:
    """Whether a reel should go out right now, and why not if it should not.

    The reason is returned rather than logged because it is the answer to the
    question people actually ask - "why has nothing posted?" - and every one
    of these is a different thing to go and look at.

    Missed slots stay missed. A quiet morning means fewer posts that day
    rather than four arriving in four minutes once the service comes back -
    catching up by dumping the backlog is the burst this schedule exists to
    avoid.
    """
    local = now.astimezone(tz)

    if posted_today >= window.per_day:
        return False, (
            f"all {window.per_day} have gone out today; "
            f"the next is tomorrow at {window.start_hour:02d}:00"
        )

    if local.hour < window.start_hour:
        return False, (
            f"the day opens at {window.start_hour:02d}:00 "
            f"{tz.key} - it is {local:%H:%M}"
        )

    shut = closes(now, window, tz)
    if local >= shut:
        return False, (
            f"today's window closed at {shut:%H:%M} "
            f"({window.per_day} slots from {window.start_hour:02d}:00)"
        )

    elapsed = slots_elapsed(now, window, tz)
    if posted_today >= elapsed:
        return False, f"slot {posted_today + 1} of {window.per_day} has not come round yet"

    if last_post is not None:
        waited = local - last_post.astimezone(tz)
        gap = timedelta(minutes=window.every_minutes)
        if waited < gap:
            left = gap - waited
            return False, f"{int(left.total_seconds() // 60) + 1} min until the next slot"

    return True, ""


def next_slot(
    now: datetime,
    posted_today: int,
    last_post: datetime | None,
    window: Window,
    tz: ZoneInfo,
) -> datetime:
    """When the next reel will go out, in local time. For the dashboard."""
    local = now.astimezone(tz)
    opens = day_of(now, tz).replace(hour=window.start_hour)

    if posted_today >= window.per_day or local >= closes(now, window, tz):
        return opens + timedelta(days=1)
    if local < opens:
        return opens
    if last_post is None:
        return local
    return max(local, last_post.astimezone(tz) + timedelta(minutes=window.every_minutes))
