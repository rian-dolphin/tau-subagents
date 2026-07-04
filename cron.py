"""Vendored minimal 5-field cron matcher (no external dependencies).

pi-subagents uses croner, a 6-field (second-precision) cron engine. To avoid a
new dependency we vendor a small 5-field matcher instead, covering the standard
Unix crontab format:

    minute  hour  day-of-month  month  day-of-week

Documented supported subset per field:

    ``*``       every value in the field's range
    ``N``       a single number
    ``A,B,C``   a comma-separated list
    ``A-B``     an inclusive range
    ``*/N``     a step over the whole range
    ``A-B/N``   a step over a range
    ``N/M``     a step from N to the field maximum

Field ranges: minute 0-59, hour 0-23, day-of-month 1-31, month 1-12,
day-of-week 0-6 (0 = Sunday; 7 is also accepted as Sunday). Month and weekday
*names* (JAN, MON, ...) are NOT supported — numbers only.

When both day-of-month and day-of-week are restricted (neither is ``*``) the
day matches if *either* field matches, following Vixie cron. Seconds are not
supported (the smallest granularity is one minute), which is the main deviation
from pi's croner-based 6-field expressions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_MINUTE = (0, 59)
_HOUR = (0, 23)
_DOM = (1, 31)
_MONTH = (1, 12)
_DOW = (0, 6)

# Cap the forward search so a pathological expression (one that never matches)
# returns None instead of looping forever. Four years covers Feb-29 crons.
_MAX_SEARCH = timedelta(days=366 * 4 + 2)


class CronField:
    """One parsed cron field: the set of allowed values and whether it is ``*``."""

    __slots__ = ("restricted", "values")

    def __init__(self, values: frozenset[int], restricted: bool) -> None:
        self.values = values
        self.restricted = restricted

    def __contains__(self, value: int) -> bool:
        return value in self.values


def _parse_field(field: str, low: int, high: int, *, is_dow: bool = False) -> CronField:
    """Parse a single cron field into a CronField over ``[low, high]``."""
    field = field.strip()
    if field == "":
        raise ValueError("empty cron field")
    restricted = field != "*"
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        rng = part
        if "/" in part:
            rng, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"invalid step in cron field: {part!r}") from exc
            if step <= 0:
                raise ValueError(f"cron step must be positive: {part!r}")
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            start_text, _, end_text = rng.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = int(rng)
            # "N/M" means "from N to the maximum, stepping by M".
            end = high if step > 1 else start
        for value in range(start, end + 1, step):
            normalized = 0 if is_dow and value == 7 else value
            if normalized < low or normalized > high:
                raise ValueError(f"cron value {value} out of range [{low}, {high}]")
            values.add(normalized)
    if not values:
        raise ValueError(f"cron field matches nothing: {field!r}")
    return CronField(frozenset(values), restricted)


class CronExpression:
    """A parsed 5-field cron expression with ``matches`` and ``next_after``."""

    __slots__ = ("day_of_month", "day_of_week", "expression", "hour", "minute", "month")

    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(
                "cron must have 5 fields (minute hour day-of-month month "
                f"day-of-week), got {len(fields)}"
            )
        self.expression = expression
        self.minute = _parse_field(fields[0], *_MINUTE)
        self.hour = _parse_field(fields[1], *_HOUR)
        self.day_of_month = _parse_field(fields[2], *_DOM)
        self.month = _parse_field(fields[3], *_MONTH)
        self.day_of_week = _parse_field(fields[4], *_DOW, is_dow=True)

    def _day_matches(self, moment: datetime) -> bool:
        # Cron weekday: 0 = Sunday. Python weekday(): 0 = Monday.
        cron_dow = (moment.weekday() + 1) % 7
        dom_ok = moment.day in self.day_of_month
        dow_ok = cron_dow in self.day_of_week
        if self.day_of_month.restricted and self.day_of_week.restricted:
            return dom_ok or dow_ok
        if self.day_of_month.restricted:
            return dom_ok
        if self.day_of_week.restricted:
            return dow_ok
        return True

    def matches(self, moment: datetime) -> bool:
        """True if ``moment`` (to the minute) satisfies the expression."""
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and moment.month in self.month
            and self._day_matches(moment)
        )

    def next_after(self, after: datetime) -> datetime | None:
        """Next matching minute strictly after ``after``, or None if unreachable."""
        moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = after + _MAX_SEARCH
        while moment <= limit:
            if moment.month not in self.month:
                moment = _first_of_next_month(moment)
                continue
            if not self._day_matches(moment):
                moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if moment.hour not in self.hour:
                moment = (moment + timedelta(hours=1)).replace(minute=0)
                continue
            if moment.minute not in self.minute:
                moment = moment + timedelta(minutes=1)
                continue
            return moment
        return None


def _first_of_next_month(moment: datetime) -> datetime:
    year = moment.year + (1 if moment.month == 12 else 0)
    month = 1 if moment.month == 12 else moment.month + 1
    return moment.replace(
        year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
    )


def validate_cron(expression: str) -> bool:
    """Return True if ``expression`` parses as a supported 5-field cron."""
    try:
        CronExpression(expression)
    except (ValueError, TypeError):
        return False
    return True
