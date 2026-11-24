import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import requests

from .dependency_resolver import resolve_dynamic_dependencies
from .utils import LOGGER

if TYPE_CHECKING:
    from .data import Stage
    from .settings import Settings
    from .suite import Suite


class DepManager(ABC):
    def __init__(self, stage: "Stage") -> None:
        """Initialize the dependency manager with the given stage."""

        all_deps: set[str] = set(stage.deps_static_cnt).union(
            stage.deps_dynamic_cnt
        )
        self.all = frozenset(all_deps)

    @abstractmethod
    def try_lock(self, suite: "Suite") -> bool:
        """Attempt to lock dependencies required for the given suite."""

    @abstractmethod
    def free(self, suite: "Suite", devices: set[str] | None = None) -> None:
        """Release the dependencies assigned to a suite."""

    def keepalive(self) -> None:
        """Keep active dependency locks alive if required."""


# Local Dependency Manager for automatic medusa runs
class LocalDepManager(DepManager):
    def __init__(self, stage: "Stage") -> None:
        """Initialize the local dependency manager with the given stage."""

        super().__init__(stage)
        self.available = set(self.all)
        self.in_use: set[str] = set()

    def try_lock(self, suite: "Suite") -> bool:
        """Attempt to lock dependencies for the given suite. Returns True on
        success or False if not all necessary dependencies were available."""

        if not suite.deps_static.issubset(self.available):
            return False

        available_options = {
            name: ({dyn.value} if dyn.has_value else dyn.options).intersection(
                self.available - suite.deps_static
            )
            for name, dyn in suite.deps_dynamic.items()
        }

        assignments = resolve_dynamic_dependencies(available_options)

        if assignments is None:
            LOGGER.error(
                f"[LOCAL-DEPMGR] Failed to resolve dynamic dependencies for suite '{suite.full_name}'"
            )
            return False

        for name, value in assignments.items():
            dyn = suite.deps_dynamic[name]

            if not dyn.has_value:
                dyn.value = value

        deps_assigned = suite.deps

        assert deps_assigned.issubset(self.all)
        assert deps_assigned.issubset(self.available)

        self.available.difference_update(deps_assigned)
        self.in_use.update(deps_assigned)

        return True

    def free(
        self,
        suite: "Suite",
        devices: set[str] | None = None,
    ) -> None:
        """Release all or selected dependencies assigned of a suite."""

        deps_assigned = suite.deps if devices is None else devices

        assert deps_assigned.issubset(self.all)
        assert deps_assigned.issubset(self.in_use)

        self.available.update(deps_assigned)
        self.in_use.difference_update(deps_assigned)


