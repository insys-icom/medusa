def resolve_dynamic_dependencies(
    dynamic_dependencies: dict[str, set[str]],
) -> dict[str, str] | None:
    """Resolve dynamic dependencies by assigning distinct available options.

    Args:
        dynamic_dependencies: A dictionary mapping each dynamic dependency
                              name to a set of possible devices.

    Returns:
        A dictionary mapping each dependency name to its assigned device.
        Returns an empty dictionary if there are no dynamic dependencies.
        Returns None if no valid assignment exists.
    """

    if not dynamic_dependencies:
        # Suite does not have dynamic dependencies -> nothing to assign
        return {}

    # Every dynamic dependency needs at least one possible device
    # If any dependency has no options, a complete assignment is impossible
    if any(not options for options in dynamic_dependencies.values()):
        return None

    # Maps each assigned device to the dependency that currently owns it
    # This makes it possible to find and rearrange existing assignments
    option_owner: dict[str, str] = {}

    def try_assign(dep_name: str, seen: set[str]) -> bool:
        # Try each possible device in a deterministic order
        for option in sorted(dynamic_dependencies[dep_name]):
            # Do not examine the same device twice during one assignment attempt
            if option in seen:
                continue

            seen.add(option)
            owner = option_owner.get(option)

            # If the device is free, assign it directly
            # If it is already assigned, try to move its current owner to another
            # device before taking it over.
            if owner is None or try_assign(owner, seen):
                option_owner[option] = dep_name
                return True

        # No available device could be assigned to this dependency
        return False

    # Try to assign a distinct device to every dynamic dependency
    # If even one dependency cannot be assigned, no complete matching exists
    for name in dynamic_dependencies:
        if not try_assign(name, set()):
            return None

    # The internal mapping is device -> dependency
    # Convert it to the expected dependency -> device mapping
    return {dep_name: option for option, dep_name in option_owner.items()}
