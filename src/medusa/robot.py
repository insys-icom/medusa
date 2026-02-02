import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from robot import running
from robot.api import SuiteVisitor
from robot.errors import Information  # type: ignore
from robot.rebot import rebot  # type: ignore
from robot.run import RobotFramework  # type: ignore

from .data import Data
from .robot_reader import RobotSuiteWalker
from .settings import Settings
from .suite import Suite
from .utils import LOGGER, Timer


def fetch_robot_data(settings: Settings) -> Data:
    t = Timer("processing suite data")
    t.timer_start()

    data = Data(settings.filters)
    errors: list[str] = list()
    suite_walker = RobotSuiteWalker(data, errors)
    rf = RobotFramework()

    try:
        opts_args = rf.parse_arguments(settings.robotargs)
        opts: dict[str, Any] = opts_args[0]
        args: list[str] = opts_args[1]
    except Information as e:
        # Catch `--help` and `--version` option output
        sys.exit(str(e))

    # Collects suite data
    opts.setdefault("listener", list())  # Don't overwrite user opts
    opts["listener"].append(suite_walker)

    # Deletes unnecessary empty suites and sets correct execution mode
    opts.setdefault("prerunmodifier", list())  # Don't overwrite user opts
    opts["prerunmodifier"].insert(0, SuitePrepModifier())
    opts["prerunmodifier"].insert(0, SuitePrepDeleter())

    # If we have more than one base suite, they get added to an automatically
    # named parent suite. Here we manually set the parent suite name.
    if len(args) > 1:
        opts["name"] = "Medusa"

    opts["log"] = None  # No log.html
    opts["report"] = None  # No report.html
    opts["output"] = None  # No output.xml
    opts["quiet"] = True  # Disable cli output
    opts["dryrun"] = True  # Don't actually run any keywords
    opts["runemptysuite"] = True  # Some suites may be empty if using -i or -I

    with StringIO() as stdout, StringIO() as stderr:
        opts["stdout"] = stdout
        opts["stderr"] = stderr

        rf.execute(*args, **opts)
        t.timer_end()

        if stderr.tell() > 0:
            print("Robot Framework Errors:")
            print(stderr.getvalue())
            sys.exit(1)

    if errors:
        print("Medusa Errors:")
        for error in errors:
            print(error)
        sys.exit(1)

    return data


def _get_pretty_metadata(suite: Suite) -> dict[str, str]:
    """Get a dictionary representation of suite metadata as it was resolved
    with variables all resolved and lists flattened.
    """
    metadata = {
        "medusa:stage": suite.stage,
        "medusa:deps": "    ".join(suite.deps),
    }

    if suite.for_vars:
        metadata["medusa:for"] = str(
            {k.strip("${}"): v for k, v in suite.for_vars.items()}
        ).strip("{}")

    return metadata


