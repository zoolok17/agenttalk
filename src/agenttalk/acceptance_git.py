"""Command-local reuse of verified Git object/history reads, never live state."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps


_reads = ContextVar("acceptance_git_reads", default=None)


@contextmanager
def command_scope():
    # A nested CLI invocation also gets a fresh view. Reset on every exit,
    # including exceptions. Bare verification calls remain uncached; the
    # public resolver establishes an operation scope when called directly.
    token = _reads.set({})
    try:
        yield
    finally:
        _reads.reset(token)


def read_once(key, read):
    cache = _reads.get()
    if cache is None:
        return read()
    if key not in cache:
        cache[key] = read()
    # Callers must not be able to modify later readers' verified facts.
    return deepcopy(cache[key])


def operation(function):
    """Give a direct resolver call its own scope; share an enclosing CLI scope."""
    @wraps(function)
    def resolve(*args, **kwargs):
        if _reads.get() is not None:
            return function(*args, **kwargs)
        with command_scope():
            return function(*args, **kwargs)
    return resolve
