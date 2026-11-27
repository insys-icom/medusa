import pytest

from datetime import UTC, datetime, timedelta
from uuid import UUID

from medusa.server import DepLockManager, Session


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
