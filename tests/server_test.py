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
def test_lock_dependencies() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    result = manager._resolve_and_assign_dependencies(
        None,
        {"dev1", "dev2"},
        {},
    )

    # Assert
    assert result["status"] == "locked"
    session = result["session"]
    assert session.devices == {"dev1", "dev2"}
    assert manager.active_locks == {
        "dev1": session.uuid,
        "dev2": session.uuid,
    }


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
    assert manager.active_locks == {"dev1": first["session"].uuid,}


def test_extend_existing_session() -> None:
    # Arrange
    manager = DepLockManager()

    # Act
    first = manager._resolve_and_assign_dependencies(
        None,
        {"dev1"},
        {},
    )

    session = first["session"].uuid

    result = manager._resolve_and_assign_dependencies(
        first["session"].uuid,
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
