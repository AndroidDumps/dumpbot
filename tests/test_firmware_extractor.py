"""Unit tests for FirmwareExtractor cleanup behavior.

Regression coverage for original firmware archives being committed/pushed to
the dump repositories: the legacy extract_and_push.sh deleted the downloaded
archive (`rm -f "$FILE"`) after extraction, before `git add -A`. The Python
rewrite dropped that step, so every dump shipped its multi-GB source archive
(e.g. fastboot_lamuc_...zip) at the repo root. extract_firmware() must remove
the original archive after a successful extraction, on every dumper path.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dumpyarabot.firmware_extractor import (
    FirmwareExtractionError,
    FirmwareExtractor,
    summarize_dumper_failure,
)
from dumpyarabot.process_utils import ProcessException, ProcessResult
from dumpyarabot.schemas import DumpArguments, DumpJob


def _make_job(use_alt_dumper: bool) -> DumpJob:
    return DumpJob(
        job_id="test-job",
        dump_args=DumpArguments(
            url="https://example.com/fw.zip",
            use_alt_dumper=use_alt_dumper,
            use_privdump=False,
        ),
    )


async def test_extract_firmware_removes_archive_python_dumper(tmp_path):
    """The downloaded archive must be gone after Python-dumper extraction."""
    archive = tmp_path / "fastboot_lamuc.zip"
    archive.write_bytes(b"firmware-archive-bytes")
    extracted = tmp_path / "boot.img"
    extracted.write_bytes(b"extracted-content")

    extractor = FirmwareExtractor(str(tmp_path))

    with patch.object(
        extractor,
        "_extract_with_python_dumper",
        new=AsyncMock(return_value=str(tmp_path)),
    ):
        await extractor.extract_firmware(_make_job(use_alt_dumper=False), str(archive))

    assert not archive.exists(), "original firmware archive should be deleted"
    assert extracted.exists(), "extracted content must be preserved"


async def test_extract_firmware_removes_archive_alt_dumper(tmp_path):
    """The downloaded archive must also be gone after alternative-dumper extraction."""
    archive = tmp_path / "SM-S938B_EUX_ODIN.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    extractor = FirmwareExtractor(str(tmp_path))

    with patch.object(
        extractor,
        "_extract_with_alternative_dumper",
        new=AsyncMock(return_value=str(tmp_path)),
    ):
        await extractor.extract_firmware(_make_job(use_alt_dumper=True), str(archive))

    assert not archive.exists(), "original firmware archive should be deleted"


# --- Silent dumper failures -------------------------------------------------
#
# otadump reports errors through an indicatif progress bar (hidden when stdout
# is not a terminal) and its main() throws the extraction result away, so it
# exits 0 whether or not it extracted anything. dumpyara trusts that exit code,
# carries on with no partition images, and dies on an unrelated-looking
# assertion: "System folder doesn't exist". The extractor has to notice that a
# dumper produced nothing and try the other one, whose payload extractor is a
# different binary (Firmware_extractor ships its own under tools/).

DUMPYARA_SILENT_OTADUMP_OUTPUT = """[INFO] Step 1 - Extracting archive
[INFO] Step 2 - Preparing partition images
[INFO] Found multipartition image: payload.bin
[INFO] Extracting payload.bin with otadump (/usr/local/bin/otadump)
[INFO] Step 3 - Extracting partitions
Traceback (most recent call last):
  File "/x/dumpyara/dumpyara.py", line 64, in dumpyara
    assert (output_path / "system").exists(), "System folder doesn't exist"
