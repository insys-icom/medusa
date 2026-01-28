import re
from typing import Any

import pytest
from robot import running  # type: ignore

from medusa.robot_handler import RobotHandlerInterface, Undefined
from medusa.robot_reader import RobotSuiteReader
from medusa.suite import DynDep
from medusa.utils import Timeout

MOCK_SUITE = object()
VARIABLES: dict[str, Any] = {
    # variable: value
    "${scalar}": "val",
    "${int_var}": 42,
    "${list_var}": ["val1", "val2", "val3"],
    "${nested_list}": [["val1.1", "val1.2"], ["val2.1", "val2.2"]],
    "${dict_var}": {"val1.1": "val1.2", "val2.1": "val2.2"},
    "${target1}": None,
    "${target2}": None,
}


class MockRobotHandler(RobotHandlerInterface):
    """This handler does not accurately emulate variable replacements as that
    would be too complex. Variables are assumed to have the form `${var_name}`
    and are simply looked up in the `VARIABLES` dictionary.
    """

    def __init__(self) -> None:
        self.metadata: dict[str, str] = dict()

    def set_variables(self, varmap: dict[str, Any]) -> None:
        assert False, "Not implemented"

    def replace_variables(self, s: str) -> Any:
        try:
            val = self.get_variable_value(s)
            return val
        except Exception:
            pass

        def replace_var(m: re.Match) -> Any:
            val = self.get_variable_value(m.group(0))
            assert val is not Undefined
            return val

        return re.sub(r"\$\{[a-z0-9_]+\}", replace_var, s)

    def get_variable_value(self, name: str) -> Any:
        if re.fullmatch(r"\$\{[a-z0-9_]+\}", name):
            if name in VARIABLES:
                res = VARIABLES[name]
                return res
            else:
                return Undefined
        else:
            raise AssertionError("Not a valid variable name")

    def get_metadata(
        self, suite: running.TestSuite, name: str, required: bool
    ) -> str | None:
        assert suite == MOCK_SUITE, (
            "Suite parameter was not passed as expected"
        )
        if required:
            return self.metadata[name]
        else:
            return self.metadata.get(name)


@pytest.mark.parametrize(
    "input, expected",
    [
        ("some${scalar}", "someval"),
        ("${int_var}", "42"),
    ],
)
def test__get_stage(input: str, expected: str) -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)

    mock_handler.metadata["medusa:stage"] = input

    # Act
    output = suite_reader._get_stage(MOCK_SUITE)

    # Assert
    assert output == expected


@pytest.mark.parametrize(
    "input,expected_static,expected_dynamic",
    [
        ("one", ["one"], {}),
        ("one    two", ["one", "two"], {}),
        ("one    two", ["one", "two"], {}),
        ("${scalar}", ["val"], {}),
        ("${int_var}", ["42"], {}),
        ("partial${scalar}", ["partialval"], {}),
        ("one  ${scalar}", ["one", "val"], {}),
        ("${list_var}", ["val1", "val2", "val3"], {}),
        (
            "one    partial${scalar}    ${scalar}    ${list_var}",
            ["one", "partialval", "val", "val1", "val2", "val3"],
            {},
        ),
        (
            "ANY ${target1} IN ${list_var}",
            [],
            {"${target1}": DynDep({"val1", "val2", "val3"})},
        ),
        (
            "ANY ${target1} IN ${list_var}    ANY ${target2} IN ${list_var}",
            [],
            {
                "${target1}": DynDep({"val1", "val2", "val3"}),
                "${target2}": DynDep({"val1", "val2", "val3"}),
            },
        ),
        (
            "one    ANY ${target1} IN ${list_var}  ${scalar}",
            ["one", "val"],
            {"${target1}": DynDep({"val1", "val2", "val3"})},
        ),
    ],
)
def test__get_deps(
    input: str, expected_static: set[str], expected_dynamic: dict[str, DynDep]
) -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)

    mock_handler.metadata["medusa:deps"] = input

    # Act
    static, dynamic = suite_reader._get_deps(MOCK_SUITE)

    # Assert
    assert static == frozenset(expected_static)
    assert dynamic == expected_dynamic


@pytest.mark.parametrize(
    "input",
    [
        "ANY ${nonexistent} IN ${list_var}",  # Target has to exist
        "ANY ${scalar} IN ${list_var}",  # Target has to have value None
        "ANY ${target1} IN ${nonexistent}",  # Source has to exist
        "ANY ${target1} IN ${scalar}",  # Source has to be a list
    ],
)
def test__get_deps_dynamic_negative(input: str) -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)
    mock_handler.metadata["medusa:deps"] = input

    # Act
    try:
        static, dynamic = suite_reader._get_deps(MOCK_SUITE)
    except Exception:
        return  # Failed as expected

    # Assert
    assert False, "No exception was raised!"


@pytest.mark.parametrize(
    "input,expected",
    [
        ("123", Timeout(123)),
        ("123,45", Timeout(123, 45)),
        ("123,45,6", Timeout(123, 45, 6)),
    ],
)
def test__get_timeout(input: str, expected: Timeout) -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)
    mock_handler.metadata["medusa:timeout"] = input

    # Act
    output = suite_reader._get_timeout(MOCK_SUITE)

    # Assert
    assert output == expected


def test__get_timeout_absent() -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)

    # Act
    ret = suite_reader._get_timeout(MOCK_SUITE)

    # Assert
    assert ret is None


@pytest.mark.parametrize(
    "input,expected",
    [
        (
            "${target1}    IN    ${list_var}",
            [
                {"target1": "val1"},
                {"target1": "val2"},
                {"target1": "val3"},
            ],
        ),
        (
            "${target1}  in  ${list_var}",
            [
                {"target1": "val1"},
                {"target1": "val2"},
                {"target1": "val3"},
            ],
        ),
        (
            "${target1}    ${target2}    IN    ${nested_list}",
            [
                {"target1": "val1.1", "target2": "val1.2"},
                {"target1": "val2.1", "target2": "val2.2"},
            ],
        ),
        (
            "${target1}    ${target2}    IN    ${dict_var}",
            [
                {"target1": "val1.1", "target2": "val1.2"},
                {"target1": "val2.1", "target2": "val2.2"},
            ],
        ),
    ],
)
def test__get_for(input: str, expected: list[dict[str, Any]]) -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)
    mock_handler.metadata["medusa:for"] = input

    # Act
    output = suite_reader._get_for(MOCK_SUITE)

    # Assert
    assert output == expected


def test__get_for_absent() -> None:
    # Arrange
    mock_handler = MockRobotHandler()
    suite_reader = RobotSuiteReader(mock_handler)

    # Act
    ret = suite_reader._get_for(MOCK_SUITE)

    # Assert
    assert ret is None
