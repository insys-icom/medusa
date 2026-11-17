# TODO(SZI): Dafür würde ich gerne einen unit test haben, da du die funktion
# schon sehr schön rausgetrennt hast! Nimm dir den test_filter_dynamic in
# tests/filters_test.py als Beispiel und besprich es kurz mit mir nachdem du es
# dir angeschaut hast :)
# Mir fehlt hier aber noch mehr Doku über die Ein- und Rückgabewerte, wie bei
# der originalen try_assign_deps funktion.
# Da die Funktion von der Logik her relativ komplex ist, hat sie auf jeden Fall
# mehr Erklärung nötig. Auch die ganzen Kommentare übner die Logik würde ich
# wieder einfügen, damit der algorithmus hier auch in menschensprache
# nachvollziehbar ist
def resolve_dynamic_dependencies(
    # TODO(SZI): Ich würde das nicht available options nennen, es sind ja nicht
    # nur die Optionen sondern ein dict aus zielvariable und optionen, also die
    # ganzen dyndeps
    available_options: dict[str, set[str]],
) -> dict[str, str] | None:
    """Assign a distinct available option to every dynamic dependency."""

    if not available_options:
        # TODO(SZI): Für solche neuen Fälle wäre eine Erklärung auch gut, z.B.
        # Suite does not have dynamic dependencies, nothing to assign
        return {}

    if any(not options for options in available_options.values()):
        return None

    option_owner: dict[str, str] = {}

    def try_assign(dep_name: str, seen: set[str]) -> bool:
        for option in sorted(available_options[dep_name]):
            if option in seen:
                continue

            seen.add(option)
            owner = option_owner.get(option)

            if owner is None or try_assign(owner, seen):
                option_owner[option] = dep_name
                return True

        return False

    for name in available_options:
        if not try_assign(name, set()):
            return None

    return {dep_name: option for option, dep_name in option_owner.items()}
