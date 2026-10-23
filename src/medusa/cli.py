import argparse
import requests
import uuid
import time


SERVER_URL = "http://127.0.0.1:8080"


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

def send_lock(client_id: str, static_deps: list[str], dynamic_deps: list[set]) -> bool:
    # Sends lock-request to server for dependencies
    payload = {"client_id": client_id,
               "deps_static": static_deps,
               "deps_dynamic": dynamic_deps}

    try:
        response = requests.get(f"{SERVER_URL}/lock", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact lock server: {exc}")
        return False

    if response.status_code != 200:
        print(f"Lock request failed: {response.status_code} {response.text}")
        return False

    data = response.json()
    static_locked = data.get('devices', [])
    dynamic_locked = data.get('deps_assigned', [])

    print(f"\nLocked dependencies:")
    print(f"- static: {static_locked}")
    print(f"- dynamic: {dynamic_locked}")

    return True

def send_free(client_id:str, dependencies: list[str]) -> bool:
    # Release-request for locked dependencies
    payload = {"client_id": client_id,
               "devices": dependencies}

    try:
        response = requests.get(f"{SERVER_URL}/free", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"Failed to contact server: {exc}")
        return False

    if response.status_code != 200:
        print(f"Free request failed: {response.status_code} {response.text}")
        return False

    print(f"Freed dependencies: {dependencies}")
    return True

def show_status() -> bool:
    # Print current server status
    try:
        response = requests.get(f"{SERVER_URL}/status", timeout=5)
    except requests.RequestException as exc:
        print(f"Status request failed: {exc}")
        return False

    if response.status_code != 200:
        print(f"Keepalive rejected: {response.status_code} {response.text}")
        return False

    print(response.text)
    return True

def send_keepalive(client_id: str) -> bool:
    # Tell server that client is still alive
    try:
        response = requests.post(f"{SERVER_URL}/alive", json={"client_id": client_id}, timeout=5)
    except requests.RequestException as exc:
        print(f"Keepalive failed: {exc}")
        return False

    if response.status_code != 200:
        print(f"Keepalive rejected: {response.status_code} {response.text}")
        return False

    return True

def keepalive_loop(client_id: str):
    # Keep the client alive while it owns locks
    try:
        while True:
            time.sleep(30)

            if not send_keepalive(client_id):
                print("Keepalive failed. Stopping keepalive loop.")
                break

    except KeyboardInterrupt:
        print("\nStopping client.")

def main():
    client_id = generate_client_id()

    args = parse_arguments()

    if args.command == "status":
        show_status()
        return

    if args.command == "lock":
        if send_lock(client_id, args.dependencies):
            print(f"Client ID: {client_id}")
            keepalive_loop(client_id)

    elif args.command == "free":
        send_free(client_id, args.dependencies)

if __name__ == "__main__":
    main()