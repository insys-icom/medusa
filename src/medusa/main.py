import logging
import sys
from pathlib import Path

from docopt import docopt  # type: ignore

from .constants import OUTPUTDIR, REPO_LINK, T_HARD, T_KILL
from .data import Data
from .errors import MedusaError
from .filters import Filters
from .robot import fetch_robot_data, merge_results
from .runner import Runner
from .settings import Settings
from .stats import print_stats
from .utils import LOGGER, Timeout
from .version import __version__
from .visual import write_visualization

HELP = f"""Medusa: Run Robot Framework suites with depdendency-aware parallelization

Usage:
  medusa run [-v] [-d OUTPUTDIR] [-f FILTER]... [-t TIMEOUT] [--] ROBOTARGS...
  medusa stats [-v] [-f FILTER]... [-s SELECTION] [--] ROBOTARGS...
  medusa help [filter|timeout]
  medusa version

Subcommands:
  help           Show extended help for the given topic.
  run            Run the given robot suite(s).
  stats          Display information about the given robot suite(s). Outputs
                 all information by default, see -s if you want less output.
  version        Show Medusa version.

Arguments:
  ROBOTARGS      Arguments for the robot command line tool. If you use robot
                 options, you need to write '--' before the robot options to
                 separate them from the medusa options.

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
            else:
                print(HELP)
        elif arguments["version"]:
            print(__version__)
        else:
            filters = Filters(arguments["--filter"])
            outputdir = Path(arguments["--outputdir"])
            robotargs = arguments["ROBOTARGS"]
            timeout = Timeout.from_argstr(arguments["--timeout"])
            settings = Settings(
                filters, log_level, outputdir, robotargs, timeout
            )

            if arguments["run"]:
                run(settings)
            elif arguments["stats"]:
                stats(settings, arguments.get("--select"))
    except MedusaError as e:
        LOGGER.error(str(e))
        exit(1)


def run(settings: Settings):
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


def stats(settings: Settings, selection: str):
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


def add_file_logger(settings: Settings):
    log_file = settings.outputdir / "medusa.log"

    # We use the same formatter as the StreamHandler
    assert len(LOGGER.handlers) > 0
    formatter = LOGGER.handlers[0].formatter

    # We write everything to file, log_level should only affect stdout output
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    LOGGER.addHandler(file_handler)


def format_path(path: Path) -> str:
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
