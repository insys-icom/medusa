import logging
import os
import sys
from typing import TYPE_CHECKING

from docopt import docopt  # type: ignore

from .constants import DEFAULT_SERVER_URL, OUTPUTDIR, REPO_LINK, T_HARD, T_KILL
from .errors import MedusaError
from .server import normalize_server_url
from .utils import LOGGER

if TYPE_CHECKING:
    from pathlib import Path

    from .data import Data
    from .settings import Settings


HELP = f"""Medusa: Run Robot Framework suites with depdendency-aware parallelization

Usage:
  medusa run [-v] [-d OUTPUTDIR] [-f FILTER]... [-t TIMEOUT] [--server SERVER] [--] ROBOTARGS...
  medusa stats [-v] [-f FILTER]... [-s SELECTION] [--] ROBOTARGS...
  medusa server [-v] [--host HOST] [--port PORT]
  medusa lock [-v] DEVICES... [--server SERVER]
  medusa free [-v] UUID [--server SERVER]
  medusa status [-v] [--server SERVER]
  medusa help [filter|timeout|server]
  medusa version

Subcommands:
  help           Show extended help for the given topic.
  run            Run the given robot suite(s).
  stats          Display information about the given robot suite(s). Outputs
                 all information by default, see -s if you want less output.
  server         Start the Medusa lock server.
  lock           Lock dependencies using the lock server.
  free           Release a lock session.
  status         Show currently active lock sessions.
  version        Show Medusa version.

Arguments:
  ROBOTARGS      Arguments for the robot command line tool. If you use robot
                 options, you need to write '--' before the robot options to
                 separate them from the medusa options.
  DEVICES        Dependencies to lock. Supports static and dynamic
                 dependencies.

Options:
  -d --outputdir OUTPUTDIR  Store results in OUTPUTDIR, must not already exist.
                            [default: {OUTPUTDIR}]
  -f --filter FILTER        Only process suites that match the given FILTER.
                            Can be used multiple times. See `help filter`
                            for a detailed description.
  -h --help                 Display this message.
  -s --select SELECTION     Output the given comma-separated selection of
                            stats. Possible values are: all, deps, dynamic,
                            static, stages, suites, tags, totals
                            [default: all]
  -t --timeout TIMEOUT      Timeout for each suite. See `help timeout` for a
                            detailed description.
  -v --verbose              Enable debug logging.
  --host HOST               Address the Medusa lock server listens on.
                            [default: 127.0.0.1]
  --port PORT               Port the Medusa lock server listens on.
                            [default: 8081]
  --server SERVER           Address of the Medusa lock server (HOST[:PORT]).

Medusa is open source software licensed under the Apache License, Version 2.0.
Source code and license text can be viewed at:
{REPO_LINK}
"""

HELP_TIMEOUT = f"""Timeouts:
  Timeouts allow you to set a timeout for the robot process that runs a single
  test suite. The TIMEOUT argument has the following format:

    T_SOFT[,T_HARD[,T_KILL]]

  The three timeout arguments are in seconds. Only T_SOFT is mandatory. The
  values that are not specified use defaults: T_HARD={T_HARD}, T_KILL={T_KILL}

  Example values for TIMEOUT:
    360,30,5  (all three values are specified)
    360,30    (T_KILL is not specified and uses the default)
    360       (T_HARD and T_KILL are not specified and use the default)

  After T_SOFT seconds, an INT signal is sent to the robot process. Robot
  should skip to teardown and results should still be available.

  If the process is still running T_HARD seconds later, a second INT is sent
  and Robot should exit immediately. Results will be lost.

  If the process is still running T_KILL seconds later then medusa sends a KILL
  signal to forcefully stop the suite. Results will be lost.
"""

HELP_FILTER = """Filters:
  Filters allow you to restrict which suites are executed based on their stage
  and deps. You may have to surround the FILTERs in single quotes to prevent
  shell processing. The FILTER argument has the following format:

    <KEY><OPERATOR><VALUE>[,<VALUE>]...

  <KEY> can be either 'deps' or 'stage'. <OPERATOR> can  be '=' or '~'. The
  '~' operator is only for 'deps'. <VALUE> is the name of the stage/dep that
  should be included. If you prefix value with an '!', it will be excluded
  instead of included. Examples:

    stage=first,second  # Run only stages 'first' and 'second'.

    stage=!first        # Run all stages except 'first'.

    deps=one,two        # Run suites that ONLY contain the deps 'one' or 'two'.
                        # Suites may not contain any other deps.

    deps~one,two        # Run all suites that contain the deps 'one' or 'two'.
                        # Suites may additionally contain other deps.

    deps=!two,!three    # Run all suites that don't contain 'two' and 'three'.

    deps~one,!two       # Run all suites that contain the dep 'one', except
                        # for those that also contain 'two'.
"""

