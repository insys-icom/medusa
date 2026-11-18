import pytest

from medusa.dependency_resolver import resolve_dynamic_dependencies


@pytest.mark.parametrize(
    "dependencies, expected",
    [
        ({}, {}),
        ({"$DUT": {"device1"}}, {"$DUT": "device1"}),
        (
            {"$DUT1": {"one"}, "$DUT2": {"two"}},
            {"$DUT1": "one", "$DUT2": "two"},
        ),
        (
            {"$DUT1": {"one", "two"}, "$DUT2": {"one"}},
            {"$DUT1": "two", "$DUT2": "one"},
        ),
        ({"$DUT1": {"one"}, "$DUT2": {"one"}}, None),
        ({"$DUT1": set()}, None),
    ],
)
def test_resolve_dynamic_dependencies(
    dependencies: dict[str, set[str]],
    expected: dict[str, str] | None,
) -> None:
    output = resolve_dynamic_dependencies(dependencies) # Act
    assert output == expected   # Assert
