import datetime
import json
import logging
import multiprocessing
import multiprocessing.connection
import os
import requests
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .robot import run_suite
from .suite import Result, Status
from .utils import LOGGER, Timer

if TYPE_CHECKING:
    from typing import Any, Self

    from .data import Data, Stage
    from .settings import Settings
    from .suite import Suite


class _SignalMonitor:
    """Signal handler for SIGINT and SIGTERM, use as a context manager.
    When entering the context, installs itself as handler and when exiting
    it reverts the signal handlers to default. Stores the number of received
    signals.
    """

    def __init__(self):
        self.signal_count = 0

    def __call__(self, signum, frame):
        self.signal_count += 1
        signame = signal.Signals(signum).name
        LOGGER.warning(f"Received {signame}")

        if self.signal_count == 1:
            LOGGER.warning(
                "Stopping execution after running suites are finished."
                + " Further signals will be sent directly to running robot processes."
            )

    def __enter__(self) -> "Self":
        signal.signal(signal.Signals.SIGINT, self)
        signal.signal(signal.Signals.SIGTERM, self)
        return self

    def __exit__(self, *exc_info):
        signal.signal(signal.Signals.SIGINT, signal.SIG_DFL)
        signal.signal(signal.Signals.SIGTERM, signal.SIG_DFL)


SIGNAL_MONITOR = _SignalMonitor()


class ServerDepManager:
    def __init__(self, stage:"Stage", server_url="http://127.0.0.1:8080"):
        all_deps: set[str] = set(stage.deps_static_cnt).union(stage.deps_dynamic_cnt)
        self.all = frozenset(all_deps)
        self.available = set(all_deps)
        self.in_use: set[str] = set()

        self.pending_locks = {}
        self._state_lock = threading.Lock()

        self.server_url = server_url
        self.client_id = str(uuid.uuid4())

    def try_lock(self, suite: "Suite") -> bool:
        # Ask server to lock & assign the suite's dependencies

        # Do not create another lock session for an already locked suite
        if suite in self.pending_locks:
            return False

        payload = {
            "client_id": self.client_id,
            "deps_static": sorted(suite.deps_static),
            "deps_dynamic": {name: sorted(dyn.options)
                             for name, dyn in suite.deps_dynamic.items()}
        }

        try:
            response = requests.post(
                f"{self.server_url}/lock",
                json=payload,
                timeout=5)
            print(f"[DEBUG] HTTP response received: {response.status_code}", flush=True)

        except requests.RequestException as exc:
            print(f"[SERVER-DEPMGR] Lock request failed: {exc}")
            return False

        # Server immediately rejects unavailable dependencies
        if response.status_code == 409:
            return False

        if response.status_code != 200:
            print(f"[SERVER-DEPMGR] Lock request failed: {response.status_code} {response.text}")
            return False

        try:
            data = response.json()

            if data.get("status") != "locked":
                raise ValueError(f"Unexpected lock response: {data}")

            assigned_dynamic = data.get("deps_assigned")
            devices_raw = data.get("devices")

            if (not isinstance(assigned_dynamic, dict)
                or not isinstance(devices_raw, list)
                or any(not isinstance(device, str) for device in devices_raw)
            ):
                raise ValueError("Invalid lock confirmation fields")

            # Verify that server has assigned every dynamic dependency
            if set(assigned_dynamic) != set(suite.deps_dynamic):
                raise ValueError("Dynamic dependency assignments do not match")

            # Verfiy every dynamic assignment
            for name, device in assigned_dynamic.items():
                if(not isinstance(device, str) or device not in suite.deps_dynamic[name].options):
                    raise ValueError(f"Invalid assignment for dynamic dependency '{name}'")

            concrete_devices = set(devices_raw)
            expected_devices = set(suite.deps_static | set(assigned_dynamic.values()))

            if concrete_devices != expected_devices:
                raise ValueError("Server's device list does not match its assignments")

            if not concrete_devices.issubset(self.all):
                raise ValueError("Server assogmed a device outside this stage's dependencies")

        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[SERVER-DEPMGR] Invalid lock confirmation: {exc}")
            return False

        # Apply servers dynamic assignements to the suite
        for name, device in assigned_dynamic.items():
            suite.deps_dynamic[name].value = device

        # Keep medusas local local bookkeeping in sync
        self.available.difference_update(concrete_devices)
        self.in_use.update(concrete_devices)

        # Remember which devices belong to this suite
        self.pending_locks[suite] = concrete_devices

        print(f"[SERVER-DEPMGR] Locked {sorted(concrete_devices)} for client '{self.client_id}'.")
        return True

    def free(self, suite: "Suite"):
        # Release the suites dependencies

        devices = self.pending_locks.get(suite, None)

        if devices is None:
            print(f"[SERVER-DEPMGR] No active lock found for '{suite}'.")
            return

        payload = {"client_id": self.client_id,
                   "devices": sorted(devices)}

        try:
            response = requests.post(f"{self.server_url}/free",
                                        json=payload,
                                        timeout=5)
        except requests.RequestException as exc:
            print(f"[SERVER-DEPMGR] Failed to release locks: {exc}")
            return

        if response.status_code != 200:
            print(f"[SERVER-DEPMGR] Server rejected unlock request: {response.status_code} {response.text}")
            return

        # Server successfully released the locks
        self.pending_locks.pop(suite)

        # Keep Medusa's local bookkeeping in sync
        self.available.update(devices)
        self.in_use.difference_update(devices)

        print(f"[SERVER-DEPMGR] Released {sorted(devices)} for client '{self.client_id}'.")

    def send_keepalive(self):
        # Sends one keepalive-request

        try:
            response = requests.post(f"{self.server_url}/alive",
                                     json={"client_id": self.client_id},
                                     timeout=5)
        except requests.RequestException as exc:
            print(f"[SERVER-DEPMGR] Keepalive failed: {exc}")
            return False

        if response.status_code != 200:
            print(f"[SERVER-DEPMGR] Keepalive rejected: {response.status_code} {response.text}")
            return False

        print(f"[SERVER-DEPMGR] Keepalive sent for '{self.client_id}'")
        return True


