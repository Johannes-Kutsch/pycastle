import asyncio
from dataclasses import dataclass, field


@dataclass
class CancellationToken:
    _event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
