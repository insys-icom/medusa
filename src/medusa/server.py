import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiohttp import web


@dataclass(eq=False)
class Session:
    uuid: str
    devices: set[str] = field(default_factory=set)
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_alive: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def _create_session(cls, devices: set[str]) -> "Session":
        """Create a new session"""

        now = datetime.now(UTC)

        return cls(
            uuid=str(uuid.uuid4()),
            devices=set(devices),
            since=now,
            last_alive=now,
        )


# TODO(SZI): Go through functions and move details to separate functions where
#            necessary/sensible
class DepLockManager:
    def __init__(self):
        """Initialize session and device lock management.

        server_mutex protects concurrent access to the session and lock state.
        sessions maps session IDs to active sessions.
        active_locks maps device IDs to their owning session IDs.
        """
        self.server_mutex = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self.active_locks: dict[str, str] = {}

    def _get_expired_sessions(self, timeout_seconds: int) -> list[Session]:
        """Return sessions whose keepalive timeout has expired"""

        current_time = datetime.now(UTC)

        return [
            session
            for session in self.sessions.values()
            if current_time - session.last_alive
            > timedelta(seconds=timeout_seconds)
        ]

    def _reclaim_session(self, session: Session) -> None:
        """Release all devices and remove an expired session"""

        released_devices = sorted(session.devices)

        for device in session.devices:
            self.active_locks.pop(session.uuid, None)

        print(
            f"[CLEANUP] Released locks of session '{session.uuid}': {released_devices}."
        )

    def _check_and_reclaim_expired_locks(self, timeout_seconds=30):
        """Cleanup expired sessions and their locks"""

        expired_sessions = self._get_expired_sessions(timeout_seconds)

        for session in expired_sessions:
            self._reclaim_session(session)

    def _release_devices(
        self, session: Session, devices: set[str]
    ) -> list[str]:
        """Release selected devices from a session and the active locks."""

        released_devices = sorted(devices)

        for device in devices:
            session.devices.remove(device)
            self.active_locks.pop(device, None)

        return released_devices

    def _release_session(
        self,
        session_id: str,
        devices: set[str] | None = None,
    ) -> dict:
        """Find a session and release all or selected devices"""

        # 1. Find session
        session = self.sessions.get(session_id)

        if session is None:
            return {
                "status": "error",
                "message": "Unknown session.",
            }

        # 2. Determine which devices should be released
        devices_to_release = (
            set(session.devices) if devices is None else devices
        )

        # 3. Ensure all requested devices belong to this session
        if not devices_to_release.issubset(session.devices):
            return {
                "status": "rejected",
                "message": "One or more devices do not belong to this session.",
            }

        # 4. Release devices
        released_devices = self._release_devices(session, devices_to_release)

        # Full release if no specific devices were provided
        close_session = devices is None

        if close_session:
            del self.sessions[session_id]

        print(
            f"[FREE] Released devices {released_devices} from session '{session_id}'."
        )

        # 5. Return
        return {
            "status": "closed" if close_session else "freed",
            "session_id": session_id,
            "devices": released_devices,
        }

    def _resolve_dynamic_dependencies(
        self, available_options: dict[str, set[str]]
    ) -> dict[str, str] | None:
        """Assign a distinct available device to every dynamic dependency"""

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

    def _resolve_and_assign_dependencies(
        self,
        session_id: str | None,
        static_deps: set[str],
        dynamic_raw: dict[str, set[str]],
    ) -> dict:
        """Resolve dependencies and create a session if all devices are available"""

        print(
            f"[SERVER] Lock request received: session={session_id} static={sorted(static_deps)}, dynamic={dynamic_raw}"
        )
        session = None

        # 1. Find existing session, if a UUID was provided
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
        assigned_dynamic = self._resolve_dynamic_dependencies(
            available_options
        )

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
            session = Session._create_session(new_deps)
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


