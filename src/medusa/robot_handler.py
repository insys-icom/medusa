from abc import ABC, abstractmethod
from typing import Any

from robot import running
from robot.libraries.BuiltIn import BuiltIn  # type: ignore

from .errors import MetadataError, VariableError


class UndefinedType(object):
    """Used to check whether a robot variable was defined at all. By default
    robot returns None, but we want to use something else because we want to
    differentiate whether something was defined as None or undefined.
    """

    def __repr__(self) -> str:
        return "Undefined"


Undefined = UndefinedType()  # UndefinedType object, used like NoneType's None


class RobotHandlerInterface(ABC):
    @abstractmethod
    def set_variables(self, varmap: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def replace_variables(self, s: str) -> Any:
        pass

    @abstractmethod
    def get_variable_value(self, name: str) -> Any:
        pass

    @abstractmethod
    def get_metadata(
        self, suite: running.TestSuite, name: str, required: bool
    ) -> str | None:
        pass


class RobotHandler(RobotHandlerInterface):
    def __init__(self):
        self._builtin = BuiltIn()

    def set_variables(self, varmap: dict[str, Any]):
        for key, value in varmap.items():
            try:
                self._builtin.set_suite_variable(f"${key}", value)
            except Exception as e:
                raise VariableError(
                    key, f"Failed to set value '{value}'", str(e)
                )

    def replace_variables(self, s: str) -> Any:
        """Replace all variables in the given string.

        If the string consists of a single variable, it will be returned
        directly with its original type or Undefined if it is a valid variable
        name that has no value.

        If there is more than just the variable name in the string, the return
        value will always be a string and only non-escaped robot variables
        (`${varname}` syntax) are resolved in that string.
        """
        try:
            val = self.get_variable_value(s)
            return val
        except Exception:
            pass

        try:
            val = self._builtin.replace_variables(s)
            return val
        except Exception as e:
            raise VariableError(s, str(e))

    def get_variable_value(self, name: str) -> Any:
        """Return value of variable. Returns Undefined if variable is unset.
        Raises VariableError if var is not a valid variable."""
        try:
            val = self._builtin.get_variable_value(name, Undefined)
        except Exception as e:
            raise VariableError(name, str(e))  # var is invalid

        if val is Undefined:
            # var is either unset or edge case of invalid
            try:
                self._builtin.replace_variables(name)
            except Exception as e:
                raise VariableError(name, str(e))  # var is invalid

        return val

    def get_metadata(
        self, suite: running.TestSuite, name: str, required: bool
    ) -> str | None:
        try:
            return suite.metadata[name]
        except KeyError:
            if required:
                raise MetadataError(name, "Missing required metadata")
            else:
                return None
