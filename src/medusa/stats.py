from collections import Counter
from pathlib import Path

from .data import Data
from .errors import MedusaError


def print_stats(data: Data, selection: str) -> None:
    try:
        selections: set[str] = set(selection.split(","))
    except Exception:
        raise MedusaError("Failed to parse selection of stats")

    s_deps = False
    s_dynamic = False
    s_static = False
    s_stages = False
    s_suites = False
    s_tags = False
    s_totals = False

    for s in selections:
        match s:
            case "all":
                s_deps = True
                s_dynamic = True
                s_static = True
                s_stages = True
                s_suites = True
                s_tags = True
                s_totals = True
                break
            case "deps":
                s_deps = True
            case "dynamic":
                s_dynamic = True
            case "static":
                s_static = True
            case "stages":
                s_stages = True
            case "suites":
                s_suites = True
            case "tags":
                s_tags = True
            case "totals":
                s_totals = True
            case other:
                raise MedusaError(
                    f"Unknown value in selection of stats: '{other}'"
                )

    if s_totals:
        _print_totals(data)
    if s_stages:
        _print_stages(data)
    if s_tags:
        _print_tags(data)
    if s_suites:
        _print_suites(data)
    if s_deps:
        _print_deps(data)
    else:
        if s_dynamic:
            _print_dynamic(data)
        if s_static:
            _print_static(data)


def _print_suites(data: Data) -> None:
    _print_title("Suites")
    for stage in sorted(data.stages.values(), key=lambda s: s.name):
        print("Stage", stage.name)
        for suite in sorted(stage.suites, key=lambda s: s.full_name):
            # print(f"  {suite.source.absolute().relative_to(Path().absolute())}")
            # if suite.for_vars:
            #     for name, value in suite.for_vars.items():
            #         print(f"    {name}: {value}")
            path = str(suite.source.resolve().relative_to(Path().resolve()))

            if suite.for_vars:
                for_vars = ", ".join(
                    '{name}="{value}"'.format(
                        name=k, value=str(v).replace('"', r"\"")
                    )
                    for k, v in suite.for_vars.items()
                )
                print(f"  {path}: " + for_vars)
            else:
                print(f"  {path}")

        print()


def _print_static(data: Data) -> None:
    _print_title("Static deps")
    for name, count in sorted(
        data.deps_static_cnt.items(), key=lambda name_count: name_count[0]
    ):
        unit = "Suite" if count == 1 else "Suites"
        print(f"  {name}: {count} {unit}")
    print()


def _print_dynamic(data: Data) -> None:
    _print_title("Dynamic deps")
    for name, count in sorted(
        data.deps_dynamic_cnt.items(), key=lambda name_count: name_count[0]
    ):
        unit = "Suite" if count == 1 else "Suites"
        print(f"  {name}: {count} {unit}")
    print()


def _print_totals(data: Data) -> None:
    _print_title("Totals")
    print("Stages:", len(data.stages))
    print("Suites:", data.n_suites)
    print("Tests:", data.n_tests)
    print("Tags:", len(data.tags))
    print(
        "Deps total:",
        len(set(data.deps_static_cnt).union(data.deps_dynamic_cnt)),
    )
    print("  static:", len(data.deps_static_cnt))
    print("  dynamic:", len(data.deps_dynamic_cnt))
    print()


def _print_stages(data: Data) -> None:
    _print_title("Stages")
    for s in sorted(data.stages.values(), key=lambda stage: stage.name):
        s_unit = "Suite" if s.n_suites == 1 else "Suites"
        t_unit = "Test" if s.n_tests == 1 else "Tests"
        print(f"{s.name}: {s.n_suites} {s_unit}, {s.n_tests} {t_unit}")
    print()


def _print_deps(data: Data) -> None:
    _print_title("Deps")
    total: Counter[str] = Counter(data.deps_static_cnt)
    total.update(data.deps_dynamic_cnt)
    for name, count in sorted(
        total.items(), key=lambda name_count: name_count[0]
    ):
        unit = "Suite" if count == 1 else "Suites"
        static = data.deps_static_cnt[name]
        dynamic = data.deps_dynamic_cnt[name]
        print(f"{name}: {count} {unit} (static: {static}, dynamic: {dynamic})")
    print()


def _print_tags(data: Data) -> None:
    _print_title("Tags")
    for name, count in sorted(
        data.tags.items(), key=lambda name_count: name_count[0]
    ):
        unit = "Test" if count == 1 else "Tests"
        print(f"{name}: {count} {unit}")
    print()


def _print_title(title: str) -> None:
    total = 40
    title_len = len(title)
    fillers = total - title_len - 2

    before = fillers / 2
    after = fillers / 2 if title_len % 2 == 0 else fillers / 2 + 1

    print(int(before) * "=", title, int(after) * "=")
