import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiohttp import web


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


# TODO(SZI): Make internal functions actually internal
# TODO(SZI): Go through functions and move details to separate functions where
#            necessary/sensible
class DepLockManager:
    def __init__(self):
        self.server_mutex = asyncio.Lock()  # mutex-protection
        self.sessions: dict[
            str, Session
        ] = {}  # dict of all active sessions (session_id -> Session)
        self.active_locks: dict[
            str, str
        ] = {}  # dict of all active locks (Device -> session_id)

    def check_and_reclaim_expired_locks(self, timeout_seconds=30):
        # Clean up expired sessions

        current_time = time.time()

        # Get expired sessions
        expired_sessions = [
            session
            for session in self.sessions.values()
            if current_time - session.last_alive > timeout_seconds
        ]

        # Cleanup expired sessions
        for session in expired_sessions:
            released_devices = sorted(session.devices)

            # Remove all devices belonging to this session
            for device in session.devices:
                self.active_locks.pop(device, None)

            # Remove the session itself
            self.sessions.pop(session.uuid, None)

            print(
                f"[CLEANUP] Released locks of session '{session.uuid}': {sorted(released_devices)}."
            )

    def release_session(
        self,
        session_id: str,
        devices: set[str] | None = None,
    ) -> dict:
        # Find a session and release all or selected devices

        # Find session
        session = self.sessions.get(session_id)

        if session is None:
            return {
                "status": "error",
                "message": "Unknown session.",
            }

        # Determine which devices should be released
        devices_to_release = (
            set(session.devices) if devices is None else devices
        )

        # Ensure all requested devices belong to this session
        if not devices_to_release.issubset(session.devices):
            return {
                "status": "rejected",
                "message": "One or more devices do not belong to this session.",
            }

        released_devices = sorted(devices_to_release)

        # Remove devices from session and active locks
        for device in devices_to_release:
            session.devices.remove(device)
            self.active_locks.pop(device, None)

        # Full release if no specific devices were provided
        close_session = devices is None

        if close_session:
            del self.sessions[session_id]

        print(
            f"[FREE] Released devices {released_devices} from session '{session_id}'."
        )

        return {
            "status": "closed" if close_session else "freed",
            "session_id": session_id,
            "devices": released_devices,
        }

    def resolve_dynamic_dependencies(
        self, available_options: dict[str, set[str]]
    ) -> dict[str, str] | None:
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

        return {dep_name: device for device, dep_name in option_owner.items()}

    def resolve_and_assign_dependencies(
        self,
        session_id: str | None,
        static_deps: set[str],
        dynamic_raw: dict[str, set[str]],
    ) -> dict:
        # Resolve dependencies and create a session if all devices are available

        print(
            f"[SERVER] Lock request received: session={session_id} static={sorted(static_deps)}, dynamic={dynamic_raw}"
        )

        # 1. Find existing session, if a UUID was provided
        session = None

        if session_id is not None:
            session = self.sessions.get(session_id)

            if session is None:
                return {"status": "error", "message": "Unknown session."}

        # 2. Determine devices already held by this session
        own_deps = session.devices if session else set()

        # 3. Determine devices held by other sessions
        busy_deps = set(self.active_locks) - own_deps

        # 4. Check if requested static dependencies are available
        busy_static = static_deps & busy_deps

        if busy_static:
            return {
                "status": "unavailable",
                "message": f"Static dependencies are busy: {sorted(busy_static)}",
            }

        # 5. Remove busy devices and static dependencies
        available_options = {
            name: options - busy_deps - static_deps
            for name, options in dynamic_raw.items()
        }

        # 6. Resolve dynamic dependencies
        assigned_dynamic = self.resolve_dynamic_dependencies(available_options)

        if assigned_dynamic is None:
            return {
                "status": "unavailable",
                "message": "No distinct set of devices satisfies all dynamic dependencies.",
            }

        # 7. Combine static and assigned dynamic dependencies
        requested_deps = static_deps | set(assigned_dynamic.values())

        # 8. Determine which devices are actually new
        new_deps = requested_deps - own_deps

        # 9. Create a new session if no UUID was provided
        if session is None:
            session = Session.create_session(new_deps)
            self.sessions[session.uuid] = session
        else:
            # Extend existing session
            session.devices.update(new_deps)

        # 10. Register newly acquired devices
        for device in new_deps:
            self.active_locks[device] = session.uuid

        # 11. Return the result
        return {
            "status": "locked",
            "session": session,
            "assigned_dynamic": assigned_dynamic,
            "concrete_deps": session.devices.copy(),
        }


# TODO(SZI): Consider grouping all of this into a class to avoid global
#            variables and make things more readable
POOL = DepLockManager()


async def cleanup_loop():
    """Clean up expired locks every 5 seconds"""

    while True:
        async with POOL.server_mutex:
            POOL.check_and_reclaim_expired_locks(timeout_seconds=30)

        await asyncio.sleep(5)


async def on_startup(app):
    """Create cleanup-task when the application was started"""

    app["cleanup_task"] = asyncio.create_task(cleanup_loop())


async def on_cleanup(app):
    """Stop cleanup-task when application ends"""

    app["cleanup_task"].cancel()

    try:
        await app["cleanup_task"]
    except asyncio.CancelledError:
        pass


