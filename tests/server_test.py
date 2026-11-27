from unittest import result

import pytest
import asyncio

from aiohttp import web

from robot import result
from datetime import UTC, datetime, timedelta
from uuid import UUID

from medusa.server import DepLockManager, LockServer, start_server, Session, normalize_server_url


# Tests for Session
def test_create_session() -> None:
    # Arrange
    devices = {"dev1", "dev2"}

    # Act
    session = Session._create_session(devices)

    # Assert
    assert UUID(session.uuid)
    assert session.devices == devices
    assert session.since.tzinfo == UTC
    assert session.last_alive == session.since


# Tests for DepLockManager
@pytest.mark.parametrize(
        "static_deps, dynamic_deps, expected_deps",
        [
            ({"dev1", "dev2"}, {}, {"dev1", "dev2"}),
            (set(), {"$DUT": {"dev1", "dev2"}}, None),
            ({"dev1"}, {"$DUT": {"dev2", "dev3"}},None)
        ]
    )

def test_lock_dependencies(
    static_deps: set[str],
    dynamic_deps: dict[str, set[str]],
    expected_deps: set[str] | None,
) -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    result = manager._resolve_and_assign_dependencies(
        None,
        static_deps,
        dynamic_deps,
    )

    session = result["session"]
    assigned = result["assigned_dynamic"]

    # Assert
    assert result["status"] == "locked"
    assert session.devices == static_deps | set(assigned.values())

    if expected_deps is not None:
        assert session.devices == expected_deps

    assert manager.active_locks == {
        device: session.uuid for device in session.devices
    }


def test_lock_unavailable_dynamic_dependencies() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    result = manager._resolve_and_assign_dependencies(
        None,
        set(),
        {"$DUT": {"dev1"}, "$HOST": {"dev1"}},
    )

    # Assert
    assert result["status"] == "unavailable"
    assert manager.sessions == {}
    assert manager.active_locks == {}


def test_lock_busy_static_dependencies() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    first = manager._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )
    result = manager._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )

    # Assert
    assert result["status"] == "unavailable"
    assert len(manager.sessions) == 1
    assert manager.active_locks == {"dev1": first["session"].uuid}


def test_extend_existing_session() -> None:
    # Arrange
    manager = DepLockManager()

    first = manager._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )
    session = first["session"].uuid

    # Act
    result = manager._resolve_and_assign_dependencies(
        session,
        {"dev2"},
        {},
    )

    # Assert
    assert result["status"] == "locked"
    assert result["session"].devices == {"dev1", "dev2"}
    assert len(manager.sessions) == 1
    assert manager.active_locks == {
        "dev1": session,
        "dev2": session,
    }


def test_lock_unknown_session() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    result = manager._resolve_and_assign_dependencies(
        "unknown",
        {"dev1"},
        {},
    )

    # Assert
    assert result["status"] == "error"
    assert result["message"] == "Unknown session."
    assert manager.sessions == {}
    assert manager.active_locks == {}


def test_release_selected_devices() -> None:
    # Arrange
    manager = DepLockManager()

    locked = manager._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )
    session = locked["session"]
    session_id = session.uuid

    # Act
    result = manager._release_session(session_id, {"dev1"})

    # Assert
    assert result == {
        "status": "freed",
        "session_id": session_id,
        "devices": ["dev1"],
    }
    assert manager.sessions[session_id].devices == {"dev2"}
    assert manager.active_locks == {"dev2": session_id}


def test_release_session() -> None:
    # Arrange
    manager = DepLockManager()

    locked = manager._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )
    session = locked["session"]
    session_id = session.uuid

    # Act
    result = manager._release_session(session_id)

    # Assert
    assert result == {
        "status": "closed",
        "session_id": session_id,
        "devices": ["dev1", "dev2"],
    }
    assert manager.sessions == {}
    assert manager.active_locks == {}


def test_release_unknown_session() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    result = manager._release_session("unknown")

    # Assert
    assert result == {
        "status": "error",
        "message": "Unknown session.",
    }


