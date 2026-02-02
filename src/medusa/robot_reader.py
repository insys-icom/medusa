import re
from collections import Counter
from collections.abc import Iterable, Mapping
from itertools import chain
from pathlib import Path
from typing import Any

from robot import running
from robot.api.interfaces import ListenerV3

from .constants import META_RE
from .data import Data
from .errors import MedusaError, MetadataError, SuiteError, VariableError
from .robot_handler import RobotHandler, RobotHandlerInterface, Undefined
from .suite import DynDep, Suite
from .utils import Timeout


class RobotSuiteWalker(ListenerV3):
    def __init__(self, data: Data, errors: list[str]):
        self.data = data
        self.errors = errors
        self.reader = RobotSuiteReader()

    def start_suite(self, suite: running.TestSuite, _):
        if not suite.tests:
            return  # For now we only process leaf suites that contain tests

        try:
            for s in self.reader.get_suites(suite):
                self.data.insert(s)
        except SuiteError as e:
            self.errors.append(str(e))
        except Exception as e:
            self.errors.append(str(SuiteError(str(suite.source), str(e))))

    def start_invalid_keyword(self, keyword, implementation, __):
        self.errors.append(
            str(
                MedusaError(
                    f"{keyword.parent.source} line {keyword.lineno}",
                    str(implementation.error),
                )
            )
        )


