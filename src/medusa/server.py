from aiohttp import web
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import time
import uuid


@dataclass(eq=False)
class Session:
    uuid: str
    devices: set[str] = field(default_factory=set)
    since: float = field(default_factory=time.time)
    last_alive: float = field(default_factory=time.time)

    @classmethod
    def create_session(cls, devices: set[str]) -> "Session":
        # Creates a new session
        now = time.time()

        return cls(
            uuid=str(uuid.uuid4()),
            devices=set(devices),
            since=now,
            last_alive=now,
        )


class DepLockManager:
    def __init__(self):
        self.server_mutex = asyncio.Lock()  # mutex-protection
        self.sessions: set[Session] = set() # set of all active sessions

    def check_and_reclaim_expired_locks(self, timeout_seconds=30):
        # Clean up expired locks

        current_time = time.time()

        # Get expired sessions
        expired_sessions = [
            session
            for session in self.sessions
            if current_time - session.last_alive > timeout_seconds
        ]

        # Remove expired sessions
        for session in expired_sessions:
            self.sessions.remove(session)

            print(f"[CLEANUP] Released locks of session '{session.uuid}': {sorted(session.devices)}.")

    def release_session(self, session_id: str) -> dict:
        # Find all devices which are owned by a specific session & release them

        session = next(
            (s for s in self.sessions if s.uuid == session_id),
            None,
        )

        if session_id is None:
            return {
                "status": "error",
                "message": "Unknown session.",
            }

        self.sessions.remove(session)

        devices = sorted(session.devices)

        print(f"[FREE] Released session '{session_id}'. Freed devices: {devices}.")

        return {
            "status": "freed",
            "session_id": session_id,
            "devices": devices,
        }

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

    async def resolve_and_assign_dependencies(self, static_deps: set[str], dynamic_raw: dict[str, set[str]]) -> dict:
        # Resolve dependencies and create a session if all devices are available

        print(f"[SERVER] Lock request received: static={sorted(static_deps)}, dynamic={dynamic_raw}")

        # 1. Collect all devices currently locked by any session
        busy_deps = {device
                     for session in self.sessions
                     for device in session.devices
        }

        # 2. Check whether the requested static dependencies are available
        busy_static = static_deps & busy_deps

        if busy_static:
            unavailable_message = (f"Static dependencies are busy: {sorted(busy_static)}")
            return {
                "status": "unavailable",
                "message": unavailable_message,
            }

        # 3. Remove busy devices and static dependencies from dynamic options
        available_options = {
            name: options - busy_deps - static_deps
            for name, options in dynamic_raw.items()
        }

        # 4. Assign concrete devices to the dynamic dependencies
        assigned_dynamic = self.resolve_dynamic_dependencies(available_options)

        if assigned_dynamic is None:
            unavailable_dependencies = [
                name
                for name, options in available_options.items()
                if not options
            ]

            unavailable_message = (f"No available devices for dynamic dependencies: {unavailable_dependencies}")
            return {
                "status": "unavailable",
                "message": unavailable_message,
            }

        # 5. Combine static and assigned dynamic dependencies
        concrete_deps = (static_deps | set(assigned_dynamic.values()))

        # 6. Create and register a new session for this lock request
        session = Session.create_session(concrete_deps)
        self.sessions.add(session)

        # 7. Return the result to the handler
        return {
            "status": "locked",
            "session": session,
            "assigned_dynamic": assigned_dynamic,
            "concrete_deps": concrete_deps,
        }

POOL = DepLockManager()


async def cleanup_loop():
    # Clean up expired locks every 5 seconds
    while True:
        async with POOL.server_mutex:
            POOL.check_and_reclaim_expired_locks(timeout_seconds=30)

        await asyncio.sleep(5)

async def on_startup(app):
    # Create cleanup-task when the application was started
    app["cleanup_task"] = asyncio.create_task(cleanup_loop())

async def on_cleanup(app):
    # Stop cleanup-task when application ends
    app["cleanup_task"].cancel()

    try:
        await app["cleanup_task"]
    except asyncio.CancelledError:
        pass


