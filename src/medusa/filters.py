import re
from enum import StrEnum
from typing import Self

from .constants import META_RE
from .errors import MedusaError
from .suite import Suite


class Operator(StrEnum):
    ONLY = "="
    ANY = "~"


class FilterType(StrEnum):
    DEPS = "deps"
    STAGES = "stage"


class FilterExpr:
    def __init__(self, flt: FilterType, op: Operator, vals: set[str]):
        if flt == FilterType.STAGES:
            if op != Operator.ONLY:
                raise MedusaError(
                    "The 'stage' filter can only be used with the '=' operator!"
                )

        incl: set[str] = set()
        excl: set[str] = set()

        for val in vals:
            if val.startswith("!"):
                val = val.removeprefix("!")
                excl.add(val)
            else:
                incl.add(val)

            if not re.fullmatch(META_RE, val):
                raise MedusaError(
                    f"Filter value '{val}' is not a valid metadata value!"
                )

        self.flt = flt
        self.op = op
        self.incl = incl
        self.excl = excl

    @classmethod
    def from_arg(cls, arg: str) -> Self:
        pattern = r"(?P<flt>deps|stage)(?P<op>[=~])(?P<vals>.+)"

        matches = re.fullmatch(pattern, arg)
        if not matches:
            raise MedusaError(f"Filter '{arg}' has invalid format!")

        flt = FilterType(matches.group("flt"))
        op = Operator(matches.group("op"))
        vals = set(matches.group("vals").split(","))

        return cls(flt, op, vals)


class Filters:
    def __init__(self, args: list[str]):
        self._active = True
        self._deps_excl: set[str] = set()
        self._deps_incl: set[str] = set()
        self._stage_excl: set[str] = set()
        self._stage_incl: set[str] = set()
        self._mode: Operator | None = None

        if not args:
            self._active = False

        for arg in args:
            flt = FilterExpr.from_arg(arg)

            if flt.flt == FilterType.STAGES:
                self._stage_excl |= flt.excl
                self._stage_incl |= flt.incl
            else:
                if not self._mode:
                    self._mode = flt.op
                else:
                    if flt.op != self._mode:
                        raise MedusaError(
                            "The deps filter operators '=' and '~' can't be mixed!"
                        )
                self._deps_excl |= flt.excl
                self._deps_incl |= flt.incl

    def match_and_narrow(self, s: Suite) -> bool:
        """Checks whether the suite is allowed to run based on the filter rules
        and narrows dynamic deps if necessary to match the filter criteria.

        Returns True if the Suite is allowed by the filter, else False.
        """
        if not self._active:
            return True  # Not filtering

        if self._stage_excl and s.stage in self._stage_excl:
            return False  # Stage was excluded

        if self._stage_incl and s.stage not in self._stage_incl:
            return False  # Include active, Stage not included

        if self._deps_excl:
            if self._deps_excl.intersection(s.deps_static):
                return False  # One or more static deps were excluded

            for d in s.deps_dynamic.values():
                # Remove all excluded deps from DynDep options
                s.subtract_dynamic_stats(
                    d.options.intersection(self._deps_excl)
                )
                d.options.difference_update(self._deps_excl)
                if not d.options:
                    return False  # No options left for a dynamic dep

        if self._mode == Operator.ONLY:
            if self._deps_incl:
                if not self._deps_incl.issuperset(s.deps_static):
                    # One or more static deps were not included
                    return False

                for dyn in s.deps_dynamic.values():
                    # Remove all non-included deps from DynDep options
                    s.subtract_dynamic_stats(
                        dyn.options.difference(self._deps_incl)
                    )
                    dyn.options.intersection_update(self._deps_incl)

                    if not dyn.options:
                        return False  # No options left for a dynamic dep

                if s.deps_dynamic:
                    if s.try_assign_deps() is None:
                        # DynDeps are not solvable
                        return False
        else:
            # XXX(etaric): For Operator.ANY we only check against static deps.
            # Checking against dynamic deps would cause a lot of suites to be
            # included, not sure whether we want that - possibly to be changed
            # in the future.
            if self._deps_incl:
                if not self._deps_incl.intersection(s.deps_static):
                    return False  # Include active, no deps were included

        return True