def run_suite(suite: Suite, settings: Settings):
    # Get independent process group, otherwise any interrupt that the parent
    # receives is also received by this process
    os.setsid()

    rf = RobotFramework()
    opts_args = rf.parse_arguments(settings.robotargs)
    opts: dict[str, Any] = opts_args[0]
    args: list[str] = opts_args[1]

    result_dir = settings.outputdir / suite.stage / suite.full_name
    result_dir.mkdir(parents=True, exist_ok=False)

    # Deletes unnecessary empty suites and sets correct execution mode. Also
    # writes suite metadata and appends suffix to suite name for `medusa:for`
    opts.setdefault("prerunmodifier", list())
    opts["prerunmodifier"].insert(0, SuitePrepModifier(suite))
    opts["prerunmodifier"].insert(0, SuitePrepDeleter())

    # If we have more than one base suite, they get added to an automatically
    # named parent suite. Here we manually set the parent suite name.
    if len(args) > 1:
        opts["name"] = "Medusa"

    opts["parseinclude"] = suite.source  # Only execute the current suite
    opts["runemptysuite"] = True  # Some suites are empty due to parseinclude
    opts["log"] = None  # No log.html
    opts["report"] = None  # No report.html
    opts["output"] = result_dir / "output.xml"

    # Make deps/stage/for available as variables and set `medusa:for` values
    opts.setdefault("variable", list())
    opts["variable"].append(f"MEDUSA_DEPS: list:{list(suite.deps)}")
    opts["variable"].append(f"MEDUSA_STAGE: str:{suite.stage}")
    if suite.for_vars:
        opts["variable"].append(f"MEDUSA_FOR: dict:{suite.for_vars}")
        for var, val in suite.for_vars.items():
            opts["variable"].append(f"{var.strip('${}')}: str:{val}")

    # Set dynamic dependency values as variables and add MEDUSA_DYNAMIC
    # variable that lists all the final chosen values for them
    if suite.deps_dynamic:
        dyndep_values = {
            name: dyn.value for name, dyn in suite.deps_dynamic.items()
        }
        opts["variable"].append(f"MEDUSA_DYNAMIC: dict:{dyndep_values}")
        for name, value in dyndep_values.items():
            opts["variable"].append(f"{name.strip('${}')}: str:{value}")

    # Finally we start the suite and capture stdout/stderr
    outfile = result_dir / "stdout.txt"
    errfile = result_dir / "stderr.txt"
    with open(outfile, "w") as stdout, open(errfile, "w") as stderr:
        # robot does not respect sys.stdout/stderr when it gets interrupted and
        # writes directly to sys.__stdout__/__stderr__ anyway. mypy does not
        # like reassigning them because they are final, but it works so we need
        # to do it anyway.
        sys.__stdout__ = stdout  # type: ignore
        sys.__stderr__ = stderr  # type: ignore
        opts["stdout"] = stdout
        opts["stderr"] = stderr
        rf.execute(*args, **opts)


class SuitePrepModifier(SuiteVisitor):
    def __init__(self, target_suite: Suite | None = None):
        super().__init__()
        self.suffix: str | None = None
        self.metadata: dict[str, str] | None = None
        self.source: Path | None = None

        if target_suite:
            self.suffix = target_suite.suffix
            self.metadata = _get_pretty_metadata(target_suite)
            self.source = target_suite.source.resolve()

    def start_suite(self, suite: running.TestSuite):
        # Edge case: If we execute a single medusa:for suite file, we end up
        # having multiple root suites with different names (because we change
        # the name). In this case, we need to artificially create a parent.
        # We can't just set the "parent" property, so we need to reconfigure
        # this suite, make it a child of itself and reset all other attributes.
        if not suite.parent and suite.tests and self.suffix:
            suite_dict = suite.to_dict()

            # Create an identical dict to suite_dict with all keys = None
            reset_dict = {
                k: None for k, _ in suite_dict.items() if k != "name"
            }

            # Reconfigure current suite, use reset_dict to overwrite everything
            # with None and create a new child suite from the suite_dict
            suite.config(
                name="Medusa",
                suites=[running.TestSuite.from_dict(suite_dict)],
                **reset_dict,
            )

        # Edge case: Under some circumstances, robot framework incorrectly
        # includes other suites AND their tests when using --parseinclude.
        # If a child suite has tests but the source is not the expected source,
        # we remove it here.
        if self.source and suite.suites:
            suite.suites = [
                s
                for s in suite.suites
                if not (s.tests and s.source.resolve() != self.source)
            ]

        # If we use medusa:for, we need to give the target suite a unique name
        if suite.tests and self.suffix:
            suite.name += self.suffix

        # Set the final metadata in the target suite after medusa processed it
        if suite.tests and self.metadata:
            for key, value in self.metadata.items():
                suite.metadata[key] = value

    def visit_test(self, _):
        pass

    def visit_keyword(self, _):
        pass


