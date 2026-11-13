def resolve_dynamic_dependencies(
    available_options: dict[str, set[str]],
) -> dict[str, str] | None:
    """Assign a distinct available option to every dynamic dependency."""

    if not available_options:
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
