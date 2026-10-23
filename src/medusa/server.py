from aiohttp import web
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import time

@dataclass
class ClientInfo:
    client_id: str
    last_alive: float = field(default_factory=time.time)
    locks: set[str] = field(default_factory=set)


class DepLockManager:
    def __init__(self):
        # structure: {"device_name": {"device": "...", "client_id": "...", "timestamp": ...}}
        self.active_locks = {}              # which devices are locked
        self.server_mutex = asyncio.Lock()  # mutex-protection
        self.clients: dict[str, ClientInfo]= {}

    def get_or_create_client(self, client_id: str) -> ClientInfo:
        if client_id not in self.clients:
            self.clients[client_id] = ClientInfo(client_id=client_id, last_alive=time.time())

        return self.clients[client_id]

    def update_alive(self, client_id: str) -> ClientInfo | None:
        client = self.clients.get(client_id)
        if client is None:
            return None

        client.last_alive = time.time()
        return client

    def check_and_reclaim_expired_locks(self, timeout_seconds=10):
        # Cleans up expired locks
        current_time = time.time()

        # Get expired clients
        expired_clients = []
        for client_id, client in self.clients.items():
            if (current_time - client.last_alive) > timeout_seconds:
                expired_clients.append(client_id)

        for client_id in expired_clients:
            client = self.clients[client_id]
            print(f"[SERVER] Client '{client_id} has expired. Last alive: {client.last_alive}")

            # Free all locks belonging to this client
            self.release_all_locks_for_client(client_id, client.locks.copy())

            # Remove client
            del self.clients[client_id]

    def release_all_locks_for_client(self, client_id: str, devices: set[str]):
        # Find all devices which are currently owned by a specific client

        client = self.clients.get(client_id)

        if client is None:
            return {"status": "error", "message": "Unknown client."}

        # Check ownership
        for device in devices:
            lock = self.active_locks.get(device)

            if lock is None:
                continue

            if lock["client_id"] != client_id:
                return {"status": "rejected",
                        "message": f"Device '{device}' is locked by '{lock['client_id']}'."}

        # Release locks
        for device in devices:
            self.active_locks.pop(device, None)
            client.locks.discard(device)

        print(f"[FREE] Released locks of client '{client_id}': {sorted(devices)}.")
        return {"status": "freed", "devices": sorted(devices)}

    def resolve_dynamic_dependencies(self, available_options: dict[str, set[str]]) -> dict [str, str] | None:
        # Assign a distinct available device to every dynamic dependency

        if any(not options for options in available_options.values()):
            return None

        option_owner: dict[str, str] = {}

        def try_assign(dep_name: str, seen: set[str]) -> bool:
            for option in sorted(available_options[dep_name]):
                if option in seen:
                    continue

                seen.add(option)
                owner = option_owner.get(option)

                if owner is None or try_assign(owner, seen):
                    option_owner[option] = dep_name
                    return True

            return False

        for name in available_options:
            if not try_assign(name, set()):
                return None

        return {
            dep_name: device
            for device, dep_name in option_owner.items()
        }

    async def resolve_and_assign_dependencies(self, client_id: str, static_deps: set[str], dynamic_raw: dict[str, set[str]]) -> dict:
        print(f"[SERVER] Request for locking received from '{client_id}': static={sorted(static_deps)}, dynamic={dynamic_raw}")

        busy_deps = set(self.active_locks)
        busy_static = static_deps & busy_deps

        if busy_static:
            unavailable_message = (f"Static dependencies are busy: {sorted(busy_static)}")
            return {"status": "unavailable", "message": unavailable_message}

        available_options = {name: options - busy_deps - static_deps
                            for name, options in dynamic_raw.items()}

        assigned_dynamic = self.resolve_dynamic_dependencies(available_options)

        if assigned_dynamic is None:
            unavailable_dependencies = [name
                for name, options in available_options.items()
                if not options]
            unavailable_message = (f"No available devices for dynamic dependencies: {unavailable_dependencies}")
            return {"status": "unavailable", "message": unavailable_message}

        concrete_deps = (static_deps | set(assigned_dynamic.values()))

        # All devices are free
        client = self.get_or_create_client(client_id)
        client.last_alive = time.time()
        lock_timestamp = time.time()

        for device in concrete_deps:
            self.active_locks[device] = {"device": device,
                                            "client_id": client_id,
                                            "timestamp": lock_timestamp}

        client.locks.update(concrete_deps)

        return {"status": "locked",
                "assigned_dynamic": assigned_dynamic,
                "concrete_deps": concrete_deps}

POOL = DepLockManager()


async def parse_lock_request(request_data):
    try:
        body = await request_data.json()
        client_id = body.get("client_id")
        static_raw = body.get("deps_static", [])
        dynamic_raw = body.get("deps_dynamic", {})
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"},
            status=400)

    if (not isinstance(client_id, str)
        or not client_id
        or not isinstance(static_raw, list)
        or not isinstance(dynamic_raw, dict)
    ):
        return web.json_response({
            "status": "error",
            "message": "Missing / invalid request fields"},
            status=400)

    if (any(
            not isinstance(device, str)
            or not device
            for device in static_raw
        )
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(options, list)
            or any(
                not isinstance(device, str)
                or not device
                for device in options
            )
            for name, options in dynamic_raw.items()
        )
    ):
        return web.json_response({
            "status": "error",
            "message": "Invalid dependency names or options"},
            status=400)

    return client_id, static_raw, dynamic_raw