async def parse_lock_request(request_data):
    """Parse and validate a lock request containing static and dynamic dependencies"""
    try:
        body = await request_data.json()
    except Exception:
        return web.json_response(
            {"status": "error", "message": "Malformed JSON payload"},
            status=400,
        )

    if not isinstance(body, dict):
        return web.json_response(
            {"status": "error", "message": "Invalid request body"},
            status=400,
        )

    session_id = body.get("session_id")
    static_raw = body.get("deps_static", [])
    dynamic_raw = body.get("deps_dynamic", {})

    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        return web.json_response(
            {"status": "error", "message": "Invalid session_id"},
            status=400,
        )

    if not isinstance(static_raw, list) or not isinstance(dynamic_raw, dict):
        return web.json_response(
            {"status": "error", "message": "Missing / invalid request fields"},
            status=400,
        )

    if any(
        not isinstance(device, str) or not device for device in static_raw
    ) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(options, list)
        or any(not isinstance(device, str) or not device for device in options)
        for name, options in dynamic_raw.items()
    ):
        return web.json_response(
            {
                "status": "error",
                "message": "Invalid dependency names or options",
            },
            status=400,
        )

    return session_id, static_raw, dynamic_raw


async def parse_free_request(request):
    """Parse and validate a session_id and optional devices from a free request"""

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"status": "error", "message": "Malformed JSON payload"},
            status=400,
        )

    if not isinstance(body, dict):
        return web.json_response(
            {"status": "error", "message": "Invalid request body"},
            status=400,
        )

    session_id = body.get("session_id")

    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"status": "error", "message": "Missing or invalid session_id"},
            status=400,
        )

    devices = body.get("devices")

    if devices is not None:
        if not isinstance(devices, list) or not all(
            isinstance(device, str) and device for device in devices
        ):
            return web.json_response(
                {"status": "error", "message": "Invalid devices"},
                status=400,
            )

        if len(devices) != len(set(devices)):
            return web.json_response(
                {"status": "error", "message": "Duplicate devices"},
                status=400,
            )

        devices = set(devices)

    return session_id, devices


async def handle_status(request):
    """Shows all active sessions and their locked devices"""

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
            for session in POOL.sessions.values()
        ]

    return web.json_response({"sessions": sessions})


async def handle_alive(request):
    """Update the last_alive timestamp of an existing session"""

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"status": "error", "message": "Malformed JSON payload"},
            status=400,
        )

    if not isinstance(body, dict):
        return web.json_response(
            {"status": "error", "message": "Invalid request body"},
            status=400,
        )

    session_id = body.get("session_id")

    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"status": "error", "message": "Missing or invalid session_id"},
            status=400,
        )

    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks()

        session = POOL.sessions.get(session_id)

        if session is None:
            print(f"[ALIVE] Unknown session: {session_id}")
            return web.json_response(
                {
                    "status": "error",
                    "message": "Unknown session",
                },
                status=404,
            )

        session.last_alive = time.time()
        last_alive = session.last_alive

    readable_time = datetime.fromtimestamp(last_alive, tz=UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[ALIVE] Session '{session_id}' updated last_alive to {readable_time}"
    )

    return web.json_response(
        {
            "status": "alive",
            "session_id": session_id,
        }
    )


async def handle_lock(request):
    """Function for locking dependencies according to "all or nothing"-principle"""

    # 1. Payload parsing & validation
    parsed = await parse_lock_request(request)

    if isinstance(parsed, web.Response):
        return parsed

    session_id, static_raw, dynamic_raw = parsed

    # 2. Prepare dependencies
    static_deps = set(static_raw)
    if len(static_deps) != len(static_raw):
        return web.json_response(
            {"status": "error", "message": "Duplicate static dependencies"},
            status=400,
        )

    dynamic_options = {
        name: set(options) - static_deps
        for name, options in dynamic_raw.items()
    }
    if not static_deps and not dynamic_options:
        return web.json_response(
            {"status": "error", "message": "No dependencies supplied"},
            status=400,
        )

    async with POOL.server_mutex:
        # 3. Preventive Cleanup: Prevents orphaned locks from blocking the system
        POOL.check_and_reclaim_expired_locks()

        # 4. Check availability & assign dependencies
        lock_data = POOL.resolve_and_assign_dependencies(
            session_id,
            static_deps,
            dynamic_options,
        )

    # 5. Unavailable: Return immediately without acquiring any locks
    if lock_data["status"] == "unavailable":
        print("[SERVER] Some Dependencies from lock-request are unavailable.")
        return web.json_response(lock_data, status=409)

    if lock_data["status"] == "error":
        return web.json_response(lock_data, status=404)

    # 6. Lock successfully acquired
    session = lock_data["session"]
    assigned_dynamic = lock_data["assigned_dynamic"]

    print(
        f"[SERVER] Lock granted for {sorted(session.devices)}. (Session: {session.uuid})"
    )

    # 7. Return lock confirmation
    return web.json_response(
        {
            "status": "locked",
            "session_id": session.uuid,
            "devices": sorted(session.devices),
            "deps_assigned": assigned_dynamic,
        }
    )


async def handle_free(request):
    """Release all or selected devices belonging to a session"""

    # 1. Payload parsing & validation
    parsed = await parse_free_request(request)

    if isinstance(parsed, web.Response):
        return parsed

    session_id, devices = parsed

    # 2. Clean up expired sessions and release requested devices
    async with POOL.server_mutex:
        POOL.check_and_reclaim_expired_locks()
        result = POOL.release_session(session_id, devices)

    # 3. Return result
    if result["status"] == "error":
        return web.json_response(result, status=404)

    if result["status"] == "rejected":
        return web.json_response(result, status=409)

    return web.json_response(result, status=200)


def create_app():
    """Create and configure the web application."""

    app = web.Application()

    app.add_routes(
        [
            web.get("/status", handle_status),
            web.post("/lock", handle_lock),
            web.post("/free", handle_free),
            web.post("/alive", handle_alive),
        ]
    )

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


def start_server():
    """Start the Medusa lock-server."""

    print("Medusa lock-server auf http://127.0.0.1:8080 ...")
    web.run_app(create_app(), host="127.0.0.1", port=8080)


# TODO(SZI): Clean up
if __name__ == "__main__":
    start_server()
