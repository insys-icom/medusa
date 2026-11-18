import pytest

from medusa.cli import _parse_dependencies


@pytest.mark.parametrize(
    "dependencies, expected",
    [
        ([], ([], {})),
        (["dev1"], (["dev1"], {})),
        (["dev1", "dev2"], (["dev1", "dev2"], {})),
        (["dynamic:$DUT=a"], ([], {"$DUT": ["a"]})),
        (["dynamic:$DUT=a,b,c"], ([], {"$DUT": ["a", "b", "c"]})),
        (
            ["dev1", "dynamic:$DUT=a,b", "dev2"],
            (["dev1", "dev2"], {"$DUT": ["a", "b"]}),
        ),
        (
            ["dynamic:$DUT1=a,b", "dynamic:$DUT2=c"],
            ([], {"$DUT1": ["a", "b"], "$DUT2": ["c"]}),
        ),
    ],
)
def test_parse_dependencies(
    dependencies: list[str],
    expected: tuple[list[str], dict[str, list[str]]],
) -> None:
    output = _parse_dependencies(dependencies)  # Act
    assert output == expected  # Assert


# Edge cases for parse_dependencies
@pytest.mark.parametrize(
    "dependencies, expected",
    [
        # Only the first "=" seperates name and values
        (["dynamic:$DUT=a=b"], ([], {"$DUT": ["a=b"]})),
        # Empty value list yields one empty string
        (["dynamic:$DUT="], ([], {"$DUT": [""]})),
        # Empty entries and whitespace are kept as-is
        (["dynamic:$DUT=a,,b"], ([], {"$DUT": ["a", "", "b"]})),
        (["dynamic:$DUT=a, b"], ([], {"$DUT": ["a", " b"]})),
        # Duplicate name: the last one wins
        (["dynamic:$DUT=a", "dynamic:$DUT=b"], ([], {"$DUT": ["b"]})),
        # Prefix is case-sensitive, so this is a static dependency
        (["Dynamic:$DUT=a"], (["Dynamic:$DUT=a"], {})),
    ],
)
def test_parse_dependencies_edge_cases(
    dependencies: list[str],
    expected: tuple[list[str], dict[str, list[str]]],
) -> None:
    output = _parse_dependencies(dependencies)  # Act
    assert output == expected  # Assert


def test_parse_dependencies_missing_equals_raises() -> None:
    with pytest.raises(ValueError):
        _parse_dependencies(
            ["dynamic:$DUT"]
        )  # Missing "=" should raise ValueError
