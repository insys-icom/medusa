from aiohttp import web
import asyncio
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

    def trigger_cascade_wakeup(self):
        # Wakes up every waiting client in queue
        print(f"[SERVER-CASCADE] Triggering global wake-up for all pending clients.")

        for device, queue in self.device_queues.items():
            for item in queue._queue:
                if isinstance(item, (tuple, list)) and len(item) == 3:
                    client_event = item[1]
                    client_event.set()      # ring alarm clock

POOL = DepLockManager()


async def handle_force_unlock(request):
    # If a user chrashed, the server is still able to unlock their holded locks
    # Security Check: Is requester = admin?
    ADMIN_TOKEN = "INSYS_ADMIN_2026"
    client_token = request.headers.get("X-Admin-Token")

    if client_token != ADMIN_TOKEN:
        print(f"[SECURITY ALERT] Unauthorized attempt to FORCE_UNLOCK by '{client_token}'!")
        return web.json_response({"status": "error", "message": "Unauthorised attempt to force unlock devices."})

    # PAYLOAD PARSING & VALIDATION
    try:
        body = await request.json()
        devices = body.get("devices", [])
        reason = body.get("reason", "Manual admin intervention")
    except Exception:
        return web.json_response({"status": "error", "message": "Malformed JSON payload"}, status=400)

    if not devices:
        return web.json_response({"status": "error", "message": "Missing devices field"}, status=400)

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

    return web.json_response({"status": "force_unlock_complete", "device": device })