@dataclass
class ProcessInfo:
    process: "multiprocessing.Process"
    interrupt_count: int = 0
    start_time: "datetime.datetime" = field(
        default_factory=datetime.datetime.now, init=False
    )


@dataclass
class ProcessManager:
    settings: "Settings"

    # map of sentinel: ProcessInfo
    processes: "dict[Any, ProcessInfo]" = field(default_factory=dict)
    suites: "dict[Any, Suite]" = field(default_factory=dict)
    running: bool = field(default=False)

    def start(self, suite: "Suite", *, retry: bool = False):
        p = multiprocessing.Process(
            target=run_suite, args=(suite, self.settings, retry)
        )
        LOGGER.info(f"Starting '{suite.full_name}'")
        p.start()

        if retry:
            suite.retry -= 1

        suite.status = Status.STARTED
        suite.timer_start()
        self.processes[p.sentinel] = ProcessInfo(p)
        self.suites[p.sentinel] = suite
        self.running = True

    def get_finished_suites(self, *, retry: bool = False) -> "list[Suite]":
        ret = []
        for sentinel in multiprocessing.connection.wait(
            self.processes.keys(), timeout=1.0
        ):
            pinfo = self.processes[sentinel]
            pinfo.process.join()
            del self.processes[sentinel]

            suite = self.suites[sentinel]
            suite.result = Result.from_exitcode(pinfo.process.exitcode)

            if retry and suite.result == Result.FAIL and suite.retry > 0:
                # Flaky suite failed and has remaining retries so we leave it
                # pending to be retried again.
                suite.status = Status.PENDING
            else:
                suite.status = Status.FINISHED

            suite.timer_end()
            LOGGER.info(
                f"Finished '{suite.full_name}': {suite.result} ({suite.t_duration})"
            )

            ret.append(suite)
            del self.suites[sentinel]

        if len(self.processes) == 0:
            self.running = False

        return ret

    def handle_signals(self) -> None:
        for sentinel, pinfo in self.processes.items():
            while pinfo.interrupt_count < (SIGNAL_MONITOR.signal_count - 1):
                self._send_signal(sentinel)

    def handle_timeouts(self) -> None:
        """Checks whether any suites have exceeded a timeout and sends them the
        appropriate signal. Suite timeout is considered first, then the global
        timeout.
        """
        for sentinel, pinfo in self.processes.items():
            suite = self.suites[sentinel]

            if suite.timeout:
                timeout = suite.timeout
            elif self.settings.timeout:
                timeout = self.settings.timeout
            else:
                continue  # This suite has no timeout

            duration_delta = datetime.datetime.now() - suite.t_start
            duration = duration_delta.total_seconds()

            if pinfo.interrupt_count == 0:
                if duration > timeout.soft:
                    LOGGER.warning(
                        f"Suite '{suite.full_name}' exceeded soft timeout"
                    )
                    self._send_signal(sentinel)

            elif pinfo.interrupt_count == 1:
                if duration > timeout.hard_total:
                    LOGGER.warning(
                        f"Suite '{suite.full_name}' exceeded hard timeout"
                    )
                    self._send_signal(sentinel)

            elif duration > timeout.kill_total:
                LOGGER.warning(
                    f"Suite '{suite.full_name}' exceeded kill timeout"
                )
                self._send_signal(sentinel)

    def _send_signal(self, sentinel):
        """Sends the appropriate signal to stop a given process based on how
        many times it has already been interrupted.
        """
        pinfo = self.processes[sentinel]
        suite = self.suites[sentinel]

        if pinfo.interrupt_count < 2:
            # Added in python 3.14, can't use yet:
            # pinfo.process.interrupt()
            LOGGER.warning(
                f"Sending INT signal {pinfo.interrupt_count + 1} to '{suite.full_name}'"
            )
            assert pinfo.process.pid is not None
            os.kill(pinfo.process.pid, signal.SIGINT)
        else:
            LOGGER.warning(f"Sending KILL signal to '{suite.full_name}'")
            pinfo.process.kill()

        pinfo.interrupt_count += 1