HELP_SERVER = """Server:
  Manage dependencies across multiple Medusa instances using the lock server.

  Commands:
    medusa server [-v] [--host HOST] [--port PORT]
        Start the Medusa lock server.

    medusa run [--server SERVER] [--] ROBOTARGS...
        Run the given Robot Framework suites.
        If a server is configured, dependencies are managed through the lock server.
        Otherwise, the local dependency manager is used.

    medusa lock DEVICES... [--server SERVER]
        Lock one or more dependencies. Supports static and dynamic dependencies.
        The command stays active and sends periodic keepalive requests.
        Press Ctrl+C to release the session.

    medusa free UUID [--server SERVER]
        Release all dependencies belonging to the specified session.
        UUID is the session ID shown by 'medusa status'.

    medusa status [--server SERVER]
        Show active lock sessions, their locked dependencies and keepalive times.

  Dynamic dependencies use the format:
    dynamic:NAME=DEVICE1,DEVICE2

  Server configuration:
    Server startup:
        --host HOST     Address the server listens on. [default: 127.0.0.1]
        --port PORT     Port the server listens on. [default: 8081]

  Client connection:
    --server SERVER     Specify the server address (HOST[:PORT]).
    MEDUSA_SERVER       Environment variable for the server address.

  --server takes precedence over MEDUSA_SERVER.
  If neither is specified, lock, free and status use 127.0.0.1:8081.
  For run, a server is only used if --server or MEDUSA_SERVER is set.
  Otherwise, the local dependency manager is used.

  Examples:
    # Start the lock server
    medusa server --host 127.0.0.1 --port 8081

    # Connect clients to a specific server
    medusa run tests/robot/test.robot --server 127.0.0.1:8081
    medusa lock device1 device2 --server 127.0.0.1:8081
    medusa lock dynamic:deviceGroup=device1,device2 --server 127.0.0.1:8081
    medusa free 123e4567-e89b-12d3-a456-426614174000 --server 127.0.0.1:8081
    medusa status --server 127.0.0.1:8081

    # Alternatively, configure the server using an environment variable
    export MEDUSA_SERVER=127.0.0.1:8081
    medusa run tests/robot/test.robot
    medusa lock device1 device2
    medusa free UUID
    medusa status
"""


def main() -> None:
    try:
        arguments = docopt(HELP)

        if arguments["--verbose"]:
            log_level = logging.DEBUG
        else:
            log_level = logging.WARNING

        configure_logging(log_level)

        if arguments["help"]:
            if arguments["timeout"]:
                print(HELP_TIMEOUT)
            elif arguments["filter"]:
                print(HELP_FILTER)
            elif arguments["server"]:
                print(HELP_SERVER)
            else:
                print(HELP)
        elif arguments["version"]:
            from .version import __version__

            print(__version__)
        elif arguments["lock"]:
            from .cli import lock_dependencies

            server_url = normalize_server_url(
                arguments["--server"]
                or os.environ.get("MEDUSA_SERVER")
                or DEFAULT_SERVER_URL
            )
            lock_dependencies(arguments["DEVICES"], server_url)
        elif arguments["free"]:
            from .cli import free

            server_url = normalize_server_url(
                arguments["--server"]
                or os.environ.get("MEDUSA_SERVER")
                or DEFAULT_SERVER_URL
            )
            free(arguments["UUID"], server_url)
        elif arguments["status"]:
            from .cli import show_status

            server_url = normalize_server_url(
                arguments["--server"]
                or os.environ.get("MEDUSA_SERVER")
                or DEFAULT_SERVER_URL
            )
            show_status(server_url)
        elif arguments["server"]:
            from .server import start_server

            start_server(arguments["--host"], arguments["--port"])
        else:
            from pathlib import Path

            from .filters import Filters
            from .settings import Settings
            from .utils import Timeout

            filters = Filters(arguments["--filter"])
            outputdir = Path(arguments["--outputdir"])
            robotargs = arguments["ROBOTARGS"]
            timeout = Timeout.from_argstr(arguments["--timeout"])
            server = arguments["--server"] or os.environ.get("MEDUSA_SERVER")
            settings = Settings(
                filters,
                log_level,
                outputdir,
                robotargs,
                timeout,
                server_url=(normalize_server_url(server) if server else None),
            )

            if arguments["run"]:
                run(settings)
            elif arguments["stats"]:
                stats(settings, arguments.get("--select"))
    except MedusaError as e:
        LOGGER.error(str(e))
        sys.exit(1)


def run(settings: "Settings"):
    from .robot import fetch_robot_data, merge_results
    from .runner import Runner
    from .visual import write_visualization

    data: Data = fetch_robot_data(settings)

    if data.n_tests <= 0:
        raise MedusaError("No tests found, nothing to run!")

    try:
        settings.outputdir.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        raise MedusaError("Failed to create output directory", str(e))

    add_file_logger(settings)

    Runner.run(settings, data)

    write_visualization(settings, data)
    merge_results(settings.outputdir)

    print(f"Results: {format_path(settings.outputdir)}")


def stats(settings: "Settings", selection: str):
    from .robot import fetch_robot_data
    from .stats import print_stats

    data: Data = fetch_robot_data(settings)
    print_stats(data, selection)


def configure_logging(log_level: int):
    # Root logger should get all messages, user level is set for handler
    LOGGER.setLevel(logging.DEBUG)

    # 2025-10-28 09:46:31: <message>
    formatter = logging.Formatter(
        fmt="[{levelname}] {asctime}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{",
    )

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(log_level)

    LOGGER.addHandler(stream_handler)


def add_file_logger(settings: "Settings"):
    log_file = settings.outputdir / "medusa.log"

    # We use the same formatter as the StreamHandler
    assert len(LOGGER.handlers) > 0
    formatter = LOGGER.handlers[0].formatter

    # We write everything to file, log_level should only affect stdout output
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    LOGGER.addHandler(file_handler)


def format_path(path: "Path") -> str:
    """If stdout is a tty, the path is wrapped as a OSC 8 link to make
    terminals recognise it.
    """
    path = path.resolve()

    if sys.stdout.isatty():
        ESC = "\x1b"
        SEP = f"{ESC}\\"
        OSC = f"{ESC}]"
        OSC8 = f"{OSC}8;;"

        # The spec says to prepend the hostname but terminals don't handle this
        # hostname = socket.gethostname()
        # return f"{OSC8}file://{hostname}{path}{SEP}{path}{OSC8}{SEP}"

        return f"{OSC8}file://{path}{SEP}{path}{OSC8}{SEP}"
    else:
        return str(path)