async def handle_lock(request):
    # Function for locking devices / dependencies according to "all or nothing"-principle
    # 1. PAYLOAD PARSING & VALIDATION
    try:
        body = await request.json()
        devices = body.get("devices", [])
        client_id = body.get("client_id")
    except Exception:
        return web.json_response({"status": "error", "message": "Malformed JSON payload"}, status=400)

    if not devices or not client_id:
        return web.json_response({"status": "error", "message": "Missing devices or client_id fields"}, status=400)

    # Generate unique session-id
    session_uuid = f"{client_id}_{uuid.uuid4().hex[:8]}"

    # Preventive cleanup: Prevents orphaned locks from blocking the system
    POOL.check_and_reclaim_expired_locks(timeout_seconds=600)

    active_lock_event = None

    # 2. IMMEDIATE AVAILABILITY CHECK
    try:
        async with POOL.server_mutex:
            print(f"[SERVER] Request for locking batch '{devices}' received from '{client_id}'. Checking status ...")
            all_free_and_no_waiters = True

            for device in devices:
                is_busy = device in POOL.active_locks
                queue = POOL.get_or_create_queue(device)

                # Is queue empty or is client next in line?
                if not queue.empty():
                    first_in_line_id = queue._queue[0][0]       # Get client_id from 3-Tuple
                    has_prior_waiter = (first_in_line_id != session_uuid)
                else:
                    has_prior_waiter = False

                if is_busy or has_prior_waiter:
                    all_free_and_no_waiters = False

            # Case A: All devices are free -> lets go!
            if all_free_and_no_waiters:
                # Creating a local event ONLY for this active lock to freeze the connection
                active_lock_event = asyncio.Event()

                for device in devices:
                    POOL.active_locks[device] = {"device": device, "client_id": session_uuid, "timestamp": time.time(), "unlock_event": active_lock_event}
                    print(f"[SERVER] Hardware '{device}' locked by '{client_id}' successfully.")

        # 3. QUEUE-LOGIC
        # Case B: At least one device is busy -> Don't lock any of the free devices
        if active_lock_event is None:
            print(f"    -> Some devices in {devices} are busy. Put client '{client_id}' into queue ...")
            my_turn_event = asyncio.Event()         # local waiting-event only for this specific client
            queue_entry_time = time.time()

            async with POOL.server_mutex:
                # Client joins queue for every device it requested
                # As soon as the last requested device becomes available, it's woken up
                for device in devices:
                    queue = POOL.get_or_create_queue(device)
                    await queue.put((session_uuid, my_turn_event, queue_entry_time))
                    print(f"[SERVER-RESERVATION] '{client_id}'  was inserted in the queue for '{device}'.")

            # HTTP-Connection freezes in long-polling
            while True:
                try:
                    # Client waits without blocking something until ANYONE of its requested devices is freed
                    await asyncio.wait_for(my_turn_event.wait(), timeout=2.0)
                    my_turn_event.clear()       # reset alarm for next round
                    print(f"[SERVER] A queue event fired for '{client_id}'! Checking total batch availability for {devices}...")

                except asyncio.TimeoutError:
                    connection_dead = False     # Client here or has lost connection?

                    if request.transport is None or request.transport.is_closing():    # request.transport shows state of connection
                        connection_dead = True
                    if hasattr(request, 'task') and request.task.cancelled():
                        connection_dead = True
                    if connection_dead:
                        print(f"\n[NETWORK-GUARD] Client '{client_id}' has lost connection (Ctrl+C or Crash)!")
                        raise asyncio.CancelledError        # To start cleanup-block

                    # If client currently has connection: wait another 2 seconds
                    continue

                # Temporary check release from queues
                async with POOL.server_mutex:
                    all_conditions_met = True

                    for device in devices:
                        is_busy = device in POOL.active_locks
                        queue = POOL.get_or_create_queue(device)

                        if not queue.empty():
                            first_id = queue._queue[0][0]
                            is_my_turn = (first_id == session_uuid)
                        else:
                            is_my_turn = True

                        if is_busy or not is_my_turn:
                            all_conditions_met = False
                            break

                    if all_conditions_met:           # Every device is free
                        active_lock_event = asyncio.Event()

                        for device in devices:
                            queue = POOL.get_or_create_queue(device)
                            new_queue_items = []        # Queue without our own item

                            while not queue.empty():
                                item = queue.get_nowait()

                                if isinstance(item, (tuple, list)) and len(item) == 3:
                                    if item[0] != session_uuid:
                                        new_queue_items.append(item)
                                else:
                                    if item != session_uuid:
                                        new_queue_items.append(item)

                            for item in new_queue_items:
                                queue.put_nowait(item)

                            POOL.active_locks[device] = {"device": device, "client_id": session_uuid, "timestamp": time.time(), "unlock_event": active_lock_event}

                        print(f"[SERVER] SUCCESS: Batch {devices} locked by '{client_id}' successfully after waiting.")
                        break
                    else:
                        print(f"    -> Batch {devices} for '{client_id}' still blocked. Defending priority position...")

        # 4. ACTIVE LOCK-GUARD / NETWORK-GUARD
        while True:
            try:
                await asyncio.wait_for(active_lock_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                connection_dead = False

                if request.transport is None or request.transport.is_closing():
                    connection_dead = True
                if hasattr(request, 'task') and request.task.cancelled():
                    connection_dead = True
                if connection_dead:
                    print(f"\n[NETWORK-GUARD] Active lock holder '{client_id}' dropped connection!")
                    raise asyncio.CancelledError
                continue

            # Event fired -> /unlock or /force_unlock
            if active_lock_event.is_set():
                async with POOL.server_mutex:
                    still_owned_count = 0

                    for device in devices:
                        if device in POOL.active_locks and POOL.active_locks[device].get("client_id") == session_uuid:
                            still_owned_count += 1

                    if still_owned_count == 0:
                        # Case A: We own nothing anymore -> close connection
                        print(f"[SERVER] Client '{client_id}' holding no remaining locks. Closing connection cleanly.")
                        break
                    else:
                        # Case B: We own some devices
                        active_lock_event.clear()
                        print(f"    -> [PARTIAL-EVICTION] '{client_id}' lost some locks, but keeps holding connection for {still_owned_count} remaining devices.")
                        continue

        return web.json_response({"status": "locked", "devices": devices})

    # 5. AUTOMATIC CLEANUP WHEN ABORT / CRASH
    except asyncio.CancelledError:      # Client lost connection to server (Ctrl+C)
        async with POOL.server_mutex:
            print(f"\n[DISCONNECT PROTECTION] Client socket connection severed for '{client_id}'!")
            POOL.remove_client_from_all_queues(session_uuid)
            POOL.release_all_locks_for_client(session_uuid)
            POOL.trigger_cascade_wakeup()
        raise

async def handle_unlock(request):
    # Function for unlocking currently locked devices / dependencies
    # PAYLOAD PARSING & VALIDATION
    try:
        body = await request.json()
        devices = list(set(body.get("devices", [])))
        client_id = body.get("client_id")
    except Exception:
        return web.json_response({"status": "error", "message": "Malformed JSON payload"}, status=400)

    if not devices or not client_id:
        return web.json_response({"status": "error", "message": "Missing devices or client_id fields"}, status=400)

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
                    return web.json_response({"status": "rejected", "message": f"Device '{device}' is locked by '{current_owner}'. You cannot unlock it."}, status=403)

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

        POOL.trigger_global_cascade_wakeup()

    return web.json_response({"status": "unlocked", "devices": devices})

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

            safe_active_locks[device] = {"device": meta.get("device"), "client_id": clean_name, "timestamp": meta.get("timestamp")}

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
        web.post('/force_unlock', handle_force_unlock)
    ])
    print("Medusa lock-server auf http://127.0.0.1:8080 ...")
    web.run_app(app, host='127.0.0.1', port=8080)