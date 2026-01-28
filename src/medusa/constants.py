from datetime import datetime
from pathlib import Path

META_RE: str = r"[a-zA-Z0-9:][a-zA-Z0-9:._-]*"
OUTPUTDIR = Path("results", datetime.now().strftime("%Y-%m-%d_%H%M%S"))
REPO_LINK = "https://github.com/insys-icom/medusa"
T_HARD = 60
T_KILL = 10
