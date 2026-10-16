"""Output on a console whose code page cannot hold the report's characters.

Measured on a cp936 Windows machine: `--pretty` and `doctor` died with
UnicodeEncodeError on the first `✓`, as exit 1 — the code for "searched fine,
found nothing" — and doctor lost probe results it had already paid for.
"""

from __future__ import annotations

import io
import subprocess
import sys

from sought.cli import ensure_utf8


def _legacy_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp936")


def test_a_legacy_code_page_is_switched_to_utf8():
    stream = _legacy_stream()
    ensure_utf8(stream)
    stream.write("✓ — → 中文")
    stream.flush()
    assert stream.buffer.getvalue().decode("utf-8") == "✓ — → 中文"


def test_a_utf8_stream_is_left_alone():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    ensure_utf8(stream)
    assert stream.errors == "strict"


def test_the_error_handler_survives_the_switch():
    """Python gives stderr backslashreplace; losing it would make diagnostics a crash path."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936", errors="backslashreplace")
    ensure_utf8(stream)
    assert stream.errors == "backslashreplace"


def test_a_stream_that_cannot_be_reconfigured_is_left_alone():
    ensure_utf8(io.StringIO())


def test_the_console_script_survives_a_legacy_code_page():
    """End to end, through the real entry point, with the locale forced to cp936."""
    code = "import sys; sys.argv=['sought','doctor','--offline']; from sought.cli import entrypoint; entrypoint()"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        env={**_minimal_env(), "PYTHONIOENCODING": "cp936"},
        check=False,
    )
    assert b"UnicodeEncodeError" not in result.stderr
    # The report ran to its verdict. Exit 2 is a legitimate outcome here (no
    # Chromium on a CI runner); exit 1 is what the crash used to look like.
    assert "Overall:" in result.stdout.decode("utf-8")
    assert result.returncode != 1


def _minimal_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k.upper() not in {"PYTHONIOENCODING", "PYTHONUTF8"}}
