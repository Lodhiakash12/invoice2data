"""Runs every template pattern with a time limit.

Python's built-in `re` module can take exponentially longer on some patterns: in the audit, the
Meesho buyer pattern ran for more than 60 s on a page of OCR text, and nothing could stop it. This
module switches invoice2data to the `regex` package (already installed with invoice2data, same
syntax as `re`) and gives every call a time limit. When the limit is hit, the call raises
PatternTimeout and only the template that owns the pattern fails; the run goes on.

Call install() once at start-up (process_invoice.py and the tests do).
"""

from __future__ import annotations

from functools import lru_cache

import regex

DEFAULT_TIMEOUT = 2.0          # seconds per pattern call; settings.yml: regex_timeout_seconds

_timeout = DEFAULT_TIMEOUT
_installed = False


class PatternTimeout(TimeoutError):
    """A template pattern ran longer than the time limit on this text."""


@lru_cache(maxsize=8192)
def compile(pattern: str, flags: int = 0):  # noqa: A001 - same name as invoice2data's wrapper
    return regex.compile(pattern, flags)


def _guard(call, pattern):
    try:
        return call()
    except TimeoutError:
        raise PatternTimeout(f"pattern took longer than {_timeout:g} s: {pattern[:80]!r}") from None


def search(pattern: str, string: str, flags: int = 0):
    return _guard(lambda: compile(pattern, flags).search(string, timeout=_timeout), pattern)


def match(pattern: str, string: str, flags: int = 0):
    return _guard(lambda: compile(pattern, flags).match(string, timeout=_timeout), pattern)


def findall(pattern: str, string: str, flags: int = 0):
    return _guard(lambda: compile(pattern, flags).findall(string, timeout=_timeout), pattern)


def finditer(pattern: str, string: str, flags: int = 0):
    # materialised so that the time limit covers the whole iteration
    return iter(_guard(lambda: list(compile(pattern, flags).finditer(string, timeout=_timeout)), pattern))


def split(pattern: str, string: str, maxsplit: int = 0, flags: int = 0):
    return _guard(lambda: compile(pattern, flags).split(string, maxsplit, timeout=_timeout), pattern)


def sub(pattern: str, repl: str, string: str, count: int = 0, flags: int = 0) -> str:
    return _guard(lambda: compile(pattern, flags).sub(repl, string, count, timeout=_timeout), pattern)


class _Pattern:
    """A compiled `regex` pattern whose matching methods always carry the time limit."""

    def __init__(self, compiled):
        self._p = compiled

    def __getattr__(self, name):          # groups, groupindex, pattern, flags ...
        return getattr(self._p, name)

    def search(self, string, *args):
        return _guard(lambda: self._p.search(string, *args, timeout=_timeout), self._p.pattern)

    def match(self, string, *args):
        return _guard(lambda: self._p.match(string, *args, timeout=_timeout), self._p.pattern)

    def fullmatch(self, string, *args):
        return _guard(lambda: self._p.fullmatch(string, *args, timeout=_timeout), self._p.pattern)

    def findall(self, string, *args):
        return _guard(lambda: self._p.findall(string, *args, timeout=_timeout), self._p.pattern)

    def finditer(self, string, *args):
        return iter(_guard(lambda: list(self._p.finditer(string, *args, timeout=_timeout)), self._p.pattern))

    def split(self, string, maxsplit=0):
        return _guard(lambda: self._p.split(string, maxsplit, timeout=_timeout), self._p.pattern)

    def sub(self, repl, string, count=0):
        return _guard(lambda: self._p.sub(repl, string, count, timeout=_timeout), self._p.pattern)


class _Engine:
    """Stands in for the `re` module inside invoice2data: compile() returns guarded patterns."""

    __name__ = "regex"

    @staticmethod
    def compile(pattern, flags=0):
        return _Pattern(regex.compile(pattern, flags))


def install(timeout_seconds: float | None = None) -> bool:
    """Route invoice2data's pattern calls through the time limit (idempotent).

    invoice2data's own wrapper module may be compiled, so its functions cannot be replaced;
    instead the engine it compiles patterns with is replaced and its pattern cache emptied.
    Returns False when this invoice2data version has no such wrapper (no time limit then)."""
    global _timeout, _installed
    if timeout_seconds is not None:
        _timeout = float(timeout_seconds)
    if _installed:
        return True
    try:
        from invoice2data.extract import _regex as target
    except ImportError:
        return False
    if not hasattr(target, "_engine"):
        return False
    target._engine = _Engine
    if hasattr(target.compile, "cache_clear"):
        target.compile.cache_clear()
    _installed = True
    return True


def installed() -> bool:
    return _installed


def timeout() -> float:
    return _timeout
