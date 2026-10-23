import argparse
import requests
import uuid


SERVER_URL = "http://127.0.0.1:8080"


def generate_client_id() -> str:
    # Generates a unique client ID for this CLI session
    return str(uuid.uuid4())

def parse_arguments():
    parser = argparse.ArgumentParser(description="Medusa lock-server CLI")

    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("lock")
    lock_parser.add_argument("dependencies", nargs="+", help="Dependencies to lock")

    free_parser = subparsers.add_parser("free")
    free_parser.add_argument("dependencies", nargs="+", help="Dependencies to free")

    status_parser = subparsers.add_parser("status")

    return parser.parse_args()

def send_lock():
    pass

def send_free():
    pass

def show_status():
    pass

def send_keepalive():
    pass

def main():
    client_id = generate_client_id()

    args = parse_arguments()

    if args.command == "status":
        show_status()
        return

    if args.command == "lock":
        if send_lock(client_id, args.dependencies):
            print(f"Client ID: {client_id}")
            send_keepalive(client_id)

    elif args.command == "free":
        send_free(client_id, args.dependencies)

if __name__ == "__main__":
    main()