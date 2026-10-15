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
    pass

async def handle_lock(request):
    # Function for locking devices / dependencies
    pass

async def handle_unlock(request):
    # Function for unlocking currently locked devices / dependencies
    pass

async def handle_status(request):
    # Function for showing which devices are locked by which user & since when
    # Shows a CLI-Dashboard with locked devices & their (if existing) queues
    pass


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