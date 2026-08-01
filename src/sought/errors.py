"""Error types carrying the process exit code they should produce."""

from __future__ import annotations


class SoughtError(Exception):
    """A failure sought anticipated. Reported as a message, never as a traceback.

    Anything that escapes as a bare exception is a bug and should keep its
    traceback, so resist the urge to widen this into a catch-all.
    """

    exit_code = 2


class ConfigError(SoughtError):
    """The config file is missing, unreadable, or malformed."""

    exit_code = 2


class NoResultsError(SoughtError):
    """Every source ran without error and none of them matched anything.

    Distinct from `UpstreamError`: this means the web has no answer, not that
    sought failed to ask.
    """

    exit_code = 1


class UpstreamError(SoughtError):
    """Every source failed to answer — rate limited, blocked, or timed out."""

    exit_code = 2


class UsageError(SoughtError):
    """The command line was wrong."""

    exit_code = 2


class InvariantError(SoughtError):
    """sought produced output it is not allowed to produce.

    Specifically: zero results and an empty `attempts` list, which claims a
    search happened and then says nothing about it. That is a bug in sought,
    not a fact about the web, so it exits 2 (something malfunctioned) and
    never 1 (the web has no answer) — `all()` over an empty list is True, so
    the unguarded path fell into exactly the wrong one of those two.
    """

    exit_code = 2
