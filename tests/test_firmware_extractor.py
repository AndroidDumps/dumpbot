"""Unit tests for FirmwareExtractor cleanup behavior.

Regression coverage for original firmware archives being committed/pushed to
the dump repositories: the legacy extract_and_push.sh deleted the downloaded
archive (`rm -f "$FILE"`) after extraction, before `git add -A`. The Python
rewrite dropped that step, so every dump shipped its multi-GB source archive
(e.g. fastboot_lamuc_...zip) at the repo root. extract_firmware() must remove
the original archive after a successful extraction, on every dumper path.
"""

import shutil
from unittest.mock import AsyncMock, patch

from dumpyarabot.firmware_extractor import (
    FirmwareExtractor,
    _register_dumpyara_shutil_formats,
    _run_dumpyara,
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


def _unpack_format_names() -> set[str]:
    return {name for name, _extensions, _description in shutil.get_unpack_formats()}


def test_register_dumpyara_shutil_formats_registers_7z():
    """The .7z format must be registered with shutil."""
    _register_dumpyara_shutil_formats()

    assert "7z" in _unpack_format_names()


def test_register_dumpyara_shutil_formats_is_idempotent():
    """Repeated registration (one per job) must not raise."""
    _register_dumpyara_shutil_formats()
    _register_dumpyara_shutil_formats()

    assert "7z" in _unpack_format_names()


def test_run_dumpyara_registers_shutil_formats_before_extraction(tmp_path):
    """Formats must be registered before the direct dumpyara(...) call."""
    calls: list[str] = []

    def _record(name):
        def _callback(*_args, **_kwargs):
            calls.append(name)

        return _callback

    with (
        patch(
            "dumpyarabot.firmware_extractor.dumpyara_setup_shutil_formats",
            side_effect=_record("setup_shutil_formats"),
        ),
        patch(
            "dumpyarabot.firmware_extractor.dumpyara",
            side_effect=_record("dumpyara"),
        ),
        patch(
            "dumpyarabot.firmware_extractor.dumpyara_multipartitions.OTADUMP_EXECUTABLE",
            None,
        ),
    ):
        _run_dumpyara(tmp_path / "fw.7z", tmp_path)

    assert calls == ["setup_shutil_formats", "dumpyara"]
