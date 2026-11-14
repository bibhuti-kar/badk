"""Retry once (design 7.8, RES-1): exactly two attempts, never more."""

from collections.abc import Callable

from badk.errors import BadkError, PersistenceError


def once[T](fn: Callable[[], T], on_retry: Callable[[BadkError], None]) -> T:
    """Call `fn`; on a BadkError report it and call again. A second error, a non-BadkError and a
    PersistenceError (the sink is down: retrying would re-publish) propagate."""
    try:
        return fn()
    except PersistenceError:
        raise
    except BadkError as e:
        on_retry(e)
    return fn()
