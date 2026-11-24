import pytest
import requests

from medusa.cli import _keepalive, _parse_dependencies, _send_lock, free


# Tests for _parse_dependencies
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
def test_parse_dependencies_success(
    dependencies: list[str],
    expected: tuple[list[str], dict[str, list[str]]],
) -> None:
    output = _parse_dependencies(dependencies)  # Act
    assert output == expected  # Assert


# Edge cases for _parse_dependencies
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


# Tests for _send_lock
def test_send_lock_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    class Response:
        status_code = 200

        def json(self):
            return {
                "session_id": "1234-5678-9014",
                "devices": ["dev1"],
                "deps_assigned": {"$DUT": "dev2"},
            }

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: Response(),
    )

    # Act
    result = _send_lock(["dev1"], {"$DUT": ["dev2"]}, "http://localhost:8080")

    # Assert
    assert result == "1234-5678-9014"
    assert "Locked dependencies:" in capsys.readouterr().out


def test_send_lock_rejected(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    class Response:
        status_code = 409
        text = "Dependencies busy"

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: Response(),
    )

    # Act
    result = _send_lock(["dev1"], {}, "http://localhost:8080")

    # Assert
    assert result is None


def test_send_lock_connection_error(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("Server unavailable")

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        raise_connection_error,
    )

    # Act
    result = _send_lock(["dev1"], {}, "http://localhost:8080")

    # Assert
    assert result is None


# Tests for free
def test_free_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # Arrange
    class Response:
        status_code = 200

        def json(self):
            return {"devices": ["dev1", "dev2"]}

    def fake_post(url, json, timeout):
        return Response()

    # equivalent to:
    # lambda *args, **kwargs: Response()

    monkeypatch.setattr("medusa.cli.requests.post", fake_post)

    # Act
    result = free("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert result is True
    assert "Freed dependencies:" in capsys.readouterr().out


def test_free_rejected(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    class Response:
        status_code = 404
        text = "Unknown session id"

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: Response(),
    )

    # Act
    result = free("1234-5678", "http://localhost:8080")

    # Assert
    assert result is False


def test_free_connection_error(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("Server unavailable")

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        raise_connection_error,
    )

    # Act
    result = free("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert result is False


# Tests for _keepalive
def test_keepalive_connection_error(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("Server unavailable")

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        raise_connection_error,
    )
    monkeypatch.setattr("medusa.cli.time.sleep", lambda seconds: None)

    # Act
    _keepalive("1234-5678-9014", "http://localhost:8080")

    # Assert
    # Functions returns after the connection error


def test_keepalive_expired(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    responses = iter(
        [
            type("Response", (), {"status_code": 200})(),
            type("Response", (), {"status_code": 404})(),
        ]
    )

    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr("medusa.cli.requests.post", fake_post)
    monkeypatch.setattr("medusa.cli.time.sleep", lambda seconds: None)

    # Act
    _keepalive("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert calls == 2
