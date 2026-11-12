import time
from datetime import datetime
from typing import Any

import requests

SERVER_URL = "http://127.0.0.1:8080"


def _parse_dependencies(
    dependencies: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Parse dependency-arguments into static & dynamic dependencies"""

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
    static_deps: list[str], dynamic_deps: dict[str, list[str]]
) -> str | None:
    """Send lock-request to server and returns the session_id"""

    payload: dict[str, Any] = {
        "deps_static": static_deps,
        "deps_dynamic": {
            name: sorted(options) for name, options in dynamic_deps.items()
        },
    }

    try:
        response = requests.post(f"{SERVER_URL}/lock", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact lock server: {exc}")
        return None

    if response.status_code != 200:
        print(f"Lock request failed: {response.status_code} {response.text}")
        return None

    data = response.json()
    session_id = data.get("session_id")
    devices_locked = data.get("devices", [])
    dynamic_locked = data.get("deps_assigned", [])

    if not isinstance(session_id, str) or not session_id:
        print("Lock request failed: Server did not return a session ID.")
        return None

    print("Locked dependencies:")
    print(f"- devices: {devices_locked}")
    print(f"- dynamic: {dynamic_locked}")

    return session_id


def _send_free(session_id: str) -> bool:
    """Sends release-request for locked dependencies"""

    payload = {"session_id": session_id}

    try:
        response = requests.post(f"{SERVER_URL}/free", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact server: {exc}")
        return False

    if response.status_code != 200:
        print(f"Free request failed: {response.status_code} {response.text}")
        return False

    result = response.json()
    print(f"Freed dependencies: {result.get('devices', [])}")
    return True


def _format_datetime(value: str) -> str:
    """Format an ISO-8601 datetime as local time."""
    return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")


def _print_status(data: dict):
    """Print active sessions and their locked devices"""

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


def _send_keepalive(session_id: str) -> bool | None:
    """Tell the server that the session is still alive"""

    payload = {"session_id": session_id}

    try:
        response = requests.post(
            f"{SERVER_URL}/alive",
            json=payload,
            timeout=5,
        )
    except requests.RequestException as exc:
        print(f"Keepalive failed: {exc}")
        return False

    if response.status_code == 404:
        print(f"Session {session_id} no longer exists on the server.")
        return None

    if response.status_code != 200:
        print(f"Keepalive rejected: {response.status_code} {response.text}")
        return False

    return True


def _keepalive_loop(session_id: str):
    """Keep the session alive while it owns locks"""

    while True:
        time.sleep(10)

        result = _send_keepalive(session_id)

        if result is None:
            # Session was deleted on the server
            break

        if not result:
            print(
                f"Keepalive failed. Stopping keepalive loop. (Session: {session_id})"
            )
            break


def lock_dependencies(dependencies: list[str]) -> None:
    """Lock the specified dependencies and maintain the session alive until interrupted."""

    static_deps, dynamic_deps = _parse_dependencies(dependencies)
    session_id = _send_lock(static_deps, dynamic_deps)

    if not session_id:
        return

    print(f"Session ID: {session_id}")

    try:
        _keepalive_loop(session_id)
    except KeyboardInterrupt:
        print("\nLock session interrupted.")

        if _send_free(session_id):
            print(f"Session '{session_id}' released.")
        else:
            print(
                f"Could not release session '{session_id}'. The server will reclaim it after timeout."
            )


def free_dependencies(session_id: str) -> None:
    """Free the specified devices of a session by sending a release request to the server."""

    if _send_free(session_id):
        print(f"Session '{session_id}' released.")


def show_status():
    """Send status-request and print current server status"""

    try:
        response = requests.get(f"{SERVER_URL}/status", timeout=5)
    except requests.RequestException as exc:
        print(f"Status request failed: {exc}")
        return

    if response.status_code != 200:
        print(f"Status rejected: {response.status_code} {response.text}")
        return

    try:
        data = response.json()
    except ValueError:
        print("Status request returned invalid JSON.")
        return

    _print_status(data)
