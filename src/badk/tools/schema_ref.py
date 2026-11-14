"""A tool `output` schema must not fetch: find a remote `$ref`."""


def remote_ref(node: object) -> str | None:
    """The first `$ref` that does not start with `#` (a fetch at compile time), else None."""
    items = list(node.values()) if isinstance(node, dict) else node
    if isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"][:1] != "#":
        return str(node["$ref"])
    if not isinstance(items, list):
        return None
    return next((r for i in items if (r := remote_ref(i))), None)