def test_release_device_not_owned_by_session() -> None:
    # Arrange
    manager = DepLockManager()

    locked = manager._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )
    session = locked["session"]
    session_id = session.uuid

    # Act
    result = manager._release_session(session_id, {"dev2"})

    # Assert
    assert result == {
        "status": "rejected",
        "message": "One or more devices do not belong to this session.",
    }
    assert manager.sessions[session_id].devices == {"dev1"}
    assert manager.active_locks == {"dev1": session_id}


def test_get_expired_sessions() -> None:
    # Arrange
    manager = DepLockManager()

    expired = Session._create_session({"dev1"})
    expired.last_alive = datetime.now(UTC) - timedelta(seconds=60)
    active = Session._create_session({"dev2"})

    manager.sessions[expired.uuid] = expired
    manager.sessions[active.uuid] = active

    # Act
    result = manager._get_expired_sessions(timeout_seconds=30)

    # Assert
    assert result == [expired]


def test_reclaim_session() -> None:
    # Arrange
    manager = DepLockManager()

    session = Session._create_session({"dev1", "dev2"})
    manager.sessions[session.uuid] = session
    manager.active_locks = {"dev1": session.uuid, "dev2": session.uuid}

    # Act
    manager._reclaim_session(session)

    # Assert
    assert session.uuid in manager.sessions
    assert session.devices == {"dev1", "dev2"}
    assert manager.active_locks == {}


# Tests for LockServer
class FakeRequest:
    def __init__(self, data=None, error=None) -> None:
        self.data = data
        self.error = error

    async def json(self):
        if self.error:
            raise self.error
        return self.data


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"deps_static": ["dev1"]},
        {"deps_dynamic": {"$DUT": ["dev2", "dev3"]}},
        {
            "session_id": "1234-5678-9014",
            "deps_static": ["dev1"],
            "deps_dynamic": {"$DUT": ["dev2", "dev3"]},
        },
    ],
)
def test_parse_lock_request_valid(data) -> None:
    # Arrange
    server = LockServer()

    # Act
    result = asyncio.run(server._parse_lock_request(FakeRequest(data=data)))

    # Assert
    assert result == (
        data.get("session_id"),
        data.get("deps_static", []),
        data.get("deps_dynamic", {}),
    )

@pytest.mark.parametrize(
    "data, expected_message",
    [
        (None, "Invalid request body"),
        ([], "Invalid request body"),
        ({"session_id": ""}, "Invalid session_id"),
        ({"session_id": 123}, "Invalid session_id"),
        ({"deps_static": "dev1"}, "Missing / invalid request fields"),
        ({"deps_dynamic": []}, "Missing / invalid request fields"),
        ({"deps_static": [1]}, "Invalid dependency names or options"),
        ({"deps_static": [""]}, "Invalid dependency names or options"),
        ({"deps_dynamic": {"": ["dev1"]}}, "Invalid dependency names or options"),
        ({"deps_dynamic": {"$DUT": "dev1"}}, "Invalid dependency names or options"),
        ({"deps_dynamic": {"$DUT": [1]}}, "Invalid dependency names or options"),
        ({"deps_dynamic": {"$DUT": [""]}}, "Invalid dependency names or options"),
    ],
)
def test_parse_lock_request_invalid(data, expected_message) -> None:
    # Arrange
    server = LockServer()

    # Act
    result = asyncio.run(server._parse_lock_request(FakeRequest(data=data)))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert expected_message in result.text

def test_parse_lock_request_malformed_json() -> None:
    # Arrange
    server = LockServer()

    # Act
    result = asyncio.run(server._parse_lock_request(FakeRequest(error=ValueError("Invalid JSON"))))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert "Malformed JSON payload" in result.text


def test_prepare_lock_dependencies() -> None:
    # Arrange
    server = LockServer()

    # Act
    result = server._prepare_lock_dependencies(
        ["dev1", "dev2"],
        {
            "$DUT": {"dev2", "dev3"},
            "$PORT": {"dev4", "dev5"}
        },
    )

    # Assert
    assert result == (
        {"dev1", "dev2"},
        {
            "$DUT": {"dev3"},
            "$PORT": {"dev4", "dev5"},
        },
    )