# Dependency Manager over a server
class ServerDepManager(DepManager):
    def __init__(self, stage: "Stage", server_url: str) -> None:
        """Initialize the server dependency manager with the given stage and server URL."""

        super().__init__(stage)
        self.active_sessions: dict[Suite, tuple[str, set[str]]] = {}
        self._state_lock = threading.Lock()
        self.server_url = server_url

    def try_lock(self, suite: "Suite") -> bool:
        """Ask server to lock & assign the suite's dependencies.
        Returns true if the lock was successfully acquired and False otherwise.
        """

        # 1. Get the existing session, if the suite already has one
        session_id, existing_devices = self._get_session_data(suite)

        # 2. Convert the suite's dependencies into the format expected by the server
        payload = self._build_lock_payload(suite, session_id)

        # 3. Send lock request to the server
        response = self._send_lock_request(payload, session_id)

        if response is None:
            LOGGER.error(
                f"[SERVER-DEPMGR] Failed to send lock request for suite '{suite.full_name}'"
            )
            return False

        # 4. Extract the session_id from servers lock confirmation
        session_data = self._extract_session_id(response, session_id)

        if session_data is None:
            LOGGER.error(
                f"[SERVER-DEPMGR] Failed to extract session ID from lock confirmation for suite '{suite.full_name}'"
            )
            return False

        data, session_id = session_data

        # 5. Validate the servers lock confirmation before updating local state
        try:
            assigned_dynamic, concrete_devices = (
                self._validate_lock_confirmation(data, suite, existing_devices)
            )
        except (ValueError, TypeError) as exc:
            LOGGER.error(f"[SERVER-DEPMGR] Invalid lock confirmation: {exc}")

            # Release newly created session if confirmation is invalid
            if not existing_devices:
                self._release_invalid_session(session_id)

            return False

        # 6. Apply the servers dynamic assignments to the suite
        for name, device in assigned_dynamic.items():
            dyn = suite.deps_dynamic[name]

            if not dyn.has_value:
                dyn.value = device

        # 7. Store session_id and assigned devices for later keepalive & release requests
        with self._state_lock:
            self.active_sessions[suite] = (session_id, concrete_devices)

        LOGGER.info(
            f"[SERVER-DEPMGR] Locked {sorted(concrete_devices)} for session '{session_id}'."
        )
        return True

    def free(
        self,
        suite: "Suite",
        devices: set[str] | None = None,
    ) -> None:
        """Release all or selected dependencies of a suite."""

        session_id, session_devices = self._get_session_data(suite)

        if session_id is None:
            LOGGER.warning(
                f"[SERVER-DEPMGR] No active sessions found for '{suite.full_name}'."
            )
            return

        devices_to_free = self._get_devices_to_free(
            session_id, session_devices, devices
        )

        if devices_to_free is None:
            return

        # Close the session if no devices remain after freeing
        remaining_devices = session_devices - devices_to_free
        close_session = not remaining_devices

        payload = self._build_free_payload(
            session_id, devices_to_free, close_session
        )

        response = self._send_free_request(payload)

        if response is None:
            return

        # If the session no longer exists on the server
        # consider all locally tracked devices released
        if response.status_code == 404:
            devices_to_free = set(session_devices)
            close_session = True

        # Remove the session from local bookkeeping
        if close_session:
            with self._state_lock:
                self.active_sessions.pop(suite, None)
                LOGGER.info(
                    f"[SERVER-DEPMGR] Released {sorted(devices_to_free)} and finished session '{session_id}'."
                )
        else:
            with self._state_lock:
                self.active_sessions[suite] = (session_id, remaining_devices)

    def keepalive(self) -> None:
        """Send keepalive-requests for all active sessions."""

        # Take a snapshot of the active session_ids
        with self._state_lock:
            sessions = [
                session_id for session_id, _ in self.active_sessions.values()
            ]

        # Send keepalive requests without holding the state lock
        for session_id in sessions:
            self._send_keepalive_request(session_id)

    def _get_session_data(self, suite: "Suite") -> tuple[str | None, set[str]]:
        """Get the existing session_id and its assigned devices and return it as a tuple."""

        with self._state_lock:
            session_data = self.active_sessions.get(suite)

        if session_data is None:
            return None, set()

        return session_data

    def _build_lock_payload(
        self, suite: "Suite", session_id: str | None
    ) -> dict[str, Any]:
        """Build the payload for a server lock request and return it."""

        payload: dict[str, Any] = {
            "deps_static": sorted(suite.deps_static),
            "deps_dynamic": {
                name: ([dyn.value] if dyn.has_value else sorted(dyn.options))
                for name, dyn in suite.deps_dynamic.items()
            },
        }

        if session_id is not None:
            payload["session_id"] = session_id

        return payload

    def _build_free_payload(
        self,
        session_id: str,
        devices_to_free: set[str],
        close_session: bool,
    ) -> dict[str, Any]:
        """Build the payload for a server free request and return it."""

        payload: dict[str, Any] = {"session_id": session_id}

        if not close_session:
            payload["devices"] = sorted(devices_to_free)

        return payload

    def _send_lock_request(
        self, payload: dict[str, Any], session_id: str | None
    ) -> requests.Response | None:
        """Send a lock request and handle unsuccessful HTTP responses.
        Return the response if successful, None otherwise.
        """

        try:
            response = requests.post(
                f"{self.server_url}/lock", json=payload, timeout=5
            )
            LOGGER.debug(
                f"[SERVER-DEPMGR] HTTP response received: {response.status_code}"
            )
        except requests.RequestException as exc:
            LOGGER.error(f"[SERVER-DEPMGR] Lock request failed: {exc}")
            return None

        # Server immediately rejects request if the required dependencies are unavailable
        if response.status_code == 409:
            LOGGER.warning(
                "[SERVER-DEPMGR] Lock request rejected: dependencies unavailable."
            )
            return None

        if response.status_code == 404 and session_id is not None:
            LOGGER.warning(
                f"[SERVER-DEPMGR] Session '{session_id}' no longer exists on the server."
            )
            return None

        # Handle unexpected HTTP responses
        if response.status_code != 200:
            LOGGER.error(
                f"[SERVER-DEPMGR] Lock request failed: {response.status_code} {response.text}"
            )
            return None

        return response

    def _send_free_request(
        self, payload: dict[str, Any]
    ) -> requests.Response | None:
        """Send free request to the lock server.
        Return the response if successful, None otherwise.
        """

        try:
            response = requests.post(
                f"{self.server_url}/free", json=payload, timeout=5
            )
        except requests.RequestException as exc:
            LOGGER.error(f"[SERVER-DEPMGR] Failed to release session: {exc}")
            return None

        if response.status_code not in (200, 404):
            LOGGER.error(
                f"[SERVER-DEPMGR] Server rejected free request: {response.status_code} {response.text}"
            )
            return None

        return response

    def _send_keepalive_request(self, session_id: str) -> bool:
        """Send a keepalive request for one session.
        Return True if successful, False otherwise.
        """

        payload = {"session_id": session_id}

        try:
            response = requests.post(
                f"{self.server_url}/alive", json=payload, timeout=5
            )
        except requests.RequestException as exc:
            LOGGER.error(
                f"[SERVER-DEPMGR] Keepalive request failed for session {session_id}: {exc}"
            )
            return False

        if response.status_code != 200:
            LOGGER.error(
                f"[SERVER-DEPMGR] Keepalive request failed: {response.status_code} {response.text}"
            )
            return False

        LOGGER.debug(
            f"[SERVER-DEPMGR] Keepalive request sent for session '{session_id}'."
        )

        return True

    def _extract_session_id(
        self, response: requests.Response, session_id: str | None
    ) -> tuple[dict[str, Any], str] | None:
        """Extract and validate the session_id from the lock confirmation.
        Return a tuple of the response data and the session_id if valid, None otherwise.
        """

        try:
            data = response.json()

            if not isinstance(data, dict):
                raise TypeError("Lock confirmation must be a JSON object")

            returned_session_id = data.get("session_id")

            if (
                not isinstance(returned_session_id, str)
                or not returned_session_id
            ):
                raise ValueError("Missing or invalid session_id")

            # The server must return the same session_id when extending a session
            if session_id is not None and returned_session_id != session_id:
                raise ValueError("Server returned a different session_id")

            return data, returned_session_id

        except (ValueError, TypeError) as exc:
            LOGGER.error(f"[SERVER-DEPMGR] Invalid lock confirmation: {exc}")
            return None

    def _validate_dynamic_assignments(
        self, assigned_dynamic: dict[str, Any], suite: "Suite"
    ) -> None:
        """Validate the servers dynamic dependency assignments.
        Raise a ValueError if the assignments are invalid.
        """

        # Ensure that the server assigned every requested dynamic dependency
        if set(assigned_dynamic) != set(suite.deps_dynamic):
            raise ValueError("Dynamic dependency assignments do not match")

        # Ensure that each assigned device is a valid option for its dependency
        for name, device in assigned_dynamic.items():
            if (
                not isinstance(device, str)
                or device not in suite.deps_dynamic[name].options
            ):
                raise ValueError(
                    f"Invalid assignment for dynamic dependency '{name}'"
                )

            dyn = suite.deps_dynamic[name]

            if dyn.has_value and dyn.value != device:
                raise ValueError(
                    f"Server assigned device '{device}' to dynamic dependency '{name}', "
                    f"but it was expected to be '{dyn.value}'"
                )

    def _validate_lock_confirmation(
        self,
        data: dict[str, Any],
        suite: "Suite",
        existing_devices: set[str],
    ) -> tuple[dict[str, str], set[str]]:
        """Validate the servers lock confirmation.
        Raise a ValueError if the confirmation is invalid.
        Return a tuple containing the assigned dynamic dependencies and the concrete devices.
        """

        if data.get("status") != "locked":
            raise ValueError(f"Unexpected lock response: {data}")

        assigned_dynamic = data.get("deps_assigned")
        devices_raw = data.get("devices")

        if (
            not isinstance(assigned_dynamic, dict)
            or not isinstance(devices_raw, list)
            or any(not isinstance(device, str) for device in devices_raw)
        ):
            raise ValueError("Invalid lock confirmation fields")

        self._validate_dynamic_assignments(assigned_dynamic, suite)

        concrete_devices = set(devices_raw)

        # The server returns all devices belonging to the same session,
        # including devices locked by earlier requests
        requested_devices = set(
            suite.deps_static | set(assigned_dynamic.values())
        )
        expected_devices = existing_devices | requested_devices

        # Ensure that the servers device list matches the actual assignments
        if concrete_devices != expected_devices:
            raise ValueError(
                "Server's device list does not match its assignments"
            )

        # Ensure that the server only assigned devices belonging to this stage
        if not concrete_devices.issubset(self.all):
            raise ValueError(
                "Server assigned a device outside this stage's dependencies"
            )

        return assigned_dynamic, concrete_devices

    def _release_invalid_session(self, session_id: str) -> None:
        """Release a newly created session after an invalid confirmation."""

        payload = self._build_free_payload(
            session_id, set(), close_session=True
        )
        self._send_free_request(payload)

    def _get_devices_to_free(
        self,
        session_id: str,
        session_devices: set[str],
        devices: set[str] | None,
    ) -> set[str] | None:
        """Determine which devices can be released."""

        if devices is None:
            return set(session_devices)

        devices_to_free = set(devices)

        if not devices_to_free:
            LOGGER.warning(
                f"[SERVER-DEPMGR] No devices to release for session '{session_id}'."
            )
            return None

        if not devices_to_free.issubset(session_devices):
            LOGGER.error(
                f"[SERVER-DEPMGR] Cannot release devices not owned by session '{session_id}': {sorted(devices_to_free - session_devices)}"
            )
            return None

        return devices_to_free

    def _update_local_session(
        self,
        suite: "Suite",
        session_id: str,
        session_devices: set[str],
        devices_to_free: set[str],
        close_session: bool,
    ) -> None:
        """Update local session bookkeeping after freeing devices."""

        remaining_devices = session_devices - devices_to_free

        with self._state_lock:
            if close_session:
                self.active_sessions.pop(suite, None)
            else:
                self.active_sessions[suite] = (session_id, remaining_devices)


def get_dep_manager(stage: "Stage", settings: "Settings") -> DepManager:
    """Create the appropriate dependency manager based on the settings.
    Return a ServerDepManager if a server URL is specified in the settings,
    otherwise return a LocalDepManager.
    """

    if settings.server_url:
        return ServerDepManager(stage, settings.server_url)

    return LocalDepManager(stage)
