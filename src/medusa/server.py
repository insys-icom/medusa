from aiohttp import web
import asyncio
import json
import time
import uuid

class DepLockManager:
    def __init__(self):
        # structure: {"device_name": {"device": "...", "client_id": "...", "timestamp": ...}}
        self.active_locks = {}              # which devices are locked
        self.device_queues = {}             # which devices have a queue for waiting clients
        self.server_mutex = asyncio.Lock()  # mutex-protection

    def check_and_reclaim_expired_locks(self, timeout_seconds=600):
        # Cleans up expired locks and dead queue-items
        current_time = time.time()

        # Get expired locks
        expired_devices = []
        for device, meta in list(self.active_locks.items()):
            lock_time = meta.get("timestamp", 0)

            if (current_time - lock_time) > timeout_seconds:
                expired_devices.append((device, meta.get("client_id")))

        # Force unlock
        for device, client_id in expired_devices:
            self.active_locks.pop(device, None)
            print(f"[TTL-EXPIRED] Device '{device}' reached TTL limit! Force-Released locks for crashed client '{client_id}'!")

            queue = self.get_or_create_queue(device)
            if not queue.empty():
                _, next_event, _ = queue._queue[0]
                next_event.set()

        # Cleanup Queues
        for device, queue in list(self.device_queues.items()):
            new_queue_items = []
            removed_any = False

            while not queue.empty():
                item = queue.get_nowait()

                # Unpack 3-tupel (client, event, timestamp)
                if isinstance(item, (tuple, list)) and len(item) == 3:
                    c_id, event_obj, entry_time = item

                    # If item is too old -> delete it
                    if (current_time - entry_time) > timeout_seconds:
                        removed_any = True
                        print(f"[TTL-QUEUE-CLEANUP] Dropped dead client '{c_id}' from queue '{device}' due to timeout.")
                    else:
                        new_queue_items.append(item)
                else:
                    # Fallback for old datastructures
                    new_queue_items.append(item)

            # Valid items back into queue
            for item in new_queue_items:
                queue.put_nowait(item)

    async def cleanup_session(self, session_uuid: str):
        async with self.server_mutex:
            self.remove_client_from_all_queues(session_uuid)
            self.release_all_locks_for_client(session_uuid)
            self.trigger_cascade_wakeup()

    def get_or_create_queue(self, device):
        # Creates a queue for device if there is none existing already & returns it
        if device not in self.device_queues:
            self.device_queues[device] = asyncio.Queue()
            print(f"[SERVER] Dynamically initialized a new queue for device: '{device}'.")

        return self.device_queues[device]

    def get_queue_waiters(self, device):
        # Gets names of waiting clients from queue
        waiters = []

        if device in self.device_queues:
            queue = self.device_queues[device]

            for item in queue._queue:
                try:
                    # Case 1: It is a tuple / list (client_id, event)
                    if isinstance(item, (tuple, list)) and len(item) == 3:
                        raw_uuid = item[0]

                        if len(raw_uuid) > 9 and raw_uuid[-9] == "_":
                            clean_name = raw_uuid[:-9]
                        else:
                            clean_name = raw_uuid

                        waiters.append(clean_name)

                    # Case 2: It is just a string (only client_id)
                    else:
                        waiters.append(str(item))
                except Exception:
                    waiters.append("unknown_waiter")
        return waiters

    def release_all_locks_for_client(self, session_uuid):
        # Find all devices which are currently owned by a specific client
        devices_to_free = []

        for device, meta in list(self.active_locks.items()):
            if isinstance(meta, dict) and meta.get("client_id") == session_uuid:
                devices_to_free.append(device)

        for device in devices_to_free:
            self.active_locks.pop(device, None)
            print(f"[CLEANUP-LOCKS] Connection severed! Granted locks on device '{device}' held by chrashed client '{session_uuid}'.")

    def remove_client_from_all_queues(self, session_uuid):
        # Removes client from all queues in which he is waiting for devices
        for device, queue in self.device_queues.items():
            new_queue_items = []
            removed_any = False

            while not queue.empty():
                item = queue.get_nowait()       # 3-tuple-structure: (client, event, timestamp)

                if isinstance(item, (tuple, list)) and len(item) == 3:
                    if item[0] == session_uuid:
                        removed_any = True
                        continue
                else:
                    if item == session_uuid:
                        removed_any = True
                        continue

                new_queue_items.append(item)

            # put surviving clients back into FIFO queue
            for item in new_queue_items:
                queue.put_nowait(item)

            if removed_any:
                print(f"[CLEANUP-QUEUE] Session '{session_uuid}' has been removed from queue for device '{device}'.")

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

    def trigger_cascade_wakeup(self):
        # Wakes up every waiting client in queue
        print(f"[SERVER-CASCADE] Triggering global wake-up for all pending clients.")

        for device, queue in self.device_queues.items():
            for item in queue._queue:
                if isinstance(item, (tuple, list)) and len(item) == 3:
                    client_event = item[1]
                    client_event.set()      # ring alarm clock

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
            staus=400)

    return client_id, static_raw, dynamic_raw