def test_prepare_lock_dependencies_duplicate_static() -> None:
    # Arrange
    server = LockServer()

    # Act
    result = server._prepare_lock_dependencies(["dev1", "dev1"], {})

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert "Duplicate static dependencies" in result.text

def test_prepare_lock_dependencies_empty() -> None:
    # Arrange
    server = LockServer()

    # Act
    result = server._prepare_lock_dependencies([], {})

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert "No dependencies supplied" in result.text


def test_parse_alive_request_valid() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({"session_id": "1234-5678-9014"})

    # Act
    result = asyncio.run(server._parse_alive_request(request))

    # Assert
    assert result == "1234-5678-9014"

@pytest.mark.parametrize(
    "data",
    [
        {},
        {"session_id": ""},
        {"session_id": None},
        {"session_id": 1234},
    ],
)
def test_parse_alive_request_invalid_session_id(data) -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(data)

    # Act
    result = asyncio.run(server._parse_alive_request(request))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400

def test_parse_alive_request_invalid_json() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(error=ValueError("Invalid JSON"))

    # Act
    result = asyncio.run(server._parse_alive_request(request))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert "Malformed JSON payload" in result.text


def test_parse_free_request_without_devices() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({"session_id": "1234-5678-9014"})

    # Act
    result = asyncio.run(server._parse_free_request(request))

    # Assert
    assert result == ("1234-5678-9014", None)

def test_parse_free_request_with_devices() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(
        {
            "session_id": "1234-5678-9014",
            "devices": ["dev1", "dev2"],
        }
    )

    # Act
    result = asyncio.run(server._parse_free_request(request))

    # Assert
    assert result == ("1234-5678-9014", {"dev1", "dev2"})

@pytest.mark.parametrize(
    "data",
    [
        {},
        {"session_id": ""},
        {"session_id": 1234},
        {"session_id": "1234", "devices": "dev1"},
        {"session_id": "1234", "devices": ["dev1", ""]},
        {"session_id": "1234", "devices": ["dev1", 123]},
        {"session_id": "1234", "devices": ["dev1", "dev1"]},
    ],
)
def test_parse_free_request_invalid(data) -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(data)

    # Act
    result = asyncio.run(server._parse_free_request(request))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400

def test_parse_free_request_invalid_json() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(error=ValueError("Invalid JSON"))

    # Act
    result = asyncio.run(server._parse_free_request(request))

    # Assert
    assert isinstance(result, web.Response)
    assert result.status == 400
    assert "Malformed JSON payload" in result.text


def test_handle_lock_success() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({"deps_static": ["dev1"], "deps_dynamic": {}})

    # Act
    response = asyncio.run(server._handle_lock(request))

    # Assert
    assert response.status == 200
    assert '"status": "locked"' in response.text
    assert '"dev1"' in response.text

def test_handle_lock_unavailable() -> None:
    # Arrange
    server = LockServer()

    server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )

    request = FakeRequest({"deps_static": ["dev1"], "deps_dynamic": {}})

    # Act
    response = asyncio.run(server._handle_lock(request))

    # Assert
    assert response.status == 409
    assert '"status": "unavailable"' in response.text

def test_handle_lock_unknown_session() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest(
        {
            "session_id": "unknown-session",
            "deps_static": ["dev1"],
            "deps_dynamic": {},
        }
    )

    # Act
    response = asyncio.run(server._handle_lock(request))

    # Assert
    assert response.status == 404
    assert '"status": "error"' in response.text


def test_handle_alive_success() -> None:
    # Arrange
    server = LockServer()
    lock_data = server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )
    session = lock_data["session"]
    previous_last_alive = session.last_alive

    request = FakeRequest({"session_id": session.uuid})

    # Act
    response = asyncio.run(server._handle_alive(request))

    # Assert
    assert response.status == 200
    assert '"status": "alive"' in response.text
    assert session.last_alive >= previous_last_alive

def test_handle_alive_unknown_session() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({"session_id": "unknown-session"})

    # Act
    response = asyncio.run(server._handle_alive(request))

    # Assert
    assert response.status == 404
    assert '"status": "error"' in response.text
    assert "Unknown session" in response.text

