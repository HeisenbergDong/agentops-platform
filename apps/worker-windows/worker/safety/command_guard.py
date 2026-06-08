ALLOWED_COMMAND_PREFIXES = {
    ("npm", "run"),
    ("npm", "test"),
    ("python", "-m"),
    ("pytest",),
    ("go", "test"),
    ("mvn", "test"),
}


def assert_allowed_command(command: list[str]) -> None:
    if not command:
        raise ValueError("Empty command")
    normalized = tuple(command[:2]) if len(command) >= 2 else tuple(command)
    if normalized in ALLOWED_COMMAND_PREFIXES:
        return
    one = (command[0],)
    if one in ALLOWED_COMMAND_PREFIXES:
        return
    raise ValueError(f"Command is not allowed: {command}")
