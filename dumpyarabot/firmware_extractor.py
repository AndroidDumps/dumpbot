import asyncio
import re
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import otadump
import py7zr
from dumpyara.dumpyara import dumpyara
from dumpyara.steps.extract_images import extract_images as dumpyara_extract_images
from dumpyara.utils import multipartitions as dumpyara_multipartitions
from dumpyara.utils.partitions import get_partition_names
from py7zr.io import Py7zIO, WriterFactory
from rich.console import Console

from dumpyarabot.file_utils import (
    find_files_by_pattern,
    move_file_to_root,
    safe_remove_file,
)
from dumpyarabot.process_utils import (
    HALF_HOUR,
    ONE_HOUR,
    run_analysis_command,
    run_command,
    run_extraction_command,
    run_git_command,
)
from dumpyarabot.schemas import DumpJob

console = Console()

def _run_dumpyara(firmware_path: Path, output_path: Path) -> None:
    """Run Dumpyara with its reliable in-process payload parser."""
    dumpyara_multipartitions.OTADUMP_EXECUTABLE = None
    dumpyara_multipartitions.extract_payload_native = None
    dumpyara(firmware_path, output_path)


def _run_dumpyara_images(images_path: Path, output_path: Path) -> None:
    """Run dumpyara's existing filesystem-image extraction step once."""
    dumpyara_extract_images(images_path, output_path)
    system_path = output_path / "system"
    if not system_path.exists() or not any(system_path.iterdir()):
        raise RuntimeError("System filesystem extraction did not produce files")


class JobCancelledError(Exception):
    """Raised after cooperative native extraction has fully stopped."""


class NativeExtractionCancelled(JobCancelledError):
    """Internal form of otadump's cooperative KeyboardInterrupt."""


CancellationCheck = Callable[[], Awaitable[bool]]
ANDROID_SPARSE_MAGIC = b"\x3a\xff\x26\xed"


async def _check_cancelled(cancellation_check: CancellationCheck | None) -> bool:
    """Safely poll cooperative cancellation without failing on callback errors."""
    if cancellation_check is None:
        return False
    try:
        return await cancellation_check()
    except Exception as e:
        console.print(f"[yellow]Cancellation check error (ignored): {e}[/yellow]")
        return False


class _ArchiveImageIO(Py7zIO):
    def __init__(self, path: Path):
        self._file = path.open("x+b")

    def write(self, data: bytes | bytearray) -> int:
        return self._file.write(data)

    def read(self, size: int | None = None) -> bytes:
        return self._file.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def flush(self) -> None:
        self._file.flush()

    def size(self) -> int:
        position = self._file.tell()
        self._file.seek(0, 2)
        size = self._file.tell()
        self._file.seek(position)
        return size

    def close(self) -> None:
        self._file.close()


class _ArchiveImageFactory(WriterFactory):
    def __init__(self, destination: Path):
        self.destination = destination

    def create(self, filename: str) -> Py7zIO:
        name = _safe_archive_member(filename).name
        return _ArchiveImageIO(self.destination / name)


class _ImageMagicIO(Py7zIO):
    def __init__(self):
        self.magic = bytearray()
        self.position = 0

    def write(self, data: bytes | bytearray) -> int:
        if len(self.magic) < 4:
            self.magic.extend(data[: 4 - len(self.magic)])
        self.position += len(data)
        return len(data)

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.position = offset
        elif whence == 1:
            self.position += offset
        return self.position

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return self.position


class _ImageMagicFactory(WriterFactory):
    def __init__(self):
        self.outputs: list[_ImageMagicIO] = []

    def create(self, filename: str) -> Py7zIO:
        output = _ImageMagicIO()
        self.outputs.append(output)
        return output


def _safe_archive_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise ValueError("Archive contains an unsafe path")
    return path


def _raw_image_names_are_ready(names: Sequence[str]) -> bool:
    """Exclude containers and slot layouts that need dumpyara preparation."""
    stems = [PurePosixPath(name.replace("\\", "/")).stem for name in names]
    if not stems or any(
        stem == "super" or stem.endswith(("_a", "_b")) for stem in stems
    ):
        return False
    supported_partitions = set(get_partition_names())
    return any(stem in supported_partitions for stem in stems)


