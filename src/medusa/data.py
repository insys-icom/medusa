from .filters import Filters
from .suite import Status, Suite
from .utils import Stats, Timer


class Stage(Stats, Timer):
    def __init__(self, name: str) -> None:
        super().__init__(t_name=f"stage {name}")
        self.name = name
        self.suites: list[Suite] = []

    def insert(self, s: Suite):
        self.add_stats(s)
        self.suites.append(s)

    @property
    def pending(self) -> int:
        return len([s for s in self.suites if s.status == Status.PENDING])

    @property
    def started(self) -> int:
        return len([s for s in self.suites if s.status == Status.STARTED])

    @property
    def finished(self) -> int:
        return len([s for s in self.suites if s.status == Status.FINISHED])


class Data(Stats):
    def __init__(self, filters: Filters) -> None:
        super().__init__()
        self.filters = filters
        self.stages: dict[str, Stage] = {}

    def insert(self, s: Suite):
        if self.filters.match_and_narrow(s):
            self.add_stats(s)
            self.stages.setdefault(s.stage, Stage(s.stage)).insert(s)
