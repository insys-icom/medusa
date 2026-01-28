import logging
import re
from abc import ABC
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Self

from .constants import T_HARD, T_KILL
from .errors import MedusaError

LOGGER = logging.getLogger("medusa")


@dataclass
class Timeout:
    soft: int
    hard: int = T_HARD
    kill: int = T_KILL

    hard_total: int = field(init=False)  # Total seconds to hard timeout
    kill_total: int = field(init=False)  # Total seconds to kill timeout

    def __post_init__(self):
        self.hard_total = self.soft + self.hard
        self.kill_total = self.hard_total + self.kill

    @classmethod
    def from_argstr(cls, argstr: str | None) -> Self | None:
        if not argstr:
            return None

        if m := re.fullmatch(r"(\d+)(?:,(\d+))?(?:,(\d+))?", argstr):
            return cls(*[int(g) for g in m.groups() if g is not None])
        else:
            raise MedusaError(
                f"Invalid value '{argstr}' for timeout. Run 'medusa --help' for more information about timeout values."
            )


class Stats(ABC):
    def __init__(
        self,
        deps_static_cnt: Counter[str] | None = None,
        deps_dynamic_cnt: Counter[str] | None = None,
        tags: Counter[str] | None = None,
        n_suites: int = 0,
        n_tests: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.deps_static_cnt: Counter[str] = (
            deps_static_cnt if deps_static_cnt else Counter()
        )
        self.deps_dynamic_cnt: Counter[str] = (
            deps_dynamic_cnt if deps_dynamic_cnt else Counter()
        )
        self.tags: Counter[str] = tags if tags else Counter()
        self.n_suites = n_suites
        self.n_tests = n_tests

    def add_stats(self, s: "Stats"):
        self.n_suites += 1
        self.n_tests += s.n_tests
        self.deps_static_cnt.update(s.deps_static_cnt)
        self.deps_dynamic_cnt.update(s.deps_dynamic_cnt)
        self.tags.update(s.tags)


class Timer:
    """Keeps track of execution time. When given a name, it outputs start and
    finish messages when calling ``timer_start`` and ``timer_end``.
    """

    def __init__(self, t_name: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._t_name = t_name
        self._t_start: datetime | None = None
        self._t_end: datetime | None = None

    @property
    def t_start(self) -> datetime:
        """Time at which ``timer_start`` was called"""
        assert self._t_start
        return self._t_start

    @property
    def t_end(self) -> datetime:
        """Time at which ``timer_end`` was called"""
        assert self._t_end
        return self._t_end

    @property
    def t_duration(self) -> timedelta:
        """Duration from ``t_start`` to ``t_end``, rounded to seconds"""
        return timedelta(
            seconds=int((self.t_end - self.t_start).total_seconds())
        )

    @property
    def t_duration_accurate(self) -> timedelta:
        """Duration from ``t_start`` to ``t_end``"""
        return timedelta(seconds=(self.t_end - self.t_start).total_seconds())

    def timer_start(self):
        assert not self._t_start
        self._t_start = datetime.now()

        if self._t_name:
            print(f"Started {self._t_name}...")

    def timer_end(self):
        assert self._t_start
        assert not self._t_end
        self._t_end = datetime.now()

        if self._t_name:
            print(f"Finished {self._t_name} ({self.t_duration})", end="\n\n")
