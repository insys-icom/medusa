import secrets
from collections import Counter
from enum import Enum, auto
from itertools import chain
from pathlib import Path
from typing import Any

from .errors import MetadataError
from .utils import Stats, Timeout, Timer


class Status(Enum):
    PENDING = auto()
    STARTED = auto()
    FINISHED = auto()


class DynDep:
    def __init__(self, options: set[str]) -> None:
        self.options = options
        self._value: str | None = None

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
        source: Path,
        stage: str,
        deps_static: frozenset[str],
        deps_dynamic: dict[str, DynDep],
        timeout: Timeout | None,
        for_vars: dict[str, Any] | None,
        **kwargs,
    ) -> None:
        self.full_name = full_name
        self.source = source
        self.stage = stage
        self.deps_static = deps_static
        self.deps_dynamic = deps_dynamic
        self.timeout = timeout
        self.for_vars = for_vars
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
            # 4 random bytes ~ 1% collision for 10k suites, we will likely have
            # much less identical suites in reality
            self.suffix = " " + secrets.token_hex(4)
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

    def try_assign_deps(
        self, available_deps: set[str] | None = None
    ) -> set[str] | None:
        if available_deps is None:
            available_deps = set(self.deps_static).union(
                {d for d in self.deps_dynamic_cnt.keys()}
            )

        if not self.deps_static.issubset(available_deps):
            return None

        assignments = self._get_deps_assignment(
            available_deps - self.deps_static
        )
        if assignments is None:
            return None

        for name, _get_deps_assignment in assignments.items():
            self.deps_dynamic[name].value = _get_deps_assignment

        return self.deps

    def _get_deps_assignment(
        self, available_deps: set[str]
    ) -> dict[str, str] | None:
        """Attempt to find a distinct dependency to each DynDep using Kuhn's
        Algorithm. Only options from available_deps are considered. If
        available_deps is None, all options are considered.

        Returns a dict of name: chosen_option if every DynDep can be satisfied
        or None if no solution exists.
        """
        if not self.deps_dynamic:
            return {}

        # Filter options by available_deps
        options = {
            name: dyn.options.intersection(available_deps)
            for name, dyn in self.deps_dynamic.items()
        }

        # Early exit if any DynDep has no available options after filtering
        if any(not opts for opts in options.values()):
            return None

        # Mapping: option -> DynDep name
        option_owner: dict[str, str] = {}

        def try_assign(dyndep_name: str, seen: set[str]) -> bool:
            """Depth-first search for finding a possible assignment. When we find
            an option that is already owned by a different DynDep, we recursively
            call this function to find a different option for the conflicting
            DynDep. Returns True if an assignment was found, False if not.
            """
            # Go through all options for the current DynDep, find one that has not
            # been seen yet in a higher recursion level
            for option in options[dyndep_name]:
                if option in seen:
                    continue

                # We found an unseen option to use, mark it as seen so that deeper
                # recursion levels don't use it
                seen.add(option)

                owner = option_owner.get(option)
                if owner is None or try_assign(owner, seen):
                    # Either there was no owner or we successfully found a different
                    # option for the owner, so this DynDep becomes the new owner
                    option_owner[option] = dyndep_name
                    return True

            # There was no unseen option, no assignment possible
            return False

        # Find an option for each DynDep
        for name in self.deps_dynamic:
            if not try_assign(name, set()):
                return None

        # Return assignments as a dict of name: chosen_option
        return {owner: opt for opt, owner in option_owner.items()}
