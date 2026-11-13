import hashlib
import json
from collections import Counter
from enum import Enum, StrEnum, auto
from itertools import chain
from typing import TYPE_CHECKING

from .errors import MetadataError
from .utils import Stats, Timer

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Self

    from .utils import Timeout


class Status(Enum):
    PENDING = auto()
    STARTED = auto()
    FINISHED = auto()


class Result(StrEnum):
    NOT_EXECUTED = "NOT EXECUTED"
    PASS = "PASS"
    FAIL = "FAIL"
    ERR = "ERR"

    @classmethod
    def from_exitcode(cls, exitcode: int | None) -> "Self":
        if exitcode is None:
            # TODO: Add nicer handling for this very unlikely case?
            return Result.ERR
        elif exitcode == 0:
            return Result.PASS
        elif exitcode > 0 and exitcode <= 250:
            return Result.FAIL
        else:
            return Result.ERR


class DynDep:
    def __init__(self, options: set[str]) -> None:
        self.options = options
        self._value: str | None = None

    @property
    def has_value(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> str:
        assert isinstance(self._value, str), (
            "Attempted to get DynDep value before it was determined"
        )
        return self._value

    @value.setter
    def value(self, val: str) -> None:
        assert not isinstance(self._value, str), (
            "Attempted to overwrite already fixed DynDep value"
        )
        self._value = val

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other) -> bool:
        try:
            return self.options == other.options
        except Exception:
            return NotImplemented


class Suite(Stats, Timer):
    def __init__(
        self,
        full_name: str,
        source: "Path",
        stage: str,
        deps_static: frozenset[str],
        deps_dynamic: dict[str, DynDep],
        timeout: "Timeout|None",
        for_vars: "dict[str, Any]|None",
        retry: int,
        **kwargs,
    ) -> None:
        self.full_name = full_name
        self.source = source
        self.stage = stage
        self.deps_static = deps_static
        self.deps_dynamic = deps_dynamic
        self.timeout = timeout
        self.for_vars = for_vars
        self.retry = retry
        self.result: Result = Result.NOT_EXECUTED
        self.status: Status = Status.PENDING
        self.suffix = ""

        for name, d in deps_dynamic.items():
            d.options.difference_update(deps_static)

            if not d.options:
                raise MetadataError(
                    "medusa:deps",
                    f"Dynamic dep '{name}' is impossible to satisfy, no options or all options are already taken by static deps!",
                )

        if self.for_vars:
            # 4 hash bytes should be sufficient to prevent collisions for the
            # expected medusa:for iteration count per suite of less than 1000
            try:
                self.suffix = " " + hashlib.shake_128(
                    json.dumps(self.for_vars).encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest(4)
            except Exception:
                raise MetadataError(
                    "medusa:for", "One or more values are not serializable"
                )
            self.full_name = self.full_name + self.suffix

        assert len(bytes(self.full_name, encoding="utf-8")) <= 255

        super().__init__(
            deps_static_cnt=Counter(deps_static),
            deps_dynamic_cnt=Counter(
                set(
                    chain.from_iterable(
                        [d.options for d in deps_dynamic.values()]
                    )
                )
            ),
            n_suites=1,
            **kwargs,
        )

    @property
    def deps(self) -> set[str]:
        """Return final resolved set of dependencies. Must be called after
        dynamic deps were already resolved.
        """
        return set(self.deps_static).union(
            {dyn.value for dyn in self.deps_dynamic.values()}
        )
