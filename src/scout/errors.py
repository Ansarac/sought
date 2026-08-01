"""Error types carrying the process exit code they should produce."""

from __future__ import annotations


class ScoutError(Exception):
    """A failure scout anticipated. Reported as a message, never as a traceback.

    Anything that escapes as a bare exception is a bug and should keep its
    traceback, so resist the urge to widen this into a catch-all.
    """

    exit_code = 2


class ConfigError(ScoutError):
    """The config file is missing, unreadable, or malformed."""

    exit_code = 2


class NoResultsError(ScoutError):
    """Every source ran without error and none of them matched anything.

    Distinct from `UpstreamError`: this means the web has no answer, not that
    scout failed to ask.
    """

    exit_code = 1


class UpstreamError(ScoutError):
    """Every source failed to answer — rate limited, blocked, or timed out."""

    exit_code = 2


class UsageError(ScoutError):
    """The command line was wrong."""

    exit_code = 2
