from dataclasses import dataclass
from pathlib import Path

from .filters import Filters
from .utils import Timeout


@dataclass(frozen=True)
class Settings:
    filters: Filters
    log_level: int
    outputdir: Path
    robotargs: list[str]
    timeout: Timeout | None
