# Temporary test file (do not safe in git!!!)
import requests
import threading
import time
import uuid
from medusa.runner import ServerDepManager


SERVER_URL = "http://127.0.0.1:8080"
CLIENT_ID = "medusa_test_dummy"
DEVICES = {"testm1", "testm4", "testm4_2","m4_mrx2"}
SESSION_UUID = f"{CLIENT_ID}_{uuid.uuid4().hex[:8]}"


class FakeDynDep:
    def __init__(self, options):
        self.options = set(options)
        self.value = None

class FakeStage:
    def __init__(self, deps):
        self.deps_static_cnt = {dep: 1 for dep in deps}
        self.deps_dynamic_cnt = {}

class FakeSuite:
    def __init__(self, deps_static=None, deps_dynamic=None):
        self.deps_static = frozenset(deps_static or [])
        self.deps_dynamic = {name: FakeDynDep(options)
                             for name, options in (deps_dynamic or {}).items()}

    @property
    def deps(self):
        return set(self.deps_static) | {dyn.value for dyn in self.deps_dynamic.values()}

stage = FakeStage(DEVICES)
suite = FakeSuite(DEVICES)
depmgr = ServerDepManager(stage, SERVER_URL)

def lock():
    # global session_uuid
    # print(f"Locking: {DEVICES}")
    # response = requests.post(f"{SERVER_URL}/lock",
    #                          json={"devices": DEVICES, "client_id": CLIENT_ID, "session_uuid":SESSION_UUID},
    #                          timeout=None)
    # print("Lock response:")
    # print(response.status_code)
    # print(response.json())
    # data = response.json()
    # session_uuid = data["session_uuid"]
    print("Calling ServerDepManager.try_lock()...")
    result = depmgr.try_lock(suite)
    print(f"try_lock() returned: {result}")
    if result:
        print("Lock acquired. Simulation client crash ...")
        time.sleep(3)
        print("Exiting without calling free()!")
        return

def unlock():
    # global session_uuid
    time.sleep(2)       # give lock request enough time to acquire lock
    # print(f"Unlocking: {DEVICES}")
    # if session_uuid is None:
    #     print("No session_uuid available. Lock probably failed.")
    #     return
    # response = requests.post(f"{SERVER_URL}/unlock",
    #                          json={"devices": DEVICES, "client_id": SESSION_UUID},
    #                          timeout=5)
    # print("Unlock response:")
    # print(response.status_code)
    # print(response.json())
    print("Calling ServerDepManager.free() ...")
    depmgr.free(suite)
    print("free() finished")

# def client_a(depmgr, suite):
#     print("[A] Calling try_lock()...")
#     result = depmgr.try_lock(suite)
#     print(f"[A] try_lock() returned: {result}")

#     if result:
#         print("[A] Holding testm1 for 5 seconds...")
#         time.sleep(5)

#         print("[A] Calling free()...")
#         depmgr.free(suite)
#         print("[A] free() finished")


# def client_b(depmgr, suite):
#     print("[B] Calling try_lock()...")
#     result = depmgr.try_lock(suite)
#     print(f"[B] try_lock() returned: {result}")

#     if result:
#         print("[B] Got testm1!")

#         print("[B] Calling free()...")
#         depmgr.free(suite)
#         print("[B] free() finished")


# if __name__ == "__main__":
#     stage_a = FakeStage({"testm1"})
#     stage_b = FakeStage({"testm1"})
#     depmgr_a = ServerDepManager(stage_a)
#     depmgr_b = ServerDepManager(stage_b)
#     suite_a = FakeSuite({"testm1"})
#     suite_b = FakeSuite({"testm1"})
#     thread_a = threading.Thread(target=client_a, args=(depmgr_a, suite_a))
#     thread_b = threading.Thread(target=client_b, args=(depmgr_b, suite_b))
#     print("=== Starting Client A ===")
#     thread_a.start()
#     time.sleep(1)
#     print("\n=== Starting Client B ===")
#     thread_b.start()
#     thread_a.join()
#     thread_b.join()

#     print("\n=== FINAL STATE ===")
#     print("Suite deps:", suite.deps)
#     print("Available A:", depmgr_a.available)
#     print("In use A:", depmgr_a.in_use)
#     print("Available B:", depmgr_b.available)
#     print("In use B:", depmgr_b.in_use)
#     # print("Active connections:", depmgr.active_connections)

