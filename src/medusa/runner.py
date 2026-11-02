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

        self.active_sessions: dict[Suite, tuple[str, set[str]]] = {}
        self._state_lock = threading.Lock()
        self.server_url = server_url

    def try_lock(self, suite: "Suite") -> bool:
        # Ask server to lock & assign the suite's dependencies

        # Do not create another lock session for an already locked suite
        if suite in self.active_sessions:
            return False

        # Convert the suite's dependencies into the format expected by the server
        payload = {
            "deps_static": sorted(suite.deps_static),
            "deps_dynamic": {name: sorted(dyn.options)
                             for name, dyn in suite.deps_dynamic.items()}
        }

        # Send lock request to the server
        try:
            response = requests.post(
                f"{self.server_url}/lock",
                json=payload,
                timeout=5
            )
            print(f"[DEBUG] HTTP response received: {response.status_code}", flush=True)

        except requests.RequestException as exc:
            print(f"[SERVER-DEPMGR] Lock request failed: {exc}")
            return False

        # Server immediately rejects request if the required dependencies are unavailable
        if response.status_code == 409:
            return False

        # Handle unexpected HTTP responses
        if response.status_code != 200:
            print(f"[SERVER-DEPMGR] Lock request failed: {response.status_code} {response.text}")
            return False

        # Extract the session_id from servers lock confirmation
        # It is required to release the session if the response turns out to be invalid
        try:
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Lock confirmation must be a JSON object")

            session_id = data.get("session_id")

            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Missing or invalid session_id")

        except (ValueError, TypeError) as exc:
            print(f"[SERVER-DEPMGR] Invalid lock confirmation: {exc}")
            return False

        # Validate the servers lock confirmation before updation local state
        try:
            if data.get("status") != "locked":
                raise ValueError(f"Unexpected lock response: {data}")

            assigned_dynamic = data.get("deps_assigned")
            devices_raw = data.get("devices")

            if (not isinstance(assigned_dynamic, dict)
                or not isinstance(devices_raw, list)
                or any(not isinstance(device, str) for device in devices_raw)
            ):
                raise ValueError("Invalid lock confirmation fields")

            # Ensure that the server assigned every requested dynamic dependency
            if set(assigned_dynamic) != set(suite.deps_dynamic):
                raise ValueError("Dynamic dependency assignments do not match")

            # Ensure that each assigned device is a valid option for its dependency
            for name, device in assigned_dynamic.items():
                if(not isinstance(device, str) or device not in suite.deps_dynamic[name].options):
                    raise ValueError(f"Invalid assignment for dynamic dependency '{name}'")

            concrete_devices = set(devices_raw)
            expected_devices = set(suite.deps_static | set(assigned_dynamic.values()))

            # Ensure that the servers device list matches the actual assignments
            if concrete_devices != expected_devices:
                raise ValueError("Server's device list does not match its assignments")

            # Ensure that the server only assigned devices belonging to this stage
            if not concrete_devices.issubset(self.all):
                raise ValueError("Server assogmed a device outside this stage's dependencies")

        except (ValueError, TypeError) as exc:
            print(f"[SERVER-DEPMGR] Invalid lock confirmation: {exc}")

            # Release server-side session because its confirmation cannot be trusted
            try:
                free_response = requests.post(
                    f"{self.server_url}/free",
                    json={"session_id": session_id},
                    timeout=5,
                )

                if free_response.status_code not in (200, 404):
                    print(f"[SERVER-DEPMGR] Failed to release invalid session: {free_response.status_code} {free_response.text}")

            except requests.RequestException as free_exc:
                print(f"[SERVER-DEPMGR] Failed to release invalid session: {free_exc}")

            return False

        # Apply the servers dynamic assignments to the suite
        for name, device in assigned_dynamic.items():
            suite.deps_dynamic[name].value = device

        # Keep medusas local local bookkeeping in sync with the servers assignment
        self.available.difference_update(concrete_devices)
        self.in_use.update(concrete_devices)

        # Store session_id and assigned devices for later keepalive & release requests
        self.active_sessions[suite] = (session_id, concrete_devices)

        print(f"[SERVER-DEPMGR] Locked {sorted(concrete_devices)} for session '{session_id}'.")
        return True

    def free(self, suite: "Suite"):
        # Release the suites dependencies

        with self._state_lock:
            session_data = self.active_sessions.get(suite)

        if session_data is None:
            print(f"[SERVER-DEPMGR] No active sessions found for '{suite}'.")
            return

        session_id, devices = session_data

        payload = {"session_id": session_id}

        try:
            response = requests.post(f"{self.server_url}/free", json=payload, timeout=5)
        except requests.RequestException as exc:
            print(f"[SERVER-DEPMGR] Failed to release session: {exc}")
            return

        if response.status_code not in (200, 404):
            print(f"[SERVER-DEPMGR] Server rejected free request: {response.status_code} {response.text}")
            return

        # Server successfully released the session or it no longer exists on the server
        with self._state_lock:
            self.active_sessions.pop(suite, None)

        # Keep Medusa's local bookkeeping in sync
        self.available.update(devices)
        self.in_use.difference_update(devices)

        print(f"[SERVER-DEPMGR] Released {sorted(devices)} for session '{session_id}'.")

    def send_keepalive(self):
        # Send one keepalive-request to server

        with self._state_lock:
            sessions = [
                session_id
                for session_id, _ in self.active_sessions.values()
            ]

            for session_id in sessions:
                payload = {"session_id": session_id}

                try:
                    response = requests.post(f"{self.server_url}/alive", json=payload, timeout=5)
                except requests.RequestException as exc:
                    print(f"[SERVER-DEPMGR] Keepalive failed for session '{session_id}': {exc}")
                    continue

                if response.status_code != 200:
                    print(f"[SERVER-DEPMGR] Keepalive rejected for session '{session_id}': {response.status_code} {response.text}")
                    continue

                print(f"[SERVER-DEPMGR] Keepalive sent for session '{session_id}'.")


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
            if self.depmgr.active_sessions:
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
