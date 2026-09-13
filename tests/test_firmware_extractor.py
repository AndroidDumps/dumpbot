"""Unit tests for FirmwareExtractor cleanup behavior.

Regression coverage for original firmware archives being committed/pushed to
the dump repositories: the legacy extract_and_push.sh deleted the downloaded
archive (`rm -f "$FILE"`) after extraction, before `git add -A`. The Python
rewrite dropped that step, so every dump shipped its multi-GB source archive
(e.g. fastboot_lamuc_...zip) at the repo root. extract_firmware() must remove
the original archive after a successful extraction, on every dumper path.
"""

from unittest.mock import AsyncMock, patch

from dumpyarabot import firmware_extractor as firmware_extractor_module
from dumpyarabot import url_utils
from dumpyarabot.firmware_extractor import FirmwareExtractor
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


async def test_extract_delta_chain_consumes_only_consecutive_delta_urls(tmp_path, monkeypatch):
    """Delta extraction keeps a strict URL-chain contract and keeps changelog text out of the chain."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    archives = [tmp_path / "base.zip", tmp_path / "delta1.zip", tmp_path / "delta2.zip"]
    for archive in archives:
        archive.write_bytes(b"archive")

    backend_calls: list[tuple[str, str | None]] = []

    def fake_otadump_extract(payload_path, output_dir, **kwargs):
        output = output_dir
        output.mkdir(parents=True, exist_ok=True)
        (output / "system.img").write_bytes(b"img")
        source = kwargs.get("source_dir")
        backend_calls.append((payload_path.name, None if source is None else source.name))

    def fake_extract_images(_images_path, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "system").mkdir()
        (output_dir / "system" / "build.prop").write_text("ro.product=demo", encoding="utf-8")

    async def fake_run_thread(function, *args, timeout, token, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(firmware_extractor_module.otadump, "extract", fake_otadump_extract)
    monkeypatch.setattr(firmware_extractor_module, "_run_dumpyara_images", fake_extract_images)
    monkeypatch.setattr(firmware_extractor_module, "_run_thread", fake_run_thread)

    extractor = FirmwareExtractor(str(work_dir))
    result = await extractor.extract_delta_chain(_make_job(use_alt_dumper=False), [str(p) for p in archives])

    assert result == str(work_dir)
    assert backend_calls == [
        ("base.zip", None),
        ("delta1.zip", "stage_000"),
        ("delta2.zip", "stage_001"),
    ]
    assert (work_dir / "system").exists()
    assert all(not archive.exists() for archive in archives)


def test_url_parsers_support_direct_and_moderated_old_and_new_forms():
    direct_old_urls, direct_old_options = url_utils.parse_dump_tokens([
        "https://example.com/base.zip"
    ])
    direct_new_urls, direct_new_options = url_utils.parse_dump_tokens([
        "https://example.com/base.zip",
        "https://example.com/delta.zip",
        "af",
    ])
    moderated_old_text, moderated_old_urls = url_utils.parse_moderated_request(
        "#request https://example.com/base.zip"
    )
    moderated_new_text, moderated_new_urls = url_utils.parse_moderated_request(
        "Need dump #request please review https://example.com/base.zip "
        "https://example.com/delta.zip changelog https://example.com/changelog"
    )

    assert direct_old_urls == ["https://example.com/base.zip"]
    assert direct_old_options == ""
    assert direct_new_urls == [
        "https://example.com/base.zip",
        "https://example.com/delta.zip",
    ]
    assert direct_new_options == "af"
    assert moderated_old_text == ""
    assert moderated_old_urls == ["https://example.com/base.zip"]
    assert moderated_new_text == (
        "Need dump please review changelog https://example.com/changelog"
    )
    assert moderated_new_urls == [
        "https://example.com/base.zip",
        "https://example.com/delta.zip",
    ]
