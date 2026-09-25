"""Command-local reuse of verified Git object/history reads, never live state."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


_reads = ContextVar("acceptance_git_reads", default=None)


@contextmanager
def command_scope():
    # A nested CLI invocation also gets a fresh view. Reset on every exit,
    # including exceptions; library calls outside the CLI remain uncached.
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