async def handle_force_unlock(request):
    # If a user chrashed, the server is still able to unlock their holded locks
    # Security Check: Is requester = admin?
    ADMIN_TOKEN = "INSYS_ADMIN_2026"
    client_token = request.headers.get("X-Admin-Token")

    if client_token != ADMIN_TOKEN:
        print(f"[SECURITY ALERT] Unauthorized attempt to FORCE_UNLOCK by '{client_token}'!")
        return web.json_response({
            "status": "error",
            "message": "Unauthorised attempt to force unlock devices."})

    # PAYLOAD PARSING & VALIDATION
    try:
        body = await request.json()
        devices = body.get("devices", [])
        reason = body.get("reason", "Manual admin intervention")
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"},
            status=400)

    if not devices:
        return web.json_response({
            "status": "error",
            "message": "Missing devices field"},
            status=400)

    POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

    async with POOL.server_mutex:
        print(f"\n[ADMIN-ACTION] FORCE_UNLOCK triggered for {devices}.")

        events_to_trigger = set()

        for device in devices:
            if device in POOL.active_locks:
                meta = POOL.active_locks.pop(device)
                if isinstance(meta, dict):
                    old_owner = meta.get("client_id")
                    print(f"    -> Successfully evicted client '{old_owner}' from hardware '{device}'.")

                    # if old_owner is online -> stop connection securely
                    lock_ev = meta.get("unlock_event")
                    if lock_ev:
                        events_to_trigger.add(lock_ev)
            else:
                print(f"  -> Device '{device}' was not locked. Skipping eviction.")

            for ev in events_to_trigger:
                ev.set()

        POOL.trigger_global_cascade_wakeup()

    return web.json_response({
        "status": "force_unlock_complete",
        "device": device })

async def handle_watch(request):
    # Function to keep connection to client open while its session owns locks
    session_uuid = None
    try:
        body = await request.json()
        session_uuid = body.get("session_uuid")
        if not session_uuid:
            return web.json_response({"status": "error", "message": "Missing session_uuid"}, status=400)
        print(f"[SERVER] Watchdog connection established for session '{session_uuid}'.")

        while True:
            # Check whether this session still owns any locks.
            async with POOL.server_mutex:
                still_owns_locks = any(meta.get("client_id") == session_uuid for meta in POOL.active_locks.values())

             # The session was released normally, i.e. through /unlock.
            if not still_owns_locks:
                print(f"[SERVER] Watchdog session '{session_uuid}' has no remaining locks. Closing normally.")
                return web.json_response({"status": "released", "session_uuid": session_uuid})

            # Locks still exist: check whether the client disconnected.
            if request.transport is None or request.transport.is_closing():
                print(f"[NETWORK-GUARD] Watchdog connection lost for session '{session_uuid}'.")
                raise asyncio.CancelledError
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        if session_uuid is not None:
            async with POOL.server_mutex:
                # Recheck ownership to avoid duplicate cleanup if an
                # unlock happened just as the connection was cancelled.
                still_owns_locks = any(meta.get("client_id") == session_uuid for meta in POOL.active_locks.values())

                if still_owns_locks:
                    print(f"[DISCONNECT PROTECTION] Releasing locks for session '{session_uuid}'.")
                    POOL.remove_client_from_all_queues(session_uuid)
                    POOL.release_all_locks_for_client(session_uuid)
                    POOL.trigger_cascade_wakeup()
                else:
                    print(f"[SERVER] Session '{session_uuid}' already released its locks.")
            raise