async def parse_free_request(request):
    try:
        body = await request.json()
        devices = body.get("devices")
        client_id = body.get("client_id")
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"},
            status=400)

    if not isinstance(client_id, str) or not client_id:
        return web.json_response({
            "status": "error",
            "message": "Missing client_id"},
            status=400)

    if not isinstance(devices, list) or any(not isinstance(device, str) for device in devices):
        return web.json_response({
                    "status": "error",
                    "message": "Invalid devices"},
                    status=400)

    return client_id, set(devices)


async def handle_alive(request):
    # Function to handle a alive-request from client

    try:
        body = await request.json()
        client_id = body.get("client_id")

    except Exception:
        return web.json_response({"status": "error", "message": "Malformed JSON payload"}, status=400)

    if not isinstance(client_id, str) or not client_id:
        return web.json_response({"status": "error", "message": "Missing client_id"}, status=400)

    async with POOL.server_mutex:
        updated = POOL.update_alive(client_id)

    if not updated:
        return web.json_response({"status": "error", "message": "Unknown client"}, status=404)

    readable_time = datetime.fromtimestamp(updated.last_alive).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[ALIVE] Client '{client_id}' updated last_alive to {readable_time}")
    return web.json_response({"status": "alive"})

async def handle_lock(request):
    # Function for locking dependencies according to "all or nothing"-principle

    # 1. Payload parsing & validation
    client_id, static_raw, dynamic_raw = await parse_lock_request(request)

    # 2. Prepare dependencies
    static_deps = set(static_raw)
    if len(static_deps) != len(static_raw):
        return web.json_response({
            "status": "error",
            "message": "Duplicate static dependencies"},
            status=400)

    dynamic_options = {
        name: set(options) - static_deps
        for name, options in dynamic_raw.items()
    }
    if not static_deps and not dynamic_options:
        return web.json_response({
            "status": "error",
            "message": "No dependencies supplied"},
            status=400)

    async with POOL.server_mutex:
        # 3. Preventive Cleanup: Prevents orphaned locks from blocking the system
        POOL.check_and_reclaim_expired_locks(timeout_seconds=60)

        # 4. Check availability & assign dependencies
        lock_data = await POOL.resolve_and_assign_dependencies(client_id, static_deps, dynamic_options)

    # 5. Unavailable: Return immediately without acquiring any locks
    if lock_data["status"] == "unavailable":
        print(f"[SERVER] Some Dependencies from lock-request from '{client_id}' are unavailable.")
        return web.json_response(lock_data, status=409)

    # 6. Lock successfully acquired
    assigned_dynamic = lock_data["assigned_dynamic"]
    concrete_deps = lock_data["concrete_deps"]

    print(f"[SERVER] Lock granted to '{client_id}' for {sorted(concrete_deps)}.")

    # 7. Return lock confirmation
    return web.json_response({"status": "locked",
                              "client_id": client_id,
                              "devices": sorted(concrete_deps),
                              "deps_assigned": assigned_dynamic})

async def handle_free(request):
    # Function for unlocking currently locked devices / dependencies

    # Payload parsing & validation
    client_id, devices = await parse_free_request(request)

    # Cleanup & release locks
    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

        client = POOL.clients.get(client_id)

        if client is None:
            return web.json_response({"status": "error",
                                      "message": "Unknown client"},
                                      status=404)

        print(f"[SERVER] Request for unlocking batch {devices} received from '{client_id}'. Checking status ...")

        result = POOL.release_all_locks_for_client(client_id, set(devices))

    if result["status"] == "error":
            return web.json_response(result, status=404)

    if result["status"] == "rejected":
        return web.json_response(result, status=409)

    return web.json_response(result, status=200)

async def handle_status(request):
    # Function for showing which devices are locked by which user & since when
    # Shows a CLI-Dashboard with locked devices
    print(f"[SERVER] Global status dashboard request received. Preparing data ...")

    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks(timeout_seconds=5)

        safe_active_locks = {}

        for device, meta in POOL.active_locks.items():
            if isinstance(meta, dict):
                client_id = meta.get("client_id", "")

                safe_active_locks[device] = {"device": meta.get("device"),
                                            "client_id": client_id,
                                            "timestamp": meta.get("timestamp")}

    status_report = {"active_locks": safe_active_locks}
    return web.json_response(status_report)


if __name__ == "__main__":
    app = web.Application()
    app.add_routes([
        web.get('/status', handle_status),
        web.post('/lock', handle_lock),
        web.post('/free', handle_free),
        web.post('/alive', handle_alive)
    ])
    print("Medusa lock-server auf http://127.0.0.1:8080 ...")
    web.run_app(app, host='127.0.0.1', port=8080)