def main():
    stage = FakeStage({"testm1", "testm4", "testm4_2", "m4_mrx2"})
    suite = FakeSuite({"testm1"})
    depmgr = ServerDepManager(stage, SERVER_URL)

    print("=== LOCK TEST ===")
    acquired = depmgr.try_lock(suite)
    print(f"try_lock() returned: {acquired}")
    # print(f"Suite deps: {suite.deps}")
    # print(f"Available: {depmgr.available}")
    # print(f"In use: {depmgr.in_use}")
    if not acquired:
        print("Lock failed.")
        return

    print(f"Active connections: {list(depmgr.active_connections)}")
    status_response = requests.get(f"{SERVER_URL}/status", timeout=5)
    print("Server status while locked:", status_response.json())

    print("\n=== FREE TEST ===")
    depmgr.free(suite)

    # Give server time to detect closed stream and clean up
    time.sleep(1.5)

    status_after_response = requests.get(f"{SERVER_URL}/status", timeout=5)
    print("Server status after free:", status_after_response.json())

    print("\n=== DYNAMIC LOCK TEST ===")
    dynamic_suite = FakeSuite(deps_dynamic={"DUT": {"testm4", "testm4_2", "m4_mrx2"}})
    print("Known stage devices:", sorted(depmgr.all))
    locked = depmgr.try_lock(dynamic_suite)
    print(f"try_lock() returned: {locked}")
    print("Assigned DUT:", dynamic_suite.deps_dynamic["DUT"].value)

    if locked:
        print("Server status while dynamically locked:", requests.get(f"{depmgr.server_url}/status").json())
        print("\n=== DYNAMIC FREE TEST ===")
        depmgr.free(dynamic_suite)
        print("Server status after dynamic free:", requests.get(f"{depmgr.server_url}/status").json())
        deadline = time.monotonic() + 5
        while True:
            status = requests.get(f"{depmgr.server_url}/status", timeout=2).json()
            if not status["active_locks"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Server has not released locks on time: {status}")
            time.sleep(0.1)
        print("Server status after dynamic free:", status)

    print("\n=== DYNAMIC UNAVAILABLE TEST ===")
    blocker_suite = FakeSuite(deps_static={"testm4", "testm4_2", "m4_mrx2"})
    blocker_locked = depmgr.try_lock(blocker_suite)
    print(f"Blocking lock returned: {blocker_locked}")
    if blocker_locked:
        unavailable_suite = FakeSuite(deps_dynamic={"DUT": {"testm4", "testm4_2", "m4_mrx2"}})
        unavailable_locked = depmgr.try_lock(unavailable_suite)
        print(f"Dynamic try_lock() returned: {unavailable_locked}")
        print("Assigned DUT:", unavailable_suite.deps_dynamic["DUT"].value)
        print("Server status while candidates are locked:", requests.get(f"{depmgr.server_url}/status").json())
        depmgr.free(blocker_suite)
        print("Server status after releasing candidates:", requests.get(f"{depmgr.server_url}/status").json())
        deadline = time.monotonic() + 5
        while True:
            status = requests.get(f"{depmgr.server_url}/status", timeout=2).json()
            if not status["active_locks"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Server has not released Locks in time: {status}")
            time.sleep(0.1)
        print("Server status after dynamic free:", status)

    print("\n=== MULTIPLE DYNAMIC LOCK TEST ===")
    multiple_suite = FakeSuite(deps_dynamic={
        "DUT_A": {"testm4", "testm4_2"},
        "DUT_B": {"testm4"}
    })
    multiple_locked = depmgr.try_lock(multiple_suite)
    assignments = {name: dep.value
                   for name, dep in multiple_suite.deps_dynamic.items()}
    print(f"try_lock() returned: {multiple_locked}")
    print(f"Assignments: {assignments}")
    if multiple_locked:
        assert assignments["DUT_A"] is not None
        assert assignments["DUT_B"] is not None
        assert assignments["DUT_A"] != assignments["DUT_B"]
        depmgr.free(multiple_suite)


if __name__ == "__main__":
    main()