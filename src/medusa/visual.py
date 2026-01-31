import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.dates import (
    MICROSECONDLY,
    AutoDateLocator,
    ConciseDateFormatter,
)

from .data import Data
from .settings import Settings
from .suite import Status, Suite
from .utils import Timer

CSS = """
svg {
    width: 100%;
    height: 100%;
}
.suite path {
    opacity: 0.5;
}
.suite:hover path {
    opacity: 1;
}
"""

HOVER_FMT = """Suite: {name}

Source: {source}
Stage: {stage}
Deps: {deps}
Tags: {tags}
Started: {start}
Finished: {end}
Duration: {duration}
"""


def write_visualization(settings: Settings, data: Data) -> None:
    t = Timer("writing visualization")
    t.timer_start()

    path_svg = settings.outputdir / "visual.svg"
    suites = [
        s
        for stage in data.stages.values()
        for s in stage.suites
        if s.status == Status.FINISHED
    ]

    # Only consider stages that contain finished suites
    stage_starts: list[tuple[datetime, str]] = sorted(
        [(s.t_start, s.name) for s in data.stages.values() if s.finished > 0]
    )

    _create_plot(path_svg, suites, stage_starts)
    _add_hover_effects(path_svg, suites)

    t.timer_end()


def _create_plot(
    path_svg: Path,
    suites: list[Suite],
    stage_starts: list[tuple[datetime, str]],
) -> None:
    deps = _get_sorted_deps(suites)

    bar_count = len(deps)
    pixels_per_bar = 15
    pixels_per_inch = plt.rcParams["figure.dpi"]
    width_default, height_default = plt.rcParams["figure.figsize"]

    height_from_scale = (150 + bar_count * pixels_per_bar) / pixels_per_inch
    width_from_scale = height_from_scale * 2

    width = max(width_from_scale, width_default)
    height = max(height_from_scale, height_default)

    font_size_label = height * 2
    font_size_title = font_size_label * 1.4

    # Create a figure containing a single Axes
    fig, ax = plt.subplots(figsize=(width, height))

    # tab10 consists of 10 relatively dark colours, good for white background
    cmap = plt.get_cmap("tab10", 10)

    for i, s in enumerate(suites):
        ypositions = [deps.index(d) for d in s.deps]
        ax.barh(
            ypositions,
            width=s.t_duration_accurate,  # type: ignore
            left=s.t_start,  # type: ignore
            gid=s.full_name,
            color=cmap(i % 10),
        )

    # Locator determines which ticks are shown
    locator = AutoDateLocator(minticks=3, maxticks=6)
    locator.intervald[MICROSECONDLY] = [1000**2]  # Only seconds or greater
    # Formatter determines how the tick values are formatted
    formatter = ConciseDateFormatter(
        locator, formats=["%Y", "%b", "%d", "%H:%M", "%H:%M", ":%S"]
    )
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)

    ax.set_yticks(range(len(deps)), labels=deps)
    ax.invert_yaxis()

    # Vertical red lines with label at bottom show where a new stage starts.
    for start_time, name in stage_starts:
        ax.axvline(
            start_time,  # type: ignore
            color="r",
            linestyle="dotted",
            linewidth=max(width / 10, 1.0),
        )
        # Use get_xaxis_transform so that 0 maps to bottom and 1 to top
        ax.text(
            start_time,  # type: ignore
            0,
            name,
            size=font_size_label,
            color="r",
            ha="right",
            va="top",
            rotation=30,
            transform=ax.get_xaxis_transform(),
        )

    ax.grid(True, linewidth=1)
    ax.set_title("Dependency usage", size=font_size_title, weight="bold")
    ax.yaxis.set_label_text("Dependency", size=font_size_label)
    ax.xaxis.set_label_text("Time", size=font_size_label)

    plt.tight_layout()
    plt.margins(x=0.01, y=0.01)
    plt.savefig(path_svg, bbox_inches="tight")


def _add_hover_effects(path_svg: Path, suites: list[Suite]) -> None:
    # Prevent ugly namespace names in XML output
    ns = {
        "": "http://www.w3.org/2000/svg",
        "cc": "http://creativecommons.org/ns#",
        "dc": "http://purl.org/dc/elements/1.1/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "xlink": "http://www.w3.org/1999/xlink",
    }

    for k, v in ns.items():
        ET.register_namespace(k, v)

    tree = ET.parse(path_svg)
    root = tree.getroot()
    axes = root.find(".//g[@id='axes_1']", ns)
    assert axes

    style = ET.SubElement(root, "style", attrib={"type": "text/css"})
    style.text = CSS

    suite_elements: list[tuple[Suite, list[ET.Element]]] = []

    for s in suites:
        suite_elements.append(
            (s, axes.findall(f"./g[@id='{s.full_name}']/path", ns))
        )

        for parent in axes.findall(f"./g[@id='{s.full_name}']", ns):
            axes.remove(parent)

    for s, e in suite_elements:
        new_group = ET.SubElement(axes, "g", attrib={"class": "suite"})
        new_group.extend(e)
        description = ET.SubElement(new_group, "title")
        description.text = HOVER_FMT.format(
            name=s.full_name,
            source=s.source,
            stage=s.stage,
            deps=sorted(s.deps),
            tags=dict(s.tags),
            start=s.t_start,
            end=s.t_end,
            duration=s.t_duration,
        )

    tree.write(path_svg, encoding="utf-8")


def _get_sorted_deps(suites: list[Suite]) -> list[str]:
    """Returns list of dependencies sorted descending by time in use."""
    durations: dict[str, timedelta] = {}

    for s in suites:
        for d in s.deps:
            durations.setdefault(d, timedelta())
            durations[d] += s.t_duration

    return sorted(durations, key=lambda k: durations[k], reverse=True)
