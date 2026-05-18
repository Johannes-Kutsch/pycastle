import dataclasses


@dataclasses.dataclass(frozen=True)
class StageOverride:
    model: str = ""
    effort: str = ""
