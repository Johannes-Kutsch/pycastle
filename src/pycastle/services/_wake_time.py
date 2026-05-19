from datetime import datetime, timedelta

_BUFFER = timedelta(minutes=2)


def compute_wake_time(
    reset_time: datetime | None, now: datetime
) -> tuple[datetime, bool]:
    if reset_time is not None:
        return reset_time + _BUFFER, False
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour + _BUFFER, True