def _archive_member_needs_preparation(path: PurePosixPath) -> bool:
    """Keep known Android partition containers on the legacy extraction path."""
    name = path.name.lower()
    return name == "payload.bin" or name.endswith(
        (
            ".new.dat",
            ".new.dat.br",
            ".patch.dat",
            ".transfer.list",
            ".img.br",
            ".img.gz",
            ".img.lz4",
            ".img.xz",
            ".img.zst",
        )
    )


def _unpack_raw_image_archive(archive_path: Path, destination: Path) -> None:
    """Validate and flatten regular .img members without shelling out."""
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            zip_image_members: list[zipfile.ZipInfo] = []
            for info in archive.infolist():
                member_path = _safe_archive_member(info.filename)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or file_type not in {
                    0,
                    stat.S_IFREG,
                    stat.S_IFDIR,
                }:
                    raise ValueError("Archive contains a link or special file")
                if info.is_dir():
                    continue
                if member_path.suffix.lower() == ".img":
                    key = member_path.name.casefold()
                    if key in seen:
                        raise ValueError(
                            f"Archive contains duplicate partition image basename: {member_path.name}"
                        )
                    seen.add(key)
                    zip_image_members.append(info)
            for info in zip_image_members:
                name = PurePosixPath(info.filename.replace("\\", "/")).name
                with archive.open(info) as source, (destination / name).open("xb") as target:
                    shutil.copyfileobj(source, target)

    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, mode="r:*") as archive:
            tar_image_members: list[tarfile.TarInfo] = []
            for member in archive.getmembers():
                member_path = _safe_archive_member(member.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ValueError("Archive contains a link or special file")
                if member_path.suffix.lower() == ".img":
                    key = member_path.name.casefold()
                    if key in seen:
                        raise ValueError(
                            f"Archive contains duplicate partition image basename: {member_path.name}"
                        )
                    seen.add(key)
                    tar_image_members.append(member)
            for member in tar_image_members:
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("Could not read archive image entry")
                name = PurePosixPath(member.name.replace("\\", "/")).name
                with extracted, (destination / name).open("xb") as target:
                    shutil.copyfileobj(extracted, target)

    elif archive_path.name.lower().endswith(".7z"):
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            image_names: list[str] = []
            for info in archive.list():
                member_path = _safe_archive_member(info.filename)
                if info.is_directory:
                    continue
                if info.is_symlink or not info.is_file:
                    raise ValueError("Archive contains a link or special file")
                if member_path.suffix.lower() == ".img":
                    key = member_path.name.casefold()
                    if key in seen:
                        raise ValueError(
                            f"Archive contains duplicate partition image basename: {member_path.name}"
                        )
                    seen.add(key)
                    image_names.append(info.filename)

            archive.extract(
                targets=image_names,
                factory=_ArchiveImageFactory(destination),
            )
    else:
        raise ValueError("Base input is not a supported raw-image archive")

    if not seen:
        raise ValueError("Raw-image archive contains no .img files")


def _carry_forward_omitted_images(current_stage: Path, next_stage: Path) -> None:
    """Copy unchanged partition bytes into a successfully extracted next stage."""
    for source_image in current_stage.glob("*.img"):
        destination = next_stage / source_image.name
        if not destination.exists():
            shutil.copy2(source_image, destination)


class FirmwareExtractor:
    """Handles firmware extraction using both Python dumper and alternative methods."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.firmware_extractor_path = Path.home() / "Firmware_extractor"

    @staticmethod
    def is_raw_image_archive(firmware_path: str) -> bool:
        """Identify extraction-ready raw images while preserving legacy containers."""
        path = Path(firmware_path)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                zip_image_files: list[zipfile.ZipInfo] = []
                image_basenames: set[str] = set()
                for info in archive.infolist():
                    member_path = _safe_archive_member(info.filename)
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if stat.S_ISLNK(mode) or file_type not in {
                        0,
                        stat.S_IFREG,
                        stat.S_IFDIR,
                    }:
                        return False
                    if info.is_dir():
                        continue
                    if _archive_member_needs_preparation(member_path):
                        return False
                    if member_path.suffix.lower() == ".img":
                        basename = member_path.name.casefold()
                        if basename in image_basenames:
                            return False
                        image_basenames.add(basename)
                        zip_image_files.append(info)
                return _raw_image_names_are_ready(
                    [info.filename for info in zip_image_files]
                ) and all(
                    archive.open(info).read(4) != ANDROID_SPARSE_MAGIC
                    for info in zip_image_files
                )
        if tarfile.is_tarfile(path):
            with tarfile.open(path, mode="r:*") as archive:
                tar_image_files: list[tarfile.TarInfo] = []
                image_basenames = set()
                for member in archive.getmembers():
                    member_path = _safe_archive_member(member.name)
                    if member.isdir():
                        continue
                    if not member.isreg():
                        return False
                    if _archive_member_needs_preparation(member_path):
                        return False
                    if member_path.suffix.lower() == ".img":
                        basename = member_path.name.casefold()
                        if basename in image_basenames:
                            return False
                        image_basenames.add(basename)
                        tar_image_files.append(member)
                if not _raw_image_names_are_ready(
                    [member.name for member in tar_image_files]
                ):
                    return False
                for member in tar_image_files:
                    extracted = archive.extractfile(member)
                    if extracted is None or extracted.read(4) == ANDROID_SPARSE_MAGIC:
                        return False
                return True
        if path.name.lower().endswith(".7z"):
            with py7zr.SevenZipFile(path, mode="r") as archive:
                image_names: list[str] = []
                image_basenames = set()
                for info in archive.list():
                    member_path = _safe_archive_member(info.filename)
                    if info.is_directory:
                        continue
                    if info.is_symlink or not info.is_file:
                        return False
                    if _archive_member_needs_preparation(member_path):
                        return False
                    if member_path.suffix.lower() == ".img":
                        basename = member_path.name.casefold()
                        if basename in image_basenames:
                            return False
                        image_basenames.add(basename)
                        image_names.append(info.filename)
                if not _raw_image_names_are_ready(image_names):
                    return False
                magic_factory = _ImageMagicFactory()
                archive.extract(targets=image_names, factory=magic_factory)
                return all(
                    bytes(output.magic) != ANDROID_SPARSE_MAGIC
                    for output in magic_factory.outputs
                )
        return False

    @staticmethod
    def _zip_contains_payload(firmware_path: str) -> bool:
        if not zipfile.is_zipfile(firmware_path):
            return False
        with zipfile.ZipFile(firmware_path) as archive:
            return any(
                PurePosixPath(name.replace("\\", "/")).name == "payload.bin"
                for name in archive.namelist()
            )

    async def _drain_task(self, task: asyncio.Task[Any]) -> BaseException | None:
        """Wait until a shielded worker really exits, despite caller cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                if not task.done():
                    continue
                break
        try:
            task.result()
        except BaseException as error:
            return error
        return None

    async def classify_raw_image_archive(
        self,
        firmware_path: str,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> bool:
        """Classify potentially large archives off-loop and drain on cancellation."""
        worker = asyncio.create_task(
            asyncio.to_thread(self.is_raw_image_archive, firmware_path)
        )
        try:
            while not worker.done():
                if await _check_cancelled(cancellation_check):
                    await self._drain_task(worker)
                    raise JobCancelledError("Job was cancelled")
                await asyncio.wait({worker}, timeout=0.25)
            error = await self._drain_task(worker)
            if error:
                raise error
            return worker.result()
        except asyncio.CancelledError:
            await self._drain_task(worker)
            raise
        except BaseException:
            if not worker.done():
                await self._drain_task(worker)
            raise

    async def _run_otadump(
        self,
        payload_path: Path,
        output_dir: Path,
        *,
        source_dir: Path | None = None,
        cancellation_check: CancellationCheck | None = None,
        timeout: float = ONE_HOUR,
    ) -> None:
        """Run one native call with cooperative cancellation and mandatory drain."""
        token = otadump.CancellationToken()

        def run() -> None:
            try:
                otadump.extract(
                    payload_path,
                    output_dir,
                    source_dir=source_dir,
                    cancellation_token=token,
                )
            except KeyboardInterrupt as error:
                raise NativeExtractionCancelled(
                    "Native OTA extraction was cancelled"
                ) from error

        worker = asyncio.create_task(asyncio.to_thread(run))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not worker.done():
                if await _check_cancelled(cancellation_check):
                    token.cancel()
                    await self._drain_task(worker)
                    raise JobCancelledError("Job was cancelled")

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    token.cancel()
                    await self._drain_task(worker)
                    raise TimeoutError("Native OTA extraction timed out")
                await asyncio.wait({worker}, timeout=min(0.25, remaining))

            error = await self._drain_task(worker)
            if isinstance(error, (NativeExtractionCancelled, JobCancelledError)):
                raise JobCancelledError("Job was cancelled") from error
            if error:
                raise error
        except asyncio.CancelledError:
            token.cancel()
            await self._drain_task(worker)
            raise
        except (JobCancelledError, TimeoutError):
            if not worker.done():
                token.cancel()
                await self._drain_task(worker)
            raise
        except BaseException:
            if not worker.done():
                token.cancel()
                await self._drain_task(worker)
            raise

    async def _run_blocking_and_drain(
        self,
        function: Callable[..., None],
        *args: object,
        timeout: float = ONE_HOUR,
    ) -> None:
        """Do not let a non-native extraction thread outlive its work directory."""
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not worker.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._drain_task(worker)
                    raise TimeoutError("Extraction step timed out")
                await asyncio.wait({worker}, timeout=min(0.25, remaining))

            error = await self._drain_task(worker)
            if error:
                raise error
        except asyncio.CancelledError:
            await self._drain_task(worker)
            raise
        except TimeoutError:
            if not worker.done():
                await self._drain_task(worker)
            raise
        except BaseException:
            if not worker.done():
                await self._drain_task(worker)
            raise

    async def extract_reconstructed_firmware(
        self,
        job: DumpJob,
        firmware_paths: Sequence[str],
        *,
        cancellation_check: CancellationCheck | None = None,
        base_is_raw: bool | None = None,
    ) -> str:
        """Build ordered final images, then extract filesystems exactly once."""
        if not firmware_paths:
            raise ValueError("No firmware inputs were downloaded")

        if job.dump_args.use_alt_dumper:
            raise ValueError(
                "Alternative dumper is not supported for delta OTA or raw-image reconstruction"
            )

        staging_root = self.work_dir / ".ota_staging"
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_root.mkdir()
        try:
            base_path = Path(firmware_paths[0])
            source_dir = staging_root / "stage_000"
            if base_is_raw is None:
                base_is_raw = await self.classify_raw_image_archive(
                    str(base_path), cancellation_check=cancellation_check
                )
            if base_is_raw:
                await self._run_blocking_and_drain(
                    _unpack_raw_image_archive, base_path, source_dir
                )
            elif self._zip_contains_payload(str(base_path)):
                source_dir.mkdir()
                await self._run_otadump(
                    base_path,
                    source_dir,
                    cancellation_check=cancellation_check,
                )
            else:
                raise ValueError(
                    "Delta reconstruction requires a full OTA or raw-image archive base"
                )

            current_stage = source_dir
            for index, delta_path_value in enumerate(firmware_paths[1:], start=1):
                next_stage = staging_root / f"stage_{index:03d}"
                next_stage.mkdir()
                await self._run_otadump(
                    Path(delta_path_value),
                    next_stage,
                    source_dir=current_stage,
                    cancellation_check=cancellation_check,
                )

                # A payload may omit unchanged partitions. Copy their bytes into
                # the completed next stage; never hardlink or mutate source images.
                await self._run_blocking_and_drain(
                    _carry_forward_omitted_images, current_stage, next_stage
                )
                shutil.rmtree(current_stage)
                current_stage = next_stage

            if await _check_cancelled(cancellation_check):
                raise JobCancelledError("Job was cancelled")

            await self._run_blocking_and_drain(
                _run_dumpyara_images, current_stage, self.work_dir
            )
            return str(self.work_dir)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            for firmware_path in firmware_paths:
                safe_remove_file(firmware_path)

    async def extract_firmware(self, job: DumpJob, firmware_path: str) -> str:
        """Extract firmware and return extraction directory."""
        if not job.dump_args.use_privdump:
            console.print(f"[blue]Extracting firmware: {firmware_path}[/blue]")

        if job.dump_args.use_alt_dumper:
            extraction_dir = await self._extract_with_alternative_dumper(firmware_path)
        else:
            extraction_dir = await self._extract_with_python_dumper(firmware_path)

        # Delete the original firmware archive so it isn't committed/pushed with
        # the extracted contents. Mirrors `rm -f "$FILE"` from the legacy
        # extract_and_push.sh; without it every dump shipped its multi-GB source
        # archive at the repo root.
        if safe_remove_file(firmware_path):
            if not job.dump_args.use_privdump:
                console.print(f"[green]Removed original firmware archive: {firmware_path}[/green]")
        else:
            if not job.dump_args.use_privdump:
                console.print(f"[yellow]Failed to remove original firmware archive: {firmware_path}[/yellow]")

        return extraction_dir

    async def _extract_with_python_dumper(self, firmware_path: str) -> str:
        """Extract using the modern Python dumpyara tool."""
        console.print("[blue]Python dumper extraction...[/blue]")
        await self._run_blocking_and_drain(
            _run_dumpyara,
            Path(firmware_path),
            self.work_dir,
        )
        console.print("[green]Python dumper extraction completed successfully[/green]")

        return str(self.work_dir)

    async def _extract_with_alternative_dumper(self, firmware_path: str) -> str:
        """Extract using the alternative Firmware_extractor toolkit."""
        console.print("[blue]Using alternative dumper (Firmware_extractor)...[/blue]")

        # Clone/update Firmware_extractor
        await self._setup_firmware_extractor()

        # Run the extractor script
        extractor_script = self.firmware_extractor_path / "extractor.sh"
        result = await run_command(
            "bash", str(extractor_script), firmware_path, str(self.work_dir),
            cwd=self.work_dir,
            timeout=ONE_HOUR,
            check=True,
            description="Alternative dumper extraction"
        )

        # Extract individual partitions
        await self._extract_partitions()

        console.print("[green]Alternative dumper extraction completed[/green]")
        return str(self.work_dir)

    async def _setup_firmware_extractor(self):
        """Clone or update the Firmware_extractor repository."""
        # Network + HDD I/O can be slow; bound generously rather than the 30s default.
        if not self.firmware_extractor_path.exists():
            await run_git_command(
                "clone", "-q",
                "https://github.com/AndroidDumps/Firmware_extractor",
                str(self.firmware_extractor_path),
                timeout=HALF_HOUR,
                description="Cloning Firmware_extractor"
            )
        else:
            await run_git_command(
                "-C", str(self.firmware_extractor_path), "pull", "-q", "--rebase",
                timeout=HALF_HOUR,
                description="Updating Firmware_extractor"
            )

    async def _extract_partitions(self):
        """Extract individual partition images using alternative dumper tools."""
        partitions = [
            "system", "systemex", "system_ext", "system_other",
            "vendor", "cust", "odm", "odm_ext", "oem", "factory", "product", "modem",
            "xrom", "oppo_product", "opproduct", "reserve", "india", "my_preload",
            "my_odm", "my_stock", "my_operator", "my_country", "my_product", "my_company",
            "my_engineering", "my_heytap", "my_custom", "my_manifest", "my_carrier", "my_region",
            "my_bigball", "my_version", "special_preload", "vendor_dlkm", "odm_dlkm", "system_dlkm",
            "mi_ext", "radio", "product_h", "preas", "preavs", "preload", "mi_product"
        ]

        fsck_erofs = self.firmware_extractor_path / "tools" / "fsck.erofs"
        ext2rd = self.firmware_extractor_path / "tools" / "ext2rd"

        for partition in partitions:
            img_file = self.work_dir / f"{partition}.img"
            if not img_file.exists():
                continue

            partition_dir = self.work_dir / partition
            partition_dir.mkdir(exist_ok=True)

            # Try extraction methods in order
            success = False

            # Method 1: fsck.erofs
            if fsck_erofs.exists():
                result = await run_extraction_command(
                    str(fsck_erofs), f"--extract={partition_dir}", str(img_file),
                    description=f"Extracting '{partition}' via fsck.erofs"
                )
                if result.success:
                    success = True

            # Method 2: ext2rd
            if not success and ext2rd.exists():
                result = await run_extraction_command(
                    str(ext2rd), str(img_file), f"./{partition}",
                    cwd=self.work_dir,
                    description=f"Extracting '{partition}' via ext2rd"
                )
                if result.success:
                    success = True

            # Method 3: 7zip
            if not success:
                result = await run_extraction_command(
                    "7zz", "-snld", "x", str(img_file), "-y", f"-o{partition_dir}/",
                    description=f"Extracting '{partition}' via 7zz"
                )
                if result.success:
                    success = True

            if success:
                # Clean up the image file
                safe_remove_file(img_file)
                console.print(f"[green]Successfully extracted {partition}[/green]")
            else:
                console.print(f"[yellow]Failed to extract {partition}[/yellow]")
                # Only abort on first partition failure
                if partition == partitions[0]:
                    raise Exception(f"Critical partition extraction failed: {partition}")

        # Extract fsg.mbn from radio.img if present
        await self._extract_fsg_partition()

    async def _extract_fsg_partition(self):
        """Extract fsg.mbn partition if present."""
        fsg_file = self.work_dir / "fsg.mbn"
        if not fsg_file.exists():
            return

        console.print("[blue]Extracting fsg.mbn via 7zz...[/blue]")

        fsg_dir = self.work_dir / "radio" / "fsg"
        fsg_dir.mkdir(parents=True, exist_ok=True)

        result = await run_extraction_command(
            "7zz", "-snld", "x", str(fsg_file), f"-o{fsg_dir}",
            description="Extracting fsg.mbn via 7zz"
        )

        if result.success:
            safe_remove_file(fsg_file)
            console.print("[green]Successfully extracted fsg.mbn[/green]")

    async def process_boot_images(self) -> None:
        """Process boot images (boot.img, vendor_boot.img, etc.)."""
        boot_images = [
            "init_boot.img",
            "vendor_kernel_boot.img",
            "vendor_boot.img",
            "boot.img",
            "recovery.img",
            "dtbo.img",
        ]

        # Move boot images to work directory root if they're in subdirectories
        for image_name in boot_images:
            found_images = find_files_by_pattern(self.work_dir, [image_name], recursive=True)
            if found_images and not (self.work_dir / image_name).exists():
                move_file_to_root(found_images[0], self.work_dir)

        # Process each boot image
        for image_name in boot_images:
            image_path = self.work_dir / image_name
            if image_path.exists():
                await self._process_single_boot_image(image_path)

        # Process Oppo/Realme/OnePlus images in special directories
        await self._process_oppo_images()

    async def _process_single_boot_image(self, image_path: Path):
        """Process a single boot image file."""
        image_name = image_path.name
        output_dir = self.work_dir / image_path.stem

        console.print(f"[blue]Processing {image_name}...[/blue]")

        if image_name == "boot.img":
            await self._process_boot_img(image_path, output_dir)
        elif image_name == "recovery.img":
            await self._process_recovery_img(image_path, output_dir)
        elif image_name in ["vendor_boot.img", "vendor_kernel_boot.img", "init_boot.img"]:
            await self._process_vendor_boot_img(image_path, output_dir)
        elif image_name == "dtbo.img":
            await self._process_dtbo_img(image_path, output_dir)

    async def _process_boot_img(self, image_path: Path, output_dir: Path):
        """Process boot.img with comprehensive analysis."""
        output_dir.mkdir(exist_ok=True)

        # Extract kernel, ramdisk, etc. if using alternative dumper
        if self.firmware_extractor_path.exists():
            await self._unpack_boot_image(image_path, output_dir)

        # Extract ikconfig (kernel configuration)
        await self._extract_ikconfig(image_path)

        # Generate kallsyms.txt (kernel symbols)
        await self._extract_kallsyms(image_path)

        # Generate analyzable ELF
        await self._extract_boot_elf(image_path)

        # Extract and process device tree blobs
        await self._extract_device_trees(image_path, output_dir)

    async def _process_vendor_boot_img(self, image_path: Path, output_dir: Path):
        """Process vendor_boot.img or similar images."""
        output_dir.mkdir(exist_ok=True)

        # Extract contents if using alternative dumper
        if self.firmware_extractor_path.exists():
            await self._unpack_boot_image(image_path, output_dir)

        # Extract device tree blobs
        await self._extract_device_trees(image_path, output_dir)

    async def _process_recovery_img(self, image_path: Path, output_dir: Path):
        """Process recovery.img by unpacking the image and extracting its ramdisk."""
        output_dir.mkdir(exist_ok=True)

        if self.firmware_extractor_path.exists():
            await self._unpack_boot_image(image_path, output_dir)

        await self._extract_device_trees(image_path, output_dir)

    async def _process_dtbo_img(self, image_path: Path, output_dir: Path):
        """Process dtbo.img."""
        output_dir.mkdir(exist_ok=True)

        # Extract device tree overlays
        await self._extract_device_trees(image_path, output_dir, is_dtbo=True)

    async def _unpack_boot_image(self, image_path: Path, output_dir: Path):
        """Unpack boot image using unpackbootimg."""
        unpackbootimg = self.firmware_extractor_path / "tools" / "unpackbootimg"
        if not unpackbootimg.exists():
            return

        ramdisk_dir = output_dir / "ramdisk"
        ramdisk_dir.mkdir(exist_ok=True)

        await run_extraction_command(
            str(unpackbootimg), "-i", str(image_path), "-o", str(output_dir),
            description=f"Unpacking {image_path.name}"
        )

        # Extract ramdisk if present
        await self._extract_ramdisk(output_dir, ramdisk_dir)

    async def _extract_ramdisk(self, output_dir: Path, ramdisk_dir: Path):
        """Extract ramdisk from boot image."""
        ramdisk_files = list(output_dir.glob("*-ramdisk*"))
        if not ramdisk_files:
            return

        ramdisk_file = ramdisk_files[0]

        # Check if it's compressed
        result = await run_analysis_command(
            "file", str(ramdisk_file),
            description="Checking ramdisk compression"
        )

        if not result.success:
            return

        file_info = result.stdout

        if "LZ4" in file_info or "gzip" in file_info:
            console.print("[blue]Extracting compressed ramdisk...[/blue]")

            # Decompress with unlz4
            temp_ramdisk = output_dir / "ramdisk.lz4"
            decompress_result = await run_extraction_command(
                "unlz4", str(ramdisk_file), str(temp_ramdisk),
                description="Decompressing ramdisk"
            )

            if decompress_result.success and temp_ramdisk.exists():
                # Extract with 7zip
                await run_extraction_command(
                    "7zz", "-snld", "x", str(temp_ramdisk), f"-o{ramdisk_dir}",
                    description="Extracting ramdisk archive"
                )
                safe_remove_file(temp_ramdisk)

    async def _extract_ikconfig(self, image_path: Path):
        """Extract kernel configuration."""
        ikconfig_path = self.work_dir / "ikconfig"

        try:
            result = await run_analysis_command(
                "extract-ikconfig", str(image_path),
                output_file=ikconfig_path,
                description="Extracting ikconfig"
            )

            if result.success and ikconfig_path.exists():
                console.print("[green]ikconfig extracted successfully[/green]")
            else:
                console.print("[yellow]Failed to extract ikconfig[/yellow]")
                safe_remove_file(ikconfig_path)
        except FileNotFoundError:
            console.print("[yellow]extract-ikconfig tool not found, skipping ikconfig extraction[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Error extracting ikconfig: {e}[/yellow]")
            safe_remove_file(ikconfig_path)

    async def _extract_kallsyms(self, image_path: Path):
        """Extract kernel symbols."""
        kallsyms_path = self.work_dir / "kallsyms.txt"

        try:
            result = await run_analysis_command(
                "uvx", "--from", "git+https://github.com/marin-m/vmlinux-to-elf@master",
                "kallsyms-finder", str(image_path),
                output_file=kallsyms_path,
                description="Generating kallsyms.txt"
            )

            if result.success and kallsyms_path.exists():
                console.print("[green]kallsyms.txt generated successfully[/green]")
            else:
                console.print("[yellow]Failed to generate kallsyms.txt[/yellow]")
                safe_remove_file(kallsyms_path)
        except FileNotFoundError:
            console.print("[yellow]uvx or kallsyms-finder tool not found, skipping kallsyms extraction[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Error extracting kallsyms: {e}[/yellow]")
            safe_remove_file(kallsyms_path)

    async def _extract_boot_elf(self, image_path: Path):
        """Extract analyzable ELF file."""
        elf_path = self.work_dir / "boot.elf"

        try:
            result = await run_analysis_command(
                "uvx", "--from", "git+https://github.com/marin-m/vmlinux-to-elf@master",
                "vmlinux-to-elf", str(image_path), str(elf_path),
                description="Extracting boot.elf"
            )

            if result.success and elf_path.exists():
                console.print("[green]boot.elf extracted successfully[/green]")
            else:
                console.print("[yellow]Failed to extract boot.elf[/yellow]")
        except FileNotFoundError:
            console.print("[yellow]uvx or vmlinux-to-elf tool not found, skipping ELF extraction[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Error extracting boot ELF: {e}[/yellow]")

    async def _extract_device_trees(self, image_path: Path, output_dir: Path, is_dtbo: bool = False):
        """Extract and decompile device tree blobs."""
        if is_dtbo:
            dtb_dir = output_dir
            dts_dir = output_dir / "dts"
        else:
            dtb_dir = output_dir / "dtb"
            dts_dir = output_dir / "dts"

        dtb_dir.mkdir(exist_ok=True)
        dts_dir.mkdir(exist_ok=True)

        console.print(f"[blue]{image_path.name}: Extracting device-tree blobs...[/blue]")

        # Extract DTBs
        try:
            result = await run_extraction_command(
                "extract-dtb", str(image_path), "-o", str(dtb_dir),
                description=f"{image_path.name}: Extracting device-tree blobs"
            )

            if not result.success:
                console.print("[yellow]No device-tree blobs found[/yellow]")
                return
        except FileNotFoundError:
            console.print("[yellow]extract-dtb tool not found, skipping device tree extraction[/yellow]")
            return
        except Exception as e:
            console.print(f"[yellow]Error extracting device trees: {e}[/yellow]")
            return

        # extract-dtb writes the kernel binary as 00_kernel alongside the dtbs
        safe_remove_file(dtb_dir / "00_kernel")

        # Decompile DTBs to DTS
        dtb_files = list(dtb_dir.glob("*.dtb"))
        if dtb_files:
            console.print("[blue]Decompiling device-tree blobs...[/blue]")

            for dtb_file in dtb_files:
                dts_file = dts_dir / f"{dtb_file.stem}.dts"

                try:
                    result = await run_analysis_command(
                        "dtc", "-q", "-I", "dtb", "-O", "dts", str(dtb_file),
                        output_file=dts_file,
                        description=f"Decompiling {dtb_file.name}"
                    )

                    if result.success:
                        console.print(f"[green]Decompiled {dtb_file.name}[/green]")
                    else:
                        console.print(f"[yellow]Failed to decompile {dtb_file.name}[/yellow]")
                        safe_remove_file(dts_file)
                except FileNotFoundError:
                    console.print(f"[yellow]dtc tool not found, skipping decompilation of {dtb_file.name}[/yellow]")
                except Exception as e:
                    console.print(f"[yellow]Error decompiling {dtb_file.name}: {e}[/yellow]")
                    safe_remove_file(dts_file)

    async def _process_oppo_images(self):
        """Process Oppo/Realme/OnePlus images in special directories."""
        special_dirs = ["vendor/euclid", "system/system/euclid", "reserve/reserve"]

        for dir_path in special_dirs:
            full_dir = self.work_dir / dir_path
            if not full_dir.exists():
                continue

            console.print(f"[blue]Processing images in {dir_path}...[/blue]")

            for img_file in full_dir.glob("*.img"):
                if not img_file.is_file():
                    continue

                console.print(f"[blue]Extracting {img_file.name}...[/blue]")

                extract_dir = img_file.parent / img_file.stem
                extract_dir.mkdir(exist_ok=True)

                result = await run_extraction_command(
                    "7zz", "-snld", "x", str(img_file), f"-o{extract_dir}",
                    description=f"Extracting {img_file.name}"
                )

                if result.success:
                    safe_remove_file(img_file)
                    console.print(f"[green]Extracted {img_file.name}[/green]")
                else:
                    console.print(f"[yellow]Failed to extract {img_file.name}[/yellow]")
