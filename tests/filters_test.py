from pathlib import Path

import pytest

from medusa.filters import Filters
from medusa.suite import DynDep, Suite


@pytest.fixture
def static_suite() -> Suite:
    return Suite(
        full_name="Static",
        source=Path("foo.robot"),
        stage="Test",
        deps_static=frozenset(["one", "two", "three"]),
        deps_dynamic=dict(),
        timeout=None,
        for_vars=None,
    )


@pytest.mark.parametrize(
    "input,expected",
    [
        ([], True),
        ## STAGE
        # Simple options
        (["stage=Test"], True),
        (["stage=!Test"], False),
        (["stage=Other"], False),
        (["stage=!Other"], True),
        # Multiple options split by comma
        (["stage=Test,!Other"], True),
        (["stage=!Other,Test"], True),
        (["stage=!Test,!Other"], False),
        (["stage=!Irrelevant,!Other"], True),
        # Multiple options in separate args
        (["stage=Test", "stage=!Other"], True),
        (["stage=!Other", "stage=Test"], True),
        (["stage=!Test", "stage=!Other"], False),
        (["stage=!Irrelevant", "stage=!Other"], True),
        ## DEPS
        # Simple options
        (["deps=one"], False),
        (["deps=!one"], False),
        (["deps=Other"], False),
        (["deps=!Other"], True),
        (["deps~one"], True),
        (["deps~!one"], False),
        (["deps~Other"], False),
        (["deps~!Other"], True),
        # Multiple options split by comma
        (["deps=one,three,two"], True),
        (["deps=!one,three,two"], False),
        (["deps=Other,Irrelevant"], False),
        (["deps=!Other,!Irrelevant"], True),
        (["deps~Other,two"], True),
        (["deps~!one,two"], False),
        (["deps~Other,Irrelevant"], False),
        (["deps~!Other,!Irrelevant"], True),
        # Multiple options in separate args
        (["deps=one", "deps=three", "deps=two"], True),
        (["deps=!one", "deps=three", "deps=two"], False),
        (["deps=Other", "deps=Irrelevant"], False),
        (["deps=!Other", "deps=!Irrelevant"], True),
        (["deps~Other", "deps~two"], True),
        (["deps~!one", "deps~two"], False),
        (["deps~Other", "deps~Irrelevant"], False),
        (["deps~!Other", "deps~!Irrelevant"], True),
    ],
)
def test_filter_static(
    static_suite: Suite, input: list[str], expected: bool
) -> None:
    flt = Filters(input)  # Arrange
    output = flt.match(static_suite)  # Act
    assert output == expected  # Assert


@pytest.fixture
def dynamic_suite() -> Suite:
    return Suite(
        full_name="Static",
        source=Path("foo.robot"),
        stage="Test",
        deps_static=frozenset(["one"]),
        deps_dynamic={
            "first": DynDep({"one", "two"}),
            "second": DynDep({"two", "three", "four"}),
        },
        timeout=None,
        for_vars=None,
    )


@pytest.mark.parametrize(
    "input,expected",
    [
        ([], True),
        # Include only
        (["deps=one,two,three"], True),
        (["deps=one,two,four"], True),
        (["deps=one,two,five"], False),
        (["deps=one,two"], False),
        (["deps=one,three"], False),
        # Include any (should only check against static deps)
        (["deps~one"], True),
        (["deps~one,two"], True),
        (["deps~one,!two"], False),
        (["deps~two"], False),
    ],
)
def test_filter_dynamic(
    dynamic_suite: Suite, input: list[str], expected: bool
) -> None:
    flt = Filters(input)  # Arrange
    output = flt.match(dynamic_suite)  # Act
    assert output == expected  # Assert
