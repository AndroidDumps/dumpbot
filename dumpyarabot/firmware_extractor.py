import asyncio
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, TypeVar

from dumpyara.dumpyara import dumpyara
from dumpyara.steps.extract_images import extract_images as dumpyara_extract_images
from dumpyara.utils import multipartitions as dumpyara_multipartitions
import otadump
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
_T = TypeVar("_T")

def _run_dumpyara(firmware_path: Path, output_path: Path) -> None:
    """Run Dumpyara with its reliable in-process payload parser."""
    dumpyara_multipartitions.OTADUMP_EXECUTABLE = None
    dumpyara(firmware_path, output_path)


def _run_dumpyara_images(images_path: Path, output_path: Path) -> None:
    dumpyara_extract_images(images_path, output_path)


def _carry_forward_images(source_dir: Path, output_dir: Path) -> None:
    for source_image in source_dir.glob("*.img"):
        destination = output_dir / source_image.name
        if not destination.exists():
            shutil.copy2(source_image, destination)


def _delta_stage_has_images(stage_dir: Path) -> bool:
    return any(stage_dir.glob("*.img"))


def _publish_stage_children(staging_dir: Path, destination_dir: Path) -> None:
    children = list(staging_dir.iterdir())
    if not children:
        raise RuntimeError("delta chain final stage produced no files")
    preexisting = [destination_dir / child.name for child in children if (destination_dir / child.name).exists()]
    if preexisting:
        raise RuntimeError(f"final output already exists: {preexisting[0]}")

    moved: list[Path] = []
    try:
        for child in children:
            target = destination_dir / child.name
            shutil.move(str(child), str(target))
            moved.append(target)
    except BaseException:
        for moved_child in moved:
            if moved_child.exists():
                if moved_child.is_dir():
                    shutil.rmtree(moved_child)
                else:
                    moved_child.unlink()
        raise


async def _run_thread(
    function: Callable[..., _T],
    *args: object,
    timeout: float,
    token: otadump.CancellationToken,
    **kwargs: object,
) -> _T:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        token.cancel()
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise


class FirmwareExtractor:
    """Handles firmware extraction using both Python dumper and alternative methods."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.firmware_extractor_path = Path.home() / "Firmware_extractor"

    async def extract_delta_chain(self, job: DumpJob, firmware_paths: Sequence[str]) -> str:
        if len(firmware_paths) < 2:
            raise ValueError("A delta chain requires a base and at least one delta OTA")
        if job.dump_args.use_alt_dumper:
            raise ValueError("Alternative dumper is not supported for delta OTA chains")

        staging_root = self.work_dir / ".ota_staging"
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        cancellation_token = otadump.CancellationToken()

        try:
            current_stage = staging_root / "stage_000"
            await _run_thread(
                otadump.extract,
                Path(firmware_paths[0]),
                current_stage,
                timeout=ONE_HOUR,
                token=cancellation_token,
                cancellation_token=cancellation_token,
            )
            if not _delta_stage_has_images(current_stage):
                raise RuntimeError(
                    f"delta stage produced no images: {Path(firmware_paths[0]).name}"
                )
            for index, firmware_path in enumerate(firmware_paths[1:], start=1):
                next_stage = staging_root / f"stage_{index:03d}"
                await _run_thread(
                    otadump.extract,
                    Path(firmware_path),
                    next_stage,
                    timeout=ONE_HOUR,
                    token=cancellation_token,
                    source_dir=current_stage,
                    cancellation_token=cancellation_token,
                )
                if not _delta_stage_has_images(next_stage):
                    raise RuntimeError(
                        f"delta stage produced no images: {Path(firmware_path).name}"
                    )
                await _run_thread(
                    _carry_forward_images,
                    current_stage,
                    next_stage,
                    timeout=ONE_HOUR,
                    token=cancellation_token,
                )
                shutil.rmtree(current_stage)
                current_stage = next_stage

            final_output = staging_root / "final"
            final_output.mkdir(parents=True, exist_ok=True)
            await _run_thread(
                _run_dumpyara_images,
                current_stage,
                final_output,
                timeout=ONE_HOUR,
                token=cancellation_token,
            )
            system_path = final_output / "system"
            if not system_path.exists():
                raise RuntimeError("dumpyara output missing required non-empty system")
            if system_path.is_file() and system_path.stat().st_size == 0:
                raise RuntimeError("dumpyara output missing required non-empty system")
            if system_path.is_dir() and not any(system_path.iterdir()):
                raise RuntimeError("dumpyara output missing required non-empty system")
            _publish_stage_children(final_output, self.work_dir)
            return str(self.work_dir)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            for firmware_path in firmware_paths:
                safe_remove_file(firmware_path)

    async def extract_firmware(self, job: DumpJob, firmware_path: str) -> str:
        """Extract firmware and return extraction directory."""
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
            console.print(f"[green]Removed original firmware archive: {firmware_path}[/green]")
        else:
            console.print(f"[yellow]Failed to remove original firmware archive: {firmware_path}[/yellow]")

        return extraction_dir

    async def _extract_with_python_dumper(self, firmware_path: str) -> str:
        """Extract using the modern Python dumpyara tool."""
        console.print("[blue]Python dumper extraction...[/blue]")
        await asyncio.wait_for(
            asyncio.to_thread(
                _run_dumpyara,
                Path(firmware_path),
                self.work_dir,
            ),
            timeout=ONE_HOUR,
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
