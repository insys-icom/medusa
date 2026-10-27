import argparse
from datetime import datetime
import requests
import uuid
import time
import json
import os


SERVER_URL = "http://127.0.0.1:8080"
SESSION_FILE = os.path.expanduser("~/.medusa_lock_session")


def save_session(client_id: str, session_id: str, devices: list[str]):
    # Saves session data
    with open(SESSION_FILE, "w") as file:
        json.dump({"client_id": client_id,
                   "session_id": session_id,
                   "devices": devices,},
                   file
        )

def load_session() -> tuple[str, str] | None:
    # Loads session-file
    try:
        with open(SESSION_FILE) as file:
            data = json.load(file)

        return data["client_id"], data["session_id"], data["devices"]

    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None

def delete_session():
    # Deletes session-file
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass


def generate_client_id() -> str:
    # Generates a unique client ID for this CLI session
    return str(uuid.uuid4())

def parse_arguments():
    # Parses arguments
    parser = argparse.ArgumentParser(description="Medusa lock-server CLI")

    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("lock")
    lock_parser.add_argument("dependencies", nargs="+", help="Dependencies to lock")

    free_parser = subparsers.add_parser("free")
    free_parser.add_argument("dependencies", nargs="+", help="Dependencies to free")

    status_parser = subparsers.add_parser("status")

    return parser.parse_args()

def parse_dependencies(dependencies):
    # Parses dependency-arguments into static & dynamic dependencies
    static_deps = []
    dynamic_deps = {}

    for dep in dependencies:
        if dep.startswith("dynamic:"):
            dynamic = dep[len("dynamic:"):]
            name, values = dynamic.split("=", 1)
            dynamic_deps[name] = values.split(",")
        else:
            static_deps.append(dep)

    return static_deps, dynamic_deps


def send_lock(client_id: str, session_id: str, static_deps: list[str], dynamic_deps: list[set]) -> bool:
    # Sends lock-request to server for dependencies
    payload = {"client_id": client_id,
               "session_id": session_id,
               "deps_static": static_deps,
               "deps_dynamic": dynamic_deps}

    try:
        response = requests.post(f"{SERVER_URL}/lock", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact lock server: {exc}")
        return False

    if response.status_code != 200:
        print(f"Lock request failed: {response.status_code} {response.text}")
        return False

    data = response.json()
    devices_locked = data.get("devices", [])
    dynamic_locked = data.get("deps_assigned", [])

    print(f"\nLocked dependencies:")
    print(f"- devices: {devices_locked}")
    print(f"- dynamic: {dynamic_locked}")

    save_session(client_id, session_id, devices_locked)

    return True

def send_free(client_id: str, session_id: str, dependencies: list[str]) -> bool:
    # Release-request for locked dependencies
    payload = {"client_id": client_id,
               "session_id": session_id,
               "devices": dependencies}

    try:
        response = requests.post(f"{SERVER_URL}/free", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact server: {exc}")
        return False

    if response.status_code != 200:
        print(f"Free request failed: {response.status_code} {response.text}")
        return False

    print(f"Freed dependencies: {dependencies}")
    return True

def show_status():
    # Print current server status
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

    print_status(data)

def print_status(data):
    active_locks = data.get("active_locks", {})

    if not active_locks:
        print("No active locks.")
    else:
        print("Active locks:\n")
        print(f"{'DEVICE':<10} {'CLIENT':<40} {'SINCE'}")

        for device, lock in active_locks.items():
            client_id = lock["client_id"]
            timestamp = datetime.fromtimestamp(lock["timestamp"]).strftime("%H:%M:%S")
            print(f"{device:<10} {client_id:<40} {timestamp}")


def send_keepalive(client_id: str, session_id: str) -> bool | None:
    # Tell server that client is still alive
    try:
        response = requests.post(f"{SERVER_URL}/alive", json={"client_id": client_id, "session_id": session_id}, timeout=5)
    except requests.RequestException as exc:
        print(f"Keepalive failed: {exc}")
        return False

    if response.status_code == 404:
        print(f"Session {session_id} no longer exists on the server. Stopping keepalive.")
        return None

    if response.status_code != 200:
        print(f"Keepalive rejected: {response.status_code} {response.text}")
        return False

    return True

def keepalive_loop(client_id: str, session_id: str):
    # Keep the client alive while it owns locks
    while True:
        time.sleep(10)

        result = send_keepalive(client_id, session_id)

        if result is None:
            # Session was deleted
            break

        if not result:
            print(f"Keepalive failed. Stopping keepalive loop. (Client: {client_id})")
            break


def main():
    args = parse_arguments()

    if args.command == "status":
        show_status()
        return

    if args.command == "lock":
        client_id = generate_client_id()
        session_id = str(uuid.uuid4())
        static_deps, dynamic_deps = parse_dependencies(args.dependencies)

        if send_lock(client_id, session_id, static_deps, dynamic_deps):
            print(f"Client ID: {client_id}")
            print(f"Session ID: {session_id}")

            try:
                keepalive_loop(client_id, session_id)
            except KeyboardInterrupt:
                print(f"\nStopping client '{client_id}'.")

    elif args.command == "free":
        session = load_session()

        if session is None:
            print("No active lock session found.")
            return

        client_id, session_id, devices = session

        if send_free(client_id, session_id, args.dependencies):
            remaining_devices = [
                device for device in devices if devices not in args.dependencies
            ]
            if remaining_devices:
                save_session(client_id, session_id, remaining_devices)
            else:
                delete_session()

if __name__ == "__main__":
    main()