class RobotSuiteReader:
    def __init__(self, robot_handler: RobotHandlerInterface | None = None):
        if robot_handler:
            self.robot_handler = robot_handler
        else:
            self.robot_handler = RobotHandler()

    def get_suites(self, suite: running.TestSuite) -> list[Suite]:
        if var_maps := self._get_for(suite):
            return [self._get_suite(suite, var_map) for var_map in var_maps]
        else:
            return [self._get_suite(suite, None)]

    def _get_suite(
        self, suite: running.TestSuite, varmap: dict[str, Any] | None = None
    ) -> Suite:
        if varmap:
            self.robot_handler.set_variables(varmap)

        full_name = self.robot_handler.replace_variables(suite.full_name)
        assert isinstance(suite.source, Path)
        source = suite.source
        stage = self._get_stage(suite)
        timeout = self._get_timeout(suite)
        deps_static, deps_dynamic = self._get_deps(suite)
        tags = Counter(
            chain.from_iterable([test.tags for test in suite.tests])
        )
        n_tests = suite.test_count
        for_vars = varmap

        return Suite(
            full_name=full_name,
            source=source,
            stage=stage,
            timeout=timeout,
            for_vars=for_vars,
            deps_static=deps_static,
            deps_dynamic=deps_dynamic,
            tags=tags,
            n_tests=n_tests,
        )

    def _get_stage(self, suite: running.TestSuite) -> str:
        stage = self.robot_handler.get_metadata(suite, "medusa:stage", True)
        assert isinstance(stage, str)

        try:
            stage = str(self.robot_handler.replace_variables(stage))
        except Exception as e:
            raise MetadataError("medusa:stage", str(e))

        if not re.fullmatch(META_RE, stage):
            raise MetadataError(
                "medusa:stage",
                f"Invalid characters in '{stage}', name must match '{META_RE}'",
            )

        return stage

    def _get_deps(
        self, suite: running.TestSuite
    ) -> tuple[frozenset[str], dict[str, DynDep]]:
        """Get static and dynamic dependencies from suite metadata.

        First splits the metadata into individual arguments, tries to resolve
        them as variables and then expands list variables into individual
        values. The values are first attempted to be parsed as DynDeps and then
        as static deps.

        Returns tuple[static, dict[dyn_name, DynDep]]
        """
        deps_meta = self.robot_handler.get_metadata(suite, "medusa:deps", True)
        assert isinstance(deps_meta, str)

        try:
            deps_values: list[str] = []

            for dep in self._split_args(deps_meta):
                try:
                    resolved = self.robot_handler.get_variable_value(dep)
                except VariableError:
                    # Not a single variable, but may contain variables
                    resolved = dep

                # Add list items if it's a list, else just add it as a str
                if isinstance(resolved, Iterable) and not isinstance(
                    resolved, str
                ):
                    for element in resolved:
                        deps_values.append(str(element))
                else:
                    deps_values.append(str(resolved))

            deps_static: set[str] = set()
            deps_dynamic: dict[str, DynDep] = {}

            for dep in deps_values:
                if name_opts_tup := self._get_deps_dynamic(dep):
                    name, options = name_opts_tup
                    if name in deps_dynamic:
                        raise MetadataError(
                            "medusa:deps",
                            f"Duplicate dynamic dependency variable '{name}'",
                        )
                    deps_dynamic[name] = DynDep(options)
                else:
                    resolved = self.robot_handler.replace_variables(dep)
                    deps_static.add(resolved)

            all_deps = set(deps_static).union(
                opt for d in deps_dynamic.values() for opt in d.options
            )
            for dep in all_deps:
                if not re.fullmatch(META_RE, dep):
                    raise MetadataError(
                        "medusa:deps",
                        f"Invalid characters in '{dep}', name must match '{META_RE}'",
                    )
        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError("medusa:deps", str(e))

        return (frozenset(deps_static), deps_dynamic)

    def _get_deps_dynamic(self, dep: str) -> tuple[str, set[str]] | None:
        """Try to desolve dependency string ``dep`` as a dynamic dependency.

        Returns name and options if successful or None if it does not match the
        dynamic dependency pattern.

        Raises if it matches the pattern but can't be evaluated successfully.
        """
        match = re.fullmatch(
            "ANY (?P<varname>.+) [iI][nN] (?P<listname>.+)", dep
        )
        if not match:
            return None

        varname = match.group("varname")
        listname = match.group("listname")

        try:
            varname_val = self.robot_handler.get_variable_value(varname)
        except VariableError as e:
            raise MetadataError(
                "medusa:deps",
                f"Failed to resolve dynamic dependency target value: {e}",
            )

        if varname_val is Undefined:
            raise MetadataError(
                "medusa:deps",
                f"The target variable of a dynamic dependency needs to be defined with a value but '{varname}' is undefined!",
            )

        # XXX(etaric): Can't force this to be None if we want to allow flexible
        #              combinations with medusa:for where it overwrites its own
        #              variable with the final value
        # if varname_val is not None:
        #     raise MetadataError(
        #         "medusa:deps",
        #         f"The target variable of a dynamic dependency needs to be defined with the value None but '{varname}' has value '{varname_val}'!",
        #     )

        try:
            listname_val = self.robot_handler.get_variable_value(listname)
        except VariableError as e:
            raise MetadataError(
                "medusa:deps",
                f"Failed to resolve dynamic dependency target value: {e}",
            )

        if listname_val is Undefined:
            raise MetadataError(
                "medusa:deps",
                f"The dynamic dependency options variable '{listname}' is undefined!",
            )

        try:
            opts_iter = iter(listname_val)
        except Exception:
            raise MetadataError(
                "medusa:deps",
                f"The dynamic dependency options variable '{listname}' is not iterable!",
            )

        if not all(isinstance(opt, str) for opt in opts_iter):
            raise MetadataError(
                "medusa:deps",
                f"The dynamic dependency options variable '{listname}' contains non-string values!",
            )

        options = set(listname_val)
        if len(options) <= 0:
            raise MetadataError(
                "medusa:deps",
                f"The dynamic dependency options variable '{listname}' is empty!",
            )

        return (varname, options)

    def _get_timeout(self, suite: running.TestSuite) -> Timeout | None:
        try:
            timeout_str = self.robot_handler.get_metadata(
                suite, "medusa:timeout", False
            )
            if not timeout_str:
                return None

            timeout_str = self.robot_handler.replace_variables(timeout_str)
            return Timeout.from_argstr(timeout_str)
        except Exception as e:
            raise MetadataError("medusa:timeout", str(e))

    def _get_for(
        self, suite: running.TestSuite
    ) -> list[dict[str, Any]] | None:
        try:
            args_str = self.robot_handler.get_metadata(
                suite, "medusa:for", False
            )
            if args_str is None:
                return None

            args = self._split_args(args_str)
            if len(args) < 3:
                raise MetadataError("medusa:for", "Not enough arguments")

            if not args[-2].upper() == "IN":
                raise MetadataError(
                    "medusa:for",
                    "Format should be '$TARGET [$TARGET...] IN $SOURCE' but 'IN' was not found!",
                )

            source = self.robot_handler.get_variable_value(args[-1])
            if source is Undefined or source is None:
                raise MetadataError(
                    "medusa:for",
                    f"Source variable '{args[-1]}' is unset or None",
                )

            vars = args[0:-2]
            for i, var in enumerate(vars.copy()):
                val = self.robot_handler.get_variable_value(var)
                if val is Undefined:
                    raise MetadataError(
                        "medusa:for",
                        f"Variable '{var}' is not defined. Target variables must be defined with value '${{None}}'",
                    )

                if val is not None:
                    raise MetadataError(
                        "medusa:for",
                        f"Variable '{var}' already has value '{val}'. Target variables must be defined with value '${{None}}'",
                    )
                vars[i] = var.strip("${}")

            if isinstance(source, Mapping):
                # For a mapping, map key to var1 and value to var2
                return self._get_for_from_mapping(vars, source)
            else:
                # Otherwise try to iterate and bind the value(s) to the var(s)
                return self._get_for_from_iterable(vars, source)

        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError("medusa:for", str(e))

    def _get_for_from_mapping(
        self, vars: list[str], mapping: Mapping
    ) -> list[dict[str, Any]]:
        if len(vars) != 2:
            raise MetadataError(
                "medusa:for",
                f"Source is a mapping, which can only be assigned to 2 variables but there are {len(vars)}",
            )
        maps = [dict(zip(vars, tup)) for tup in mapping.items()]
        return maps

    def _get_for_from_iterable(
        self, vars: list[str], iterable: Any
    ) -> list[dict[str, Any]]:
        """Raises MetadataError if the value is not iterable"""
        maps: list[dict[str, Any]] = []

        try:
            source_iter = iter(iterable)
        except Exception:
            raise MetadataError(
                "medusa:for", "Source variable is not iterable"
            )

        for val_i, val in enumerate(source_iter, start=1):
            if len(vars) == 1:
                maps.append({vars[0]: val})
                continue
            else:
                try:
                    maps.append(dict(zip(vars, val)))
                except Exception:
                    raise MetadataError(
                        "medusa:for",
                        f"Source item {val_i} element count does not match variable count",
                    )

        return maps

    def _split_args(self, args: str) -> list[str]:
        res = re.split(r" {2,}", args)
        return res
