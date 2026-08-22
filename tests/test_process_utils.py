"""Unit tests for run_command's error propagation.

`check=True` raises ProcessException from inside run_command's own try block, so
the catch-all `except Exception` used to swallow and re-wrap it. That dropped the
captured ProcessResult — leaving callers unable to read the failing tool's output
— and produced doubled messages such as
"Command failed: uvx - Command failed: uvx --from ...".
"""

import pytest

from dumpyarabot.process_utils import ProcessException, run_command


async def test_check_failure_preserves_result_and_output():
    with pytest.raises(ProcessException) as excinfo:
        await run_command(
            "sh", "-c", "echo to-stdout; echo to-stderr >&2; exit 3",
            check=True,
            quiet=True,
        )

    error = excinfo.value
    assert error.result is not None, "the ProcessResult must survive"
    assert error.result.returncode == 3
    assert "to-stdout" in error.result.stdout
    assert "to-stderr" in error.result.stderr
    assert str(error).count("Command failed:") == 1


async def test_check_success_returns_result():
    result = await run_command("sh", "-c", "echo ok", check=True, quiet=True)

    assert result.success
    assert result.stdout.strip() == "ok"


async def test_missing_executable_is_still_wrapped():
    with pytest.raises(ProcessException) as excinfo:
        await run_command("dumpyarabot-no-such-binary", check=True, quiet=True)

    assert "dumpyarabot-no-such-binary" in str(excinfo.value)
