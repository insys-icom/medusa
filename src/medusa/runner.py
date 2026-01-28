import datetime
import logging
import multiprocessing
import multiprocessing.connection
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from .data import Data, Stage, Status
from .robot import run_suite
from .settings import Settings
from .suite import Suite
from .utils import LOGGER, Timer


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

    def __enter__(self):
        signal.signal(signal.Signals.SIGINT, self)
        signal.signal(signal.Signals.SIGTERM, self)

    def __exit__(self, *exc_info):
        signal.signal(signal.Signals.SIGINT, signal.SIG_DFL)
        signal.signal(signal.Signals.SIGTERM, signal.SIG_DFL)


SIGNAL_MONITOR = _SignalMonitor()


class DepManager:
    def __init__(self, stage: Stage):
        all_deps: set[str] = set(stage.deps_static_cnt).union(
            stage.deps_dynamic_cnt
        )
        self.all = frozenset(all_deps)
        self.available = set(all_deps)
        self.in_use: set[str] = set()

    def try_lock(self, suite: Suite) -> bool:
        """Attempt to lock dependencies for the given suite. Returns True on
        success or False if not all necessary dependencies were available."""
        deps_assigned = suite.try_assign_deps(self.available)
        if deps_assigned is None:
            return False

        assert deps_assigned.issubset(self.all)
        assert deps_assigned.issubset(self.available), (
            f"assigned={deps_assigned}, available={self.available}, all={self.all}"
        )
        self.available.difference_update(deps_assigned)
        self.in_use.update(deps_assigned)
        return True

    def free(self, suite: Suite):
        deps_assigned = suite.deps
        assert deps_assigned.issubset(self.all)
        assert deps_assigned.issubset(self.in_use)

        self.available.update(deps_assigned)
        self.in_use.difference_update(deps_assigned)


@dataclass
class ProcessInfo:
    process: multiprocessing.Process
    interrupt_count: int = 0
    start_time: datetime.datetime = field(
        default_factory=datetime.datetime.now, init=False
    )


@dataclass
class ProcessManager:
    settings: Settings

    # map of sentinel: ProcessInfo
    processes: dict[Any, ProcessInfo] = field(default_factory=dict)
    suites: dict[Any, Suite] = field(default_factory=dict)
    running: bool = field(default=False)

    def start(self, suite: Suite):
        p = multiprocessing.Process(
            target=run_suite, args=(suite, self.settings)
        )
        LOGGER.info(f"Starting '{suite.full_name}'")
        p.start()
        suite.status = Status.STARTED
        suite.timer_start()
        self.processes[p.sentinel] = ProcessInfo(p)
        self.suites[p.sentinel] = suite
        self.running = True

    def get_finished_suites(self) -> list[Suite]:
        ret = list()
        for sentinel in multiprocessing.connection.wait(
            self.processes.keys(), timeout=1.0
        ):
            pinfo = self.processes[sentinel]
            pinfo.process.join()
            del self.processes[sentinel]

            suite = self.suites[sentinel]
            suite.status = Status.FINISHED
            suite.timer_end()
            LOGGER.info(f"Finished '{suite.full_name}' ({suite.t_duration})")

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
    def __init__(self, settings: Settings, stage: Stage):
        self.stage = stage

        self.depmgr = DepManager(stage)
        self.procmgr = ProcessManager(settings)

        if sys.stdout.isatty() and settings.log_level == logging.WARN:
            self.interactive = True
        else:
            self.interactive = False

    @classmethod
    def run(cls, settings: Settings, data: Data):
        t = Timer("execution")
        t.timer_start()

        for name, stage in sorted(data.stages.items(), key=lambda tup: tup[0]):
            runner = cls(settings, stage)

            with SIGNAL_MONITOR:
                runner.run_stage()

            if SIGNAL_MONITOR.signal_count > 0:
                break  # We got a SIGINT or SIGTERM, don't start more stages

        t.timer_end()

    def run_stage(self):
        self.stage.timer_start()

        interrupted = False
        self.print_status()

        while (self.stage.pending and not interrupted) or self.procmgr.running:
            change_happened = False

            self.procmgr.handle_signals()
            self.procmgr.handle_timeouts()

            if SIGNAL_MONITOR.signal_count > 0:
                interrupted = True

            # Process finished suites
            for suite in self.procmgr.get_finished_suites():
                self.depmgr.free(suite)
                change_happened = True

            # Start pending suites
            if self.stage.pending and not interrupted:
                pending = (
                    s for s in self.stage.suites if s.status == Status.PENDING
                )

                for suite in pending:
                    if self.depmgr.try_lock(suite):
                        self.procmgr.start(suite)
                        change_happened = True

            if change_happened:
                self.print_status()

        self.stage.timer_end()

    def print_status(self):
        pending = self.stage.pending
        started = self.stage.started
        finished = self.stage.finished

        total = pending + started + finished
        percent = int((finished / total) * 100)
        contents = (
            f"({percent:>3}%)"
            + f" Suites pending: {pending:<4}"
            + f" running: {started:<4}"
            + f" finished: {finished:<4}"
        )

        # In interactive mode, we just keep overwriting the current status by
        # using \r and omitting \n. In the last status message we do not omit
        # the \n in order to have a new line at the end of stage execution.
        if self.interactive:
            end = "" if finished != total else "\n"
            print(f"\r{contents}", end=end)
        else:
            print(f"{contents}")