class SuitePrepDeleter(SuiteVisitor):
    """Deletes empty suites and sets the correct execution mode."""

    def start_suite(self, suite: running.TestSuite):
        # Robot adds all the other suites that were specified as command line
        # arguments, even though we use `parseinclude` to only run a single
        # suite. They are simply empty suites though (besides an edge case
        # that is taken care of by SuitePrepModifier) so we remove them except
        # for the immediate children because they impact result merging.
        suite.remove_empty_suites(preserve_direct_children=True)

        # Robot seems to get confused and believes that empty suites caused by
        # the `parseinclude` option actually contain RPA tasks. We explicitly
        # set rpa=False to prevent errors while merging the suite outputs.
        for s in suite.suites:
            if not s.has_tests:
                s.rpa = False

    def visit_test(self, _):
        pass

    def visit_keyword(self, _):
        pass


def merge_results(results_path: Path) -> None:
    """Merge each suite's output.xml into a single one at the root of the
    results path.

    If an invalid XML error is encountered during the merge, the merge is
    reattempted without the offending suite output.xml. This is repeated until
    the merge completes successfully. The report, log and output files get
    "-incomplete" appended to their names and a MISSING_RESULTS.txt is created
    containing all missing output.xml files.

    If any other error is encountered, the merge is aborted.
    """
    t = Timer("merging results")
    t.timer_start()

    os.sync()  # Force sync because results were written in other processes
    suite_outputs, error_occurred = _get_output_paths(results_path)

    output = "output.xml"
    report = "report.html"
    log = "log.html"

    abort = False
    removed_outputs: list[Path] = []

    while not abort:
        with StringIO() as stdout, StringIO() as stderr:
            rebot(
                *suite_outputs,
                merge=True,
                output=output,
                outputdir=results_path,
                log=False,
                report=False,
                stderr=stderr,
                stdout=stdout,
            )

            if stderr.tell() == 0:
                break  # No error, we are done
            else:
                error_occurred = True
                rebot_err = (
                    stderr.getvalue()
                    .removesuffix("Try --help for usage information.\n")
                    .strip()
                )

                for line in rebot_err.splitlines():
                    (results_path / output).unlink(missing_ok=True)
                    LOGGER.error("Rebot error: " + line)

                    if xml_err := re.search(
                        r"Reading XML source '(?P<path>.+output.xml)' failed:",
                        line,
                    ):
                        output = "output-incomplete.xml"
                        report = "report-incomplete.html"
                        log = "log-incomplete.html"

                        invalid_xml = Path(xml_err.group("path"))
                        suite_outputs.remove(invalid_xml)
                        removed_outputs.append(invalid_xml)

                        LOGGER.warning(
                            f"Invalid output '{invalid_xml}' removed from result"
                        )
                    else:
                        LOGGER.error("Unknown rebot error, aborting")
                        abort = True

    if removed_outputs:
        with open(results_path / "MISSING_RESULTS.txt", "w") as file:
            file.write("\n".join([str(p) for p in removed_outputs]) + "\n")

    if error_occurred:
        LOGGER.error(
            "Failed to locate or merge some results!"
            + " This can happen when a suite had to be forcefully terminated."
            + " Results may be incomplete or unavailable."
        )

    if (results_path / output).exists():
        # Create HTML log/report
        rebot(
            results_path / output,
            outputdir=results_path,
            log=log,
            report=report,
        )

    t.timer_end()


def _get_output_paths(path: Path) -> tuple[set[Path], bool]:
    """Recurses through the given path, finds all output.xml files and returns
    a tuple. The first element is a set of Paths to the output.xml files, the
    second one is a boolean that says whether an error occurred.
    """
    ret: set[Path] = set()
    failed = False

    for p in path.iterdir():
        if p.is_dir():
            paths, subdir_failed = _get_output_paths(p)
            ret.update(paths)
            if subdir_failed:
                failed = True
        elif p.name == "output.xml":
            ret.add(p)
            break  # Early stop, no need to seek more subdirs

    if not ret and not failed:
        failed = True
        LOGGER.error(
            f"Missing output.xml in '{path}', Robot Framework failed to write results!"
        )

    return (ret, failed)