class Runner:
    def __init__(self, settings: "Settings", stage: "Stage"):
        self.stage = stage

        self.depmgr = ServerDepManager(stage)
        self.procmgr = ProcessManager(settings)

        if sys.stdout.isatty() and settings.log_level == logging.WARNING:
            self.interactive = True
        else:
            self.interactive = False

    @classmethod
    def run(cls, settings: "Settings", data: "Data"):
        t = Timer("execution")
        t.timer_start()

        for name, stage in sorted(data.stages.items(), key=lambda tup: tup[0]):
            runner = cls(settings, stage)

            with SIGNAL_MONITOR:
                stage.timer_start()
                runner.run_stage()
                runner.retry_failed()
                stage.timer_end()

            if SIGNAL_MONITOR.signal_count > 0:
                break  # We got a SIGINT or SIGTERM, don't start more stages

        t.timer_end()

    def retry_failed(self):
        retry_suites = [
            s
            for s in self.stage.suites
            if s.result == Result.FAIL and s.retry > 0
        ]

        if not retry_suites:
            return  # Nothing to retry

        # Reset status to pending to re-run them with run_stage
        for s in retry_suites:
            s.status = Status.PENDING

        print("Re-running failed suites with medusa:retry metadata...")
        self.run_stage(retry=True)

    def run_stage(self, *, retry: bool = False):
        interrupted = False
        self.print_status()

        while (self.stage.pending and not interrupted) or self.procmgr.running:
            change_happened = False

            self.procmgr.handle_signals()
            self.procmgr.handle_timeouts()

            if SIGNAL_MONITOR.signal_count > 0:
                interrupted = True

            # Process finished suites
            for suite in self.procmgr.get_finished_suites(retry=retry):
                self.depmgr.free(suite)
                change_happened = True

            # Start pending suites
            if self.stage.pending and not interrupted:
                pending = (
                    s for s in self.stage.suites if s.status == Status.PENDING
                )

                for suite in pending:
                    if self.depmgr.try_lock(suite):
                        self.procmgr.start(suite, retry=retry)
                        change_happened = True

            # Send keepalive while locks are active
            if self.depmgr.pending_locks:
                self.depmgr.send_keepalive()

            if change_happened:
                self.print_status()

    def print_status(self):
        pending = self.stage.pending
        started = self.stage.started
        finished = self.stage.finished
        passed = self.stage.passed
        failed = self.stage.failed
        errored = self.stage.errored

        total = pending + started + finished
        percent = int((finished / total) * 100)
        contents = (
            f"({percent:>3}%)"
            + f" Suites pending: {pending:<4}"
            + f" running: {started:<4}"
            + f" finished: {finished:<4}"
            + f" (PASS: {passed:<4} FAIL: {failed:<4} ERR: {errored:<4})"
        )

        # In interactive mode, we just keep overwriting the current status by
        # using \r and omitting \n. In the last status message we do not omit
        # the \n in order to have a new line at the end of stage execution.
        if self.interactive:
            end = "" if finished != total else "\n"
            print(f"\r{contents}", end=end)
        else:
            print(f"{contents}")
