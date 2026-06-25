from datetime import datetime, timedelta

_BUFFER = timedelta(minutes=2)


def compute_wake_time(
    reset_time: datetime | None,
    now: datetime,
    minimum_unknown_reset_duration: timedelta = timedelta(0),
) -> tuple[datetime, bool]:
    if reset_time is not None:
        return reset_time + _BUFFER, False

    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    wake = next_hour + _BUFFER
    if minimum_unknown_reset_duration <= timedelta(0):
        return wake, True

    minimum_reset_time = now + minimum_unknown_reset_duration
    while wake < minimum_reset_time:
        wake += timedelta(hours=1)
    return wake, True