async def handle_lock(request):
    # Function for locking dependencies according to "all or nothing"-principle

    session_uuid = None
    active_lock_event = None

    # 1. Payload parsing & validation
    client_id, static_raw, dynamic_raw = await parse_lock_request(request)

    # 2. Resolve dependencies
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

    # 3. Preventive Cleanup: Prevents orphaned locks from blocking the system
    POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

    unavailable_message = None
    assigned_dynamic = None
    concrete_devices = set()

    # 4. Check Availability & assign dependencies
    async with POOL.server_mutex:
        print(f"[SERVER] Request for locking received from '{client_id}': static={sorted(static_deps)}, dynamic={dynamic_raw}")
        busy_devices = set(POOL.active_locks)
        busy_static = static_deps & busy_devices

        if busy_static:
            unavailable_message = (f"Static dependencies are busy: {sorted(busy_static)}")
        else:
            available_options = {name: options - busy_devices - static_deps
                                for name, options in dynamic_options.items()}

            assigned_dynamic = POOL.resolve_dynamic_dependencies(available_options)

            if assigned_dynamic is None:
                unavailable_message = ("No distinct set of available devices statisfies all dynamic dependencies.")
            else:
                concrete_devices= (static_deps | set(assigned_dynamic.values()))

                # All devices are free
                session_uuid = (f"{client_id}_{uuid.uuid4().hex[:8]}")
                active_lock_event = asyncio.Event()

                for device in concrete_devices:
                    POOL. active_locks[device] = {
                        "device": device,
                        "client_id": session_uuid,
                        "timestamp": time.time(),
                        "unlock_event": active_lock_event
                    }

                print(f"[SERVER] Lock granted to '{client_id}' for {sorted(concrete_devices)} (session {session_uuid}).")

    # 5. Unavailable: Return immediately without acquiring any locks
    if unavailable_message is not None:
        print(f"[SERVER] Request from '{client_id}' unavailable: {unavailable_message}")
        return web.json_response({
            "status": "unavailable",
            "message": unavailable_message},
            status=409)

    assert session_uuid is not None
    assert active_lock_event is not None
    assert assigned_dynamic is not None

    # 6. Stream confirmation, then keep this connection open
    stream = web.StreamResponse(status=200, headers={
        "Content-Type": "application/x-ndjson",
        "Cache-Control": "no-cache"})

    try:
        await stream.prepare(request)
        confirmation = {
            "status": "locked",
            "session_uuid": session_uuid,
            "devices": sorted(concrete_devices),
            "deps_assigned": assigned_dynamic
        }
        await stream.write((json.dumps(confirmation) + "\n").encode("utf-8"))
        print(f"[SERVER] Lock confirmation sent for session '{session_uuid}'. Monitoring connection...")

        # Keep connection open while the session owns locks
        while True:
            try:
                await asyncio.wait_for(active_lock_event.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                if request.transport is None or request.transport.is_closing():
                    raise asyncio.CancelledError

                # Small heartbeat helps detect a closed client connection
                await stream.write(b"\n")
                continue

            async with POOL.server_mutex:
                still_owned = any(
                    meta.get("client_id") == session_uuid
                    for meta in POOL.active_locks.values()
                )

            if not still_owned:
                break

            # Some locks remain, i.e. after a partial force-unlock
            active_lock_event.clear()

        await stream.write_eof()
        return stream

    # 7. CLEANUP WHEN CLIENT-CONNECTION ABORTED / CRASHED
    except asyncio.CancelledError:      # Client lost connection to server (Ctrl+C)
        print(f"[SESSION-END] Client connection closed for '{session_uuid}'. Releasing any remaining locks.")
        await POOL.cleanup_session(session_uuid)
        raise

    except (ConnectionResetError, OSError) as exc:
        print(f"[SESSION-END] Connection lost for '{session_uuid}': {exc}")
        await POOL.cleanup_session(session_uuid)
        return stream

async def handle_unlock(request):
    # Function for unlocking currently locked devices / dependencies
    # PAYLOAD PARSING & VALIDATION
    try:
        body = await request.json()
        devices = list(set(body.get("devices", [])))
        client_id = body.get("client_id")
    except Exception:
        return web.json_response({
            "status": "error",
            "message": "Malformed JSON payload"},
            status=400)

    if not devices or not client_id:
        return web.json_response({
            "status": "error",
            "message": "Missing devices or client_id fields"},
            status=400)

    POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

    async with POOL.server_mutex:
        print(f"[SERVER] Request for unlocking batch {devices} received from '{client_id}'. Checking status ...")

        # 1. Validation: check ownership across all devices
        for device in devices:
            if device in POOL.active_locks:
                current_owner = POOL.active_locks[device].get("client_id")

                # if devices current_owner is someone else -> abort
                if current_owner != client_id:
                    print(f"    -> [DENIED] '{client_id}' tried to unlock '{device}', but it belongs to '{current_owner}'!")
                    return web.json_response({
                        "status": "rejected",
                        "message": f"Device '{device}' is locked by '{current_owner}'. You cannot unlock it."},
                        status=403)

        # 2. Processing: release devices if validation was successful
        events_to_trigger = set()

        for device in devices:
            if device in POOL.active_locks:
                meta = POOL.active_locks.pop(device)
                print(f"[SERVER] Freed: '{device}' is free again.")

                # get event of holding client
                lock_ev = meta.get("unlock_event")
                if lock_ev:
                    events_to_trigger.add(lock_ev)

            for ev in events_to_trigger:
                ev.set()

        POOL.trigger_cascade_wakeup()

    return web.json_response({
        "status": "unlocked",
        "devices": devices})

async def handle_status(request):
    # Function for showing which devices are locked by which user & since when
    # Shows a CLI-Dashboard with locked devices & their (if existing) queues
    print(f"[SERVER] Global status dashboard request received. Preparing data ...")

    POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

    safe_active_locks = {}
    for device, meta in POOL.active_locks.items():
        if isinstance(meta, dict):
            raw_uuid = meta.get("client_id", "")

            if len(raw_uuid) > 9 and raw_uuid[-9] == "_":
                clean_name = raw_uuid[:-9]
            else:
                clean_name = raw_uuid

            safe_active_locks[device] = {"device": meta.get("device"),
                                         "client_id": clean_name,
                                         "timestamp": meta.get("timestamp")}

    status_report = {
        "active_locks": safe_active_locks,
        "queues": {}
    }

    for device in list(POOL.device_queues.keys()):
        waiters = POOL.get_queue_waiters(device)
        if waiters:
            status_report["queues"][device] = waiters

    return web.json_response(status_report)


if __name__ == "__main__":
    app = web.Application()
    app.add_routes([
        web.get('/status', handle_status),
        web.post('/lock', handle_lock),
        web.post('/unlock', handle_unlock),
        web.post('/force_unlock', handle_force_unlock),
        web.post('/watch', handle_watch)
    ])
    print("Medusa lock-server auf http://127.0.0.1:8080 ...")
    web.run_app(app, host='127.0.0.1', port=8080)