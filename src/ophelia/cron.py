"""Five-field UTC cron validation and matching without scheduler dependencies."""

from __future__ import annotations

from datetime import datetime
from typing import Set


class CronExpressionError(ValueError):
    pass


_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def validate_cron_expression(expression: str) -> None:
    _parse(expression)


def cron_matches(expression: str, instant: datetime) -> bool:
    values = _parse(expression)
    # Python Monday is 0. Cron Sunday is 0 (and also accepts 7).
    weekday = (instant.weekday() + 1) % 7
    observed = (instant.minute, instant.hour, instant.day, instant.month, weekday)
    return all(value in allowed for value, allowed in zip(observed, values))


def _parse(expression: str) -> tuple[Set[int], ...]:
    if not isinstance(expression, str):
        raise CronExpressionError("Cron expression must be text.")
    fields = expression.split()
    if len(fields) != 5:
        raise CronExpressionError("Cron expression must contain five fields.")
    return tuple(
        _field(field, minimum, maximum, weekday=index == 4)
        for index, (field, (minimum, maximum)) in enumerate(zip(fields, _RANGES))
    )


def _field(value: str, minimum: int, maximum: int, *, weekday: bool) -> Set[int]:
    result: Set[int] = set()
    for part in value.split(","):
        if not part:
            raise CronExpressionError("Cron fields may not contain empty list items.")
        base, step = (part.split("/", 1) + [None])[:2] if "/" in part else (part, None)
        if step is not None:
            increment = _number(step, 1, maximum - minimum + 1)
        else:
            increment = 1
        if base in {"*", "?"}:
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _number(raw_start, minimum, maximum)
            end = _number(raw_end, minimum, maximum)
            if start > end:
                raise CronExpressionError("Cron ranges must be ascending.")
        else:
            start = _number(base, minimum, maximum)
            end = start
            if step is not None:
                end = maximum
        result.update(range(start, end + 1, increment))
    if weekday and 7 in result:
        result.add(0)
        result.discard(7)
    if not result:
        raise CronExpressionError("Cron field selects no values.")
    return result


def _number(value: str, minimum: int, maximum: int) -> int:
    if not value.isdigit():
        raise CronExpressionError("Cron values must be integers.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise CronExpressionError(
            "Cron value %d is outside %d through %d." % (parsed, minimum, maximum)
        )
    return parsed