def test_handle_alive_invalid_request() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({})

    # Act
    response = asyncio.run(server._handle_alive(request))

    # Assert
    assert isinstance(response, web.Response)
    assert response.status == 400


def test_handle_free_all_devices() -> None:
    # Arrange
    server = LockServer()
    lock_data = server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )
    session = lock_data["session"]

    request = FakeRequest({"session_id": session.uuid})

    # Act
    response = asyncio.run(server._handle_free(request))

    # Assert
    assert response.status == 200
    assert '"status": "closed"' in response.text
    assert session.uuid not in server.pool.sessions
    assert server.pool.active_locks == {}

def test_handle_free_selected_devices() -> None:
    # Arrange
    server = LockServer()
    lock_data = server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )
    session = lock_data["session"]

    request = FakeRequest(
        {
            "session_id": session.uuid,
            "devices": ["dev1"],
        }
    )

    # Act
    response = asyncio.run(server._handle_free(request))

    # Assert
    assert response.status == 200
    assert '"status": "freed"' in response.text
    assert session.uuid in server.pool.sessions
    assert session.devices == {"dev2"}
    assert server.pool.active_locks == {"dev2": session.uuid}

def test_handle_free_unknown_session() -> None:
    # Arrange
    server = LockServer()
    request = FakeRequest({"session_id": "unknown-session"})

    # Act
    response = asyncio.run(server._handle_free(request))

    # Assert
    assert response.status == 404
    assert '"status": "error"' in response.text

def test_handle_free_device_not_owned() -> None:
    # Arrange
    server = LockServer()
    lock_data = server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )
    session = lock_data["session"]

    request = FakeRequest(
        {
            "session_id": session.uuid,
            "devices": ["dev2"],
        }
    )

    # Act
    response = asyncio.run(server._handle_free(request))

    # Assert
    assert response.status == 409
    assert '"status": "rejected"' in response.text
    assert session.devices == {"dev1"}
    assert server.pool.active_locks == {"dev1": session.uuid}


def test_handle_status_empty() -> None:
    # Arrange
    server = LockServer()

    # Act
    response = asyncio.run(server._handle_status(None))

    # Assert
    assert response.status == 200
    assert response.text == '{"sessions": []}'

def test_handle_status_active_session() -> None:
    # Arrange
    server = LockServer()
    lock_data = server.pool._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )
    session = lock_data["session"]

    # Act
    response = asyncio.run(server._handle_status(None))

    # Assert
    assert response.status == 200
    assert session.uuid in response.text
    assert '"devices": ["dev1", "dev2"]' in response.text
    assert server._format_datetime(session.since) in response.text
    assert server._format_datetime(session.last_alive) in response.text


def test_server_startup_and_cleanup() -> None:
    # Arrange
    server = LockServer()
    app = {}

    async def run_lifecycle() -> None:
        # Act: Startup
        await server._on_startup(app)

        task = app["cleanup_task"]

        # Assert: Cleanup-Task wurde erstellt
        assert isinstance(task, asyncio.Task)

        # Act: Cleanup
        await server._on_cleanup(app)

        # Assert: Task wurde beendet
        assert task.cancelled()

    asyncio.run(run_lifecycle())


def test_start_server(monkeypatch) -> None:
    # Arrange
    captured = {}

    def fake_run(self) -> None:
        captured["host"] = self.host
        captured["port"] = self.port

    monkeypatch.setattr(LockServer, "run", fake_run)

    # Act
    start_server("0.0.0.0", "8082")

    # Assert
    assert captured == {
        "host": "0.0.0.0",
        "port": 8082,
    }

@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_start_server_invalid_port(port: str) -> None:
    with pytest.raises(ValueError):
        start_server(port=port)


@pytest.mark.parametrize(
    "server, expected",
    [
        ("localhost", "http://localhost:8081"),
        ("127.0.0.1", "http://127.0.0.1:8081"),
        ("localhost:8082", "http://localhost:8082"),
        ("http://localhost:8082", "http://localhost:8082"),
        ("http://localhost:8081/", "http://localhost:8081"),
    ],
)
def test_normalize_server_url(server: str, expected: str) -> None:
    # Act
    server_url = normalize_server_url(server)

    # Assert
    assert server_url == expected