async def parse_lock_request(request_data):
    try:
        body = await request_data.json()
        static_raw = body.get("deps_static", [])
        dynamic_raw = body.get("deps_dynamic", {})
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"
        }, status=400)

    if (not isinstance(static_raw, list)
        or not isinstance(dynamic_raw, dict)):
        return web.json_response({
            "status": "error",
            "message": "Missing / invalid request fields"
        }, status=400)

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
            "message": "Invalid dependency names or options"
        }, status=400)

    return static_raw, dynamic_raw

async def parse_free_request(request):
    # Parse and validate a session ID from a free request
    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"},
            status=400)

    session_id = body.get("session_id")

    if not isinstance(session_id, str) or not session_id:
        return web.json_response({
            "status": "error",
            "message": "Missing or invalid session_id"
        }, status=400)

    return session_id


async def handle_status(request):
    # Shows all active sessions and their locked devices

    print("[SERVER] Global status dashboard request received.")

    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks()

        sessions = [
            {
                "session_id": session.uuid,
                "devices": sorted(session.devices),
                "since": session.since,
                "last_alive": session.last_alive,
            }
            for session in POOL.sessions
        ]

    return web.json_response({"sessions": sessions})

async def handle_alive(request):
    # Update the last_alive timestamp of an existing session

    try:
        body = await request.json()
        session_id = body.get("session_id")
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"
        }, status=400)

    if not isinstance(session_id, str) or not session_id:
        return web.json_response({
            "status": "error",
            "message": "Missing or invalid session_id"
        }, status=400)

    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks()

        session = next(
            (s for s in POOL.sessions if s.uuid == session_id),
            None,
        )

        if session is None:
            print(f"[ALIVE] Unknown session: {session_id}")
            return web.json_response({
                "status": "error",
                "message": "Unknown session",
            }, status=404)

        session.last_alive = time.time()

    readable_time = datetime.fromtimestamp(session.last_alive).strftime('%Y-%m-%d %H:%M:%S')

    print(f"[ALIVE] Session '{session_id}' updated last_alive to {readable_time}")

    return web.json_response({
        "status": "alive",
        "session_id": session_id,
    })

async def handle_lock(request):
    # Function for locking dependencies according to "all or nothing"-principle

    # 1. Payload parsing & validation
    static_raw, dynamic_raw = await parse_lock_request(request)

    if isinstance(static_raw, web.Response):
        return static_raw

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
        POOL.check_and_reclaim_expired_locks()

        # 4. Check availability & assign dependencies
        lock_data = await POOL.resolve_and_assign_dependencies(static_deps, dynamic_options)

    # 5. Unavailable: Return immediately without acquiring any locks
    if lock_data["status"] == "unavailable":
        print(f"[SERVER] Some Dependencies from lock-request are unavailable.")
        return web.json_response(lock_data, status=409)

    # 6. Lock successfully acquired
    session = lock_data["session"]
    assigned_dynamic = lock_data["assigned_dynamic"]

    print(f"[SERVER] Lock granted for {sorted(session.devices)}. (Session: {session.uuid})")

    # 7. Return lock confirmation
    return web.json_response({"status": "locked",
                              "session_id": session.uuid,
                              "devices": sorted(session.devices),
                              "deps_assigned": assigned_dynamic})

async def handle_free(request):
    # Release all devices belonging to a session

    # 1. Payload parsing & validation
    session_id = await parse_free_request(request)

    if isinstance(session_id, web.Response):
        return session_id

    # 2. Clean up expired sessions
    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks()
        result = POOL.release_session(session_id)

    # 3. Return result
    if result["status"] == "error":
        return web.json_response(result, status=404)

    return web.json_response(result, status=200)


if __name__ == "__main__":
    app = web.Application()
    app.add_routes([
        web.get('/status', handle_status),
        web.post('/lock', handle_lock),
        web.post('/free', handle_free),
        web.post('/alive', handle_alive),
    ])
    print("Medusa lock-server auf http://127.0.0.1:8080 ...")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(app, host='127.0.0.1', port=8080)