class LockServer:
    def __init__(self):
        self.pool = DepLockManager()

    async def _cleanup_loop(self):
        """Clean up expired locks every 5 seconds"""

        while True:
            async with self.pool.server_mutex:
                self.pool._check_and_reclaim_expired_locks(timeout_seconds=30)

            await asyncio.sleep(5)

    async def _on_startup(self, app):
        """Create cleanup-task when the application was started"""

        app["cleanup_task"] = asyncio.create_task(self._cleanup_loop())

    async def _on_cleanup(self, app):
        """Stop cleanup-task when application ends"""

        app["cleanup_task"].cancel()

        try:
            await app["cleanup_task"]
        except asyncio.CancelledError:
            pass

    async def _parse_lock_request(self, request_data):
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

        if not isinstance(static_raw, list) or not isinstance(
            dynamic_raw, dict
        ):
            return web.json_response(
                {
                    "status": "error",
                    "message": "Missing / invalid request fields",
                },
                status=400,
            )

        if any(
            not isinstance(device, str) or not device for device in static_raw
        ) or any(
            not isinstance(name, str)
            or not name
            or not isinstance(options, list)
            or any(
                not isinstance(device, str) or not device for device in options
            )
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

    def _prepare_lock_dependencies(
        self,
        static_raw: list[str],
        dynamic_raw: dict[str, list[str]],
    ) -> tuple[set[str], dict[str, set[str]]] | web.Response:
        """Prepare and validate static and dynamic dependencies."""

        static_deps = set(static_raw)

        if len(static_deps) != len(static_raw):
            return web.json_response(
                {
                    "status": "error",
                    "message": "Duplicate static dependencies",
                },
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

        return static_deps, dynamic_options

    async def _parse_free_request(self, request):
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
                {
                    "status": "error",
                    "message": "Missing or invalid session_id",
                },
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

    async def _parse_alive_request(self, request):
        """Parse and validate a keepalive request"""

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "messaqe": "Malformed JSON payload."},
                status=400,
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"status": "error", "messaqe": "Invalid request body."},
                status=400,
            )

        session_id = body.get("session_id")

        if not isinstance(session_id, str) or not session_id:
            return web.json_response(
                {
                    "status": "error",
                    "messaqe": "Missing or invalid session_id.",
                },
                status=400,
            )

        return session_id

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        """Format a datetime in the local timezone"""
        return value.replace(microsecond=0).astimezone().isoformat()

    async def _handle_status(self, request):
        """Shows all active sessions and their locked devices"""

        print("[SERVER] Global status dashboard request received.")

        async with self.pool.server_mutex:
            self.pool._check_and_reclaim_expired_locks()

            sessions = [
                {
                    "session_id": session.uuid,
                    "devices": sorted(session.devices),
                    "since": self._format_datetime(session.since),
                    "last_alive": self._format_datetime(session.last_alive),
                }
                for session in self.pool.sessions.values()
            ]

        return web.json_response({"sessions": sessions})

    async def _handle_alive(self, request):
        """Update the last_alive timestamp of an existing session"""

        session_id = await self._parse_alive_request(request)

        if isinstance(session_id, web.Response):
            return session_id

        async with self.pool.server_mutex:
            self.pool._check_and_reclaim_expired_locks()

            session = self.pool.sessions.get(session_id)

            if session is None:
                print(f"[ALIVE] Unknown session: {session_id}")
                return web.json_response(
                    {
                        "status": "error",
                        "message": "Unknown session",
                    },
                    status=404,
                )

            session.last_alive = datetime.now(UTC)
            last_alive = session.last_alive

        readable_time = self._format_datetime(last_alive)

        print(
            f"[ALIVE] Session '{session_id}' updated last_alive to {readable_time}"
        )

        return web.json_response(
            {
                "status": "alive",
                "session_id": session_id,
            }
        )

    async def _handle_lock(self, request):
        """Function for locking dependencies according to "all or nothing"-principle"""

        # 1. Payload parsing & validation
        parsed = await self._parse_lock_request(request)

        if isinstance(parsed, web.Response):
            return parsed

        session_id, static_raw, dynamic_raw = parsed

        # 2. Prepare dependencies
        prepared = self._prepare_lock_dependencies(static_raw, dynamic_raw)

        if isinstance(prepared, web.Response):
            return prepared

        static_deps, dynamic_options = prepared

        async with self.pool.server_mutex:
            # 3. Preventive Cleanup: Prevents orphaned locks from blocking the system
            self.pool._check_and_reclaim_expired_locks()

            # 4. Check availability & assign dependencies
            lock_data = self.pool._resolve_and_assign_dependencies(
                session_id,
                static_deps,
                dynamic_options,
            )

        # 5. Unavailable: Return immediately without acquiring any locks
        if lock_data["status"] == "unavailable":
            print(
                "[SERVER] Some Dependencies from lock-request are unavailable."
            )
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

    async def _handle_free(self, request):
        """Release all or selected devices belonging to a session"""

        # 1. Payload parsing & validation
        parsed = await self._parse_free_request(request)

        if isinstance(parsed, web.Response):
            return parsed

        session_id, devices = parsed

        # 2. Clean up expired sessions and release requested devices
        async with self.pool.server_mutex:
            self.pool._check_and_reclaim_expired_locks()
            result = self.pool._release_session(session_id, devices)

        # 3. Return result
        if result["status"] == "error":
            return web.json_response(result, status=404)

        if result["status"] == "rejected":
            return web.json_response(result, status=409)

        return web.json_response(result, status=200)

    def _create_app(self):
        """Create and configure the web application"""

        app = web.Application()

        app.add_routes(
            [
                web.get("/status", self._handle_status),
                web.post("/lock", self._handle_lock),
                web.post("/free", self._handle_free),
                web.post("/alive", self._handle_alive),
            ]
        )

        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

        return app

    def run(self):
        """Run the lock server"""
        web.run_app(self._create_app(), host="127.0.0.1", port=8080)


def start_server():
    """Global entry point: Start the lock server"""

    print("Medusa lock-server auf http://127.0.0.1:8080 ...")
    LockServer().run()
