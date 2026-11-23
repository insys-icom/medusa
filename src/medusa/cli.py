import time
from datetime import datetime
from typing import Any

import requests

from .utils import LOGGER


def show_status(server_url: str) -> None:
    """Send status-request and print current server status."""

    try:
        response = requests.get(f"{server_url}/status", timeout=5)
    except requests.RequestException as exc:
        LOGGER.error(f"[CLI] Status request failed: {exc}")
        return

    if response.status_code != 200:
        LOGGER.error(
            f"[CLI] Status rejected: {response.status_code} {response.text}"
        )
        return

    try:
        data = response.json()
    except ValueError:
        LOGGER.warning("[CLI] Status request returned invalid JSON.")
        return

    _print_status(data)


def _print_status(data: dict):
    """Print active sessions and their locked devices."""

    sessions = data.get("sessions", [])

    if not sessions:
        print("No active locks.")
        return

    print(
        "============================== ACTIVE LOCKS =============================="
    )
    print(f"{'SESSION':<38} {'DEVICES':<10} {'SINCE':<12} {'LAST ALIVE'}")
    print("=" * 74)  # Devider

    for session in sessions:
        session_id = session["session_id"]
        since = _format_datetime(session["since"])
        last_alive = _format_datetime(session["last_alive"])
        devices = session["devices"]

        for index, device in enumerate(devices):
            if index == 0:
                print(
                    f"{session_id:<38} {device:<10} {since:<12} {last_alive}"
                )
            else:
                print(f"{'':<38} {device:<10} {'':<12} {'':<12}")

        print("-" * 74)  # Devider


def _format_datetime(value: str) -> str:
    """Format an ISO-8601 datetime as local time."""
    return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")


def lock_dependencies(dependencies: list[str], server_url: str) -> None:
    """Lock the specified dependencies and maintain the session alive until interrupted."""

    static_deps, dynamic_deps = _parse_dependencies(dependencies)
    session_id = _send_lock(static_deps, dynamic_deps, server_url)

    if not session_id:
        return

    LOGGER.info(f"[CLI] Session ID: {session_id}")
    print(f"Session ID: {session_id}")

    try:
        _keepalive(session_id, server_url)
    except KeyboardInterrupt:
        LOGGER.info("[CLI] Lock session interrupted.")

        if free(session_id, server_url):
            LOGGER.info(f"[CLI] Session '{session_id}' released.")
            print(f"Session '{session_id}' released.")
        else:
            LOGGER.warning(
                f"[CLI] Could not release session '{session_id}'. The server will reclaim it after timeout."
            )


def _parse_dependencies(
    dependencies: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Parse dependency-arguments into static & dynamic dependencies.
    Return a tuple containing a list of static dependencies and a dictionary of dynamic dependencies.
    """

    static_deps = []
    dynamic_deps = {}

    for dep in dependencies:
        if dep.startswith("dynamic:"):
            dynamic = dep[len("dynamic:") :]
            name, values = dynamic.split("=", 1)
            dynamic_deps[name] = values.split(",")
        else:
            static_deps.append(dep)

    return static_deps, dynamic_deps


def _send_lock(
    static_deps: list[str], dynamic_deps: dict[str, list[str]], server_url: str
) -> str | None:
    """Send lock-request to server and returns the session_id.
    Return None if the request fails or the server does not provide a session ID.
    Else return the session ID.
    """

    payload: dict[str, Any] = {
        "deps_static": static_deps,
        "deps_dynamic": {
            name: sorted(options) for name, options in dynamic_deps.items()
        },
    }

    try:
        response = requests.post(f"{server_url}/lock", json=payload, timeout=5)
    except requests.RequestException as exc:
        LOGGER.error(f"[CLI] Failed to contact lock server: {exc}")
        return None

    if response.status_code != 200:
        LOGGER.error(
            f"[CLI] Lock request failed: {response.status_code} {response.text}"
        )
        return None

    data = response.json()
    session_id = data.get("session_id")
    devices_locked = data.get("devices", [])
    dynamic_locked = data.get("deps_assigned", [])

    if not isinstance(session_id, str) or not session_id:
        LOGGER.error(
            "[CLI] Lock request failed: Server did not return a session ID."
        )
        return None

    print("Locked dependencies:")
    print(f"- devices: {devices_locked}")
    print(f"- dynamic: {dynamic_locked}")

    return session_id


def _keepalive(session_id: str, server_url: str) -> None:
    """Keep the session alive on the server until it is gone or a request fails.
    Return None when the session is no longer active or a request fails.
    """

    payload = {"session_id": session_id}

    while True:
        time.sleep(10)

        try:
            response = requests.post(
                f"{server_url}/alive",
                json=payload,
                timeout=5,
            )
        except requests.RequestException as exc:
            LOGGER.error(f"Keepalive failed: {exc}")
            return

        if response.status_code == 404:
            LOGGER.error(
                f"[CLI] Session {session_id} no longer exists on the server."
            )
            return

        if response.status_code != 200:
            LOGGER.error(
                f"[CLI] Keepalive rejected: {response.status_code} {response.text}"
            )
            return


def free(session_id: str, server_url: str) -> bool:
    """Free the specified devices of a session by sending a release request to the server.
    Return True if the session was successfully released, False otherwise.
    """

    payload = {"session_id": session_id}

    try:
        response = requests.post(f"{server_url}/free", json=payload, timeout=5)
    except requests.RequestException as exc:
        LOGGER.error(f"[CLI-FREE] Failed to contact server: {exc}")
        return False

    if response.status_code != 200:
        LOGGER.error(
            f"[CLI] Free request failed: {response.status_code} {response.text}"
        )
        return False

    result = response.json()
    LOGGER.info(f"[CLI] Freed dependencies: {result.get('devices', [])}")
    print(f"Freed dependencies: {result.get('devices', [])}")
    return True
