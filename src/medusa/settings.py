import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .filters import Filters
    from .utils import Timeout

# TODO(SZI): Das sollte lieber in die constants.py datei, und lieber ohne das
# "http://" vorne dran, da wir ja erstmal ausschließlich http unterstützen.
# Das kann dann intern hinzugefügt werden.
DEFAULT_SERVER_URL = "http://127.0.0.1:8080"


# TODO(SZI): Lieber direkt os.environ.get() aufrufen in main()
def get_server_url() -> str | None:
    """Return the configured Medusa server URL, if set."""
    return os.environ.get("MEDUSA_SERVER")


@dataclass(frozen=True)
class Settings:
    filters: "Filters"
    log_level: int
    outputdir: "Path"
    robotargs: list[str]
    timeout: "Timeout|None"
    server_url: str | None = None
