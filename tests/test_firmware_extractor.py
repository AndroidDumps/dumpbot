"""Unit tests for FirmwareExtractor cleanup behavior.

Regression coverage for original firmware archives being committed/pushed to
the dump repositories: the legacy extract_and_push.sh deleted the downloaded
archive (`rm -f "$FILE"`) after extraction, before `git add -A`. The Python
rewrite dropped that step, so every dump shipped its multi-GB source archive
(e.g. fastboot_lamuc_...zip) at the repo root. extract_firmware() must remove
the original archive after a successful extraction, on every dumper path.
"""

import asyncio
import time
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from dumpyarabot.firmware_extractor import (
    FirmwareExtractor,
    JobCancelledError,
    _carry_forward_omitted_images,
)
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


def test_carry_forward_omitted_partition(tmp_path):
    current = tmp_path / "current"
    next_stage = tmp_path / "next"
    current.mkdir()
    next_stage.mkdir()
    (current / "system.img").write_bytes(b"unchanged")
    (next_stage / "vendor.img").write_bytes(b"changed")

    _carry_forward_omitted_images(current, next_stage)

    assert (next_stage / "system.img").read_bytes() == b"unchanged"
    assert (next_stage / "vendor.img").read_bytes() == b"changed"


def test_raw_archive_rejects_unsafe_member(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../system.img", b"not safe")

    with pytest.raises(ValueError, match="unsafe path"):
        FirmwareExtractor.is_raw_image_archive(str(archive))


def test_raw_archive_has_expansion_bound(tmp_path):
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("system.img", b"image")

    with (
        patch("dumpyarabot.firmware_extractor.MAX_ARCHIVE_MEMBER_SIZE", 1),
        pytest.raises(ValueError, match="exceeds the allowed size"),
    ):
        FirmwareExtractor.is_raw_image_archive(str(archive))


async def test_delta_chain_calls_full_then_ordered_deltas(tmp_path):
    base = tmp_path / "base.zip"
    with zipfile.ZipFile(base, "w") as output:
        output.writestr("payload.bin", b"payload")
    delta_one = tmp_path / "delta-one.bin"
    delta_two = tmp_path / "delta-two.bin"
    delta_one.write_bytes(b"one")
    delta_two.write_bytes(b"two")

    extractor = FirmwareExtractor(str(tmp_path))
    calls = []

    async def run_otadump(payload, output, *, source_dir=None, **kwargs):
        calls.append(
            (payload.name, output.name, source_dir.name if source_dir else None)
        )

    with patch.object(extractor, "_run_otadump", side_effect=run_otadump), patch.object(
        extractor, "_run_blocking_and_drain", new=AsyncMock()
    ):
        await extractor.extract_reconstructed_firmware(
            _make_job(use_alt_dumper=False),
            [str(base), str(delta_one), str(delta_two)],
            base_is_raw=False,
        )

    assert calls == [
        ("base.zip", "stage_000", None),
        ("delta-one.bin", "stage_001", "stage_000"),
        ("delta-two.bin", "stage_002", "stage_001"),
    ]


async def test_drain_waits_for_worker_after_caller_cancellation(tmp_path):
    finished = asyncio.Event()
    extractor = FirmwareExtractor(str(tmp_path))

    async def worker_body():
        await finished.wait()

    worker = asyncio.create_task(worker_body())
    drain = asyncio.create_task(extractor._drain_task(worker))
    await asyncio.sleep(0)
    drain.cancel()
    await asyncio.sleep(0)
    assert not drain.done()

    finished.set()
    assert await drain is None


async def test_blocking_stage_honors_cooperative_cancellation(tmp_path):
    extractor = FirmwareExtractor(str(tmp_path))

    def slow_stage():
        time.sleep(0.05)

    async def cancelled():
        return True

    with pytest.raises(JobCancelledError, match="cancelled"):
        await extractor._run_blocking_and_drain(
            slow_stage,
            cancellation_check=cancelled,
        )


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
