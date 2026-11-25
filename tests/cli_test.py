import pytest
import requests

from medusa.cli import _keepalive, _parse_dependencies, _send_lock, free


class FakeResponse:
    def __init__(
        self, status_code: int, data: dict | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._data


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
        # Only the first "=" separates name and values
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
    request_data: dict[str, object] = {}

    def fake_post(url: str, json: dict, timeout: int) -> FakeResponse:
        request_data["url"] = url
        request_data["json"] = json
        request_data["timeout"] = timeout
        return FakeResponse(
            200,
            data={
                "session_id": "1234-5678-9014",
                "devices": ["dev1"],
                "deps_assigned": {"$DUT": "dev2"},
            },
        )

    monkeypatch.setattr("medusa.cli.requests.post", fake_post)

    # Act
    result = _send_lock(
        ["dev1"], {"$DUT": ["dev3", "dev2"]}, "http://localhost:8080"
    )

    # Assert
    assert result == "1234-5678-9014"
    assert request_data == {
        "url": "http://localhost:8080/lock",
        "json": {
            "deps_static": ["dev1"],
            "deps_dynamic": {"$DUT": ["dev2", "dev3"]},
        },
        "timeout": 5,
    }
    assert "Locked dependencies:" in capsys.readouterr().out


def test_send_lock_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: FakeResponse(409, text="Dependencies busy"),
    )

    # Act
    result = _send_lock(["dev1"], {}, "http://localhost:8080")

    # Assert
    assert result is None


def test_send_lock_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    def raise_connection_error(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("Server unavailable")

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        raise_connection_error,
    )

    # Act
    result = _send_lock(["dev1"], {}, "http://localhost:8080")

    # Assert
    assert result is None


@pytest.mark.parametrize(
    "data", [{}, {"session_id": ""}, {"session_id": None}, {"session_id": 123}]
)
def test_send_lock_invalid_session_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    data: dict,
) -> None:
    # Arrange
    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: FakeResponse(200, data=data),
    )

    # Act
    result = _send_lock(["dev1"], {}, "http://localhost:8080")

    # Assert
    assert result is None
    assert capsys.readouterr().out == ""
    assert "Server did not return a session ID" in caplog.text


# Tests for free
def test_free_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    request_data: dict[str, object] = {}

    def fake_post(url: str, json: dict, timeout: int) -> FakeResponse:
        request_data["url"] = url
        request_data["json"] = json
        request_data["timeout"] = timeout
        return FakeResponse(
            200,
            data={"session_id": "1234-5678-9014", "devices": ["dev1", "dev2"]},
        )

    monkeypatch.setattr("medusa.cli.requests.post", fake_post)

    # Act
    result = free("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert result is True
    assert request_data == {
        "url": "http://localhost:8080/free",
        "json": {
            "session_id": "1234-5678-9014",
        },
        "timeout": 5,
    }
    assert "Freed dependencies:" in capsys.readouterr().out


def test_free_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: FakeResponse(404, text="Unknown session id"),
    )

    # Act
    result = free("1234-5678", "http://localhost:8080")

    # Assert
    assert result is False


def test_free_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    def raise_connection_error(*args: object, **kwargs: object) -> None:
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
def test_keepalive_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    calls = 0

    def raise_connection_error(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("_keepalive did not stop after error")
        raise requests.ConnectionError("Server unavailable")

    monkeypatch.setattr(
        "medusa.cli.requests.post",
        raise_connection_error,
    )
    monkeypatch.setattr("medusa.cli.time.sleep", lambda seconds: None)

    # Act
    _keepalive("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert calls == 1


def test_keepalive_rejected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    monkeypatch.setattr(
        "medusa.cli.requests.post",
        lambda *args, **kwargs: FakeResponse(500, text="Internal error"),
    )
    monkeypatch.setattr("medusa.cli.time.sleep", lambda seconds: None)

    # Act
    _keepalive("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert "Keepalive rejected: 500 Internal error" in caplog.text


def test_keepalive_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    responses = iter([FakeResponse(200), FakeResponse(404)])

    calls = 0

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr("medusa.cli.requests.post", fake_post)
    monkeypatch.setattr("medusa.cli.time.sleep", lambda seconds: None)

    # Act
    _keepalive("1234-5678-9014", "http://localhost:8080")

    # Assert
    assert calls == 2