AssertionError: System folder doesn't exist
"""


def _make_extracted_system(work_dir) -> None:
    """Create the minimum output a successful extraction leaves behind."""
    system_dir = work_dir / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "build.prop").write_text("ro.build.id=TEST\n")


async def test_python_dumper_failure_falls_back_to_alt_dumper(tmp_path):
    """A failed Python dumper run must not fail the job outright."""
    archive = tmp_path / "ota.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    extractor = FirmwareExtractor(str(tmp_path))
    statuses = []

    async def record_status(message: str) -> None:
        statuses.append(message)

    async def fake_alt_dumper(_firmware_path):
        _make_extracted_system(tmp_path)
        return str(tmp_path)

    with patch.object(
        extractor,
        "_extract_with_python_dumper",
        new=AsyncMock(side_effect=FirmwareExtractionError("otadump extracted nothing")),
    ), patch.object(
        extractor, "_extract_with_alternative_dumper", new=AsyncMock(wraps=fake_alt_dumper)
    ) as alt_dumper:
        result = await extractor.extract_firmware(
            _make_job(use_alt_dumper=False), str(archive), on_status=record_status
        )

    assert result == str(tmp_path)
    alt_dumper.assert_awaited_once_with(str(archive))
    assert statuses, "the fallback should be reported back to the job"
    assert not archive.exists(), "original firmware archive should be deleted"


async def test_python_dumper_reporting_success_without_partitions_falls_back(tmp_path):
    """Exit code 0 with an empty output directory counts as a failure."""
    archive = tmp_path / "ota.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    extractor = FirmwareExtractor(str(tmp_path))

    async def fake_alt_dumper(_firmware_path):
        _make_extracted_system(tmp_path)
        return str(tmp_path)

    # run_command returning without raising is what a silent otadump failure
    # looks like from dumpyarabot's side.
    with patch(
        "dumpyarabot.firmware_extractor.run_command", new=AsyncMock(return_value=None)
    ), patch.object(
        extractor, "_extract_with_alternative_dumper", new=AsyncMock(wraps=fake_alt_dumper)
    ) as alt_dumper:
        await extractor.extract_firmware(_make_job(use_alt_dumper=False), str(archive))

    alt_dumper.assert_awaited_once_with(str(archive))


async def test_extraction_error_reports_both_dumpers(tmp_path):
    """When neither dumper works, the message must name both failures."""
    archive = tmp_path / "ota.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    extractor = FirmwareExtractor(str(tmp_path))

    with patch.object(
        extractor,
        "_extract_with_python_dumper",
        new=AsyncMock(side_effect=FirmwareExtractionError("otadump extracted nothing")),
    ), patch.object(
        extractor,
        "_extract_with_alternative_dumper",
        new=AsyncMock(side_effect=RuntimeError("extractor.sh: unsupported archive")),
    ):
        with pytest.raises(FirmwareExtractionError) as excinfo:
            await extractor.extract_firmware(
                _make_job(use_alt_dumper=False), str(archive)
            )

    message = str(excinfo.value)
    assert "otadump extracted nothing" in message
    assert "unsupported archive" in message
    assert archive.exists(), "the archive must survive a failed extraction"


async def test_python_dumper_wraps_process_failure_with_readable_reason(tmp_path):
    """A dumpyara traceback becomes one line that names the real cause."""
    extractor = FirmwareExtractor(str(tmp_path))
    failure = ProcessException(
        "Command failed: uvx",
        ProcessResult(returncode=1, stderr=DUMPYARA_SILENT_OTADUMP_OUTPUT),
    )

    with patch(
        "dumpyarabot.firmware_extractor.run_command", new=AsyncMock(side_effect=failure)
    ):
        with pytest.raises(FirmwareExtractionError) as excinfo:
            await extractor._extract_with_python_dumper(str(tmp_path / "ota.zip"))

    assert "otadump" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


async def test_leftover_dumper_temp_dirs_are_removed_before_retry(tmp_path):
    """dumpyara scratch dirs from a killed run must not reach the dump repo."""
    archive = tmp_path / "ota.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    leftover = tmp_path / "temp_raw_images"
    leftover.mkdir()
    (leftover / "system.img").write_bytes(b"partial")

    extractor = FirmwareExtractor(str(tmp_path))

    async def fake_alt_dumper(_firmware_path):
        _make_extracted_system(tmp_path)
        return str(tmp_path)

    with patch.object(
        extractor,
        "_extract_with_python_dumper",
        new=AsyncMock(side_effect=FirmwareExtractionError("boom")),
    ), patch.object(
        extractor, "_extract_with_alternative_dumper", new=AsyncMock(wraps=fake_alt_dumper)
    ):
        await extractor.extract_firmware(_make_job(use_alt_dumper=False), str(archive))

    assert not leftover.exists()


def test_summarize_dumper_failure_explains_silent_otadump():
    summary = summarize_dumper_failure(DUMPYARA_SILENT_OTADUMP_OUTPUT)

    assert "otadump" in summary
    assert "payload.bin" in summary
    assert "\n" not in summary


def test_summarize_dumper_failure_falls_back_to_last_output_line():
    summary = summarize_dumper_failure("[INFO] Step 1\nRuntimeError: 7z is missing\n")

    assert summary == "RuntimeError: 7z is missing"


def test_summarize_dumper_failure_handles_no_output():
    assert summarize_dumper_failure("") == "the dumper failed without producing any output"


async def test_dumper_timeout_is_not_retried(tmp_path):
    """The job budget is 2h, so a timed-out hour is not spent twice."""
    archive = tmp_path / "ota.zip"
    archive.write_bytes(b"firmware-archive-bytes")

    extractor = FirmwareExtractor(str(tmp_path))
    timeout = ProcessException(
        "Command timed out after 3600.0s: uvx --from ... dumpyara",
        ProcessResult(returncode=-1, stderr="Command timed out", timeout_occurred=True),
    )

    with patch(
        "dumpyarabot.firmware_extractor.run_command", new=AsyncMock(side_effect=timeout)
    ), patch.object(
        extractor, "_extract_with_alternative_dumper", new=AsyncMock()
    ) as alt_dumper:
        with pytest.raises(FirmwareExtractionError) as excinfo:
            await extractor.extract_firmware(
                _make_job(use_alt_dumper=False), str(archive)
            )

    alt_dumper.assert_not_awaited()
    assert "timed out" in str(excinfo.value)
