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
    # Function for locking devices / dependencies
    pass

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
    print("Asynchronous queue-server auf http://127.0.0.1:8080 ...")
    web.run_app(app, host='127.0.0.1', port=8080)