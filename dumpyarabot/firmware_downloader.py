import asyncio
import os
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

import httpx
from rich.console import Console

from dumpyarabot.aria2_manager import Aria2Manager, DownloadProgress
from dumpyarabot.file_utils import (
    get_file_size_formatted,
    get_latest_file_in_directory,
    safe_remove_file,
)
from dumpyarabot.privacy import redact_urls, sanitize_url
from dumpyarabot.process_utils import run_download_command
from dumpyarabot.schemas import DumpJob

console = Console()

# Type alias for the optional progress callback
ProgressCallback = Callable[[DownloadProgress], Coroutine[None, None, None]]


class FirmwareDownloader:
    """Handles firmware downloading with mirror optimization and special URL handling."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    async def download_firmware(
        self,
        job: DumpJob,
        on_progress: ProgressCallback | None = None,
    ) -> Tuple[str, str]:
        """Download firmware and return (file_path, file_name).

        Args:
            job: The dump job containing URL and options.
            on_progress: Optional async callback invoked with each DownloadProgress
                         snapshot during aria2 RPC downloads.
        """
        return await self.download_url(
            job,
            str(job.dump_args.url),
            on_progress=on_progress,
        )

    async def download_url(
        self,
        job: DumpJob,
        url: str,
        on_progress: ProgressCallback | None = None,
    ) -> Tuple[str, str]:
        """Download one ordered input without exposing private source details."""
        private = job.dump_args.use_privdump

        try:
            # Check if it's a local file
            if os.path.isfile(url):
                if not private:
                    console.print(f"[green]Found local file: {url}[/green]")
                file_name = Path(url).name
                dest_path = self.work_dir / file_name
                shutil.copy2(url, dest_path)
                return str(dest_path), file_name

            # Optimize URL with mirrors
            optimized_url = await self._optimize_url(url, private=private)
            if not private:
                console.print(f"[blue]Downloading from: {sanitize_url(optimized_url)}[/blue]")

            file_path = await self._download_by_type(
                optimized_url,
                on_progress=on_progress,
                private=private,
            )
            file_name = Path(file_path).name

            if not private:
                console.print(f"[green]Downloaded: {file_name} ({get_file_size_formatted(file_path)})[/green]")
            return file_path, file_name
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if private:
                raise RuntimeError("Private firmware download failed") from None
            raise e

    async def _optimize_url(self, url: str, *, private: bool = False) -> str:
        """Optimize URL with best available mirrors."""
        # Xiaomi mirror optimization
        if "d.miui.com" in url:
            return await self._optimize_xiaomi_url(url, private=private)

        # Pixeldrain optimization
        if "pixeldrain.com/u" in url:
            file_id = url.split("/")[-1]
            return f"https://pd.cybar.xyz/{file_id}"

        if "pixeldrain.com/d" in url:
            file_id = url.split("/")[-1]
            return f"https://pixeldrain.com/api/filesystem/{file_id}"

        return url

    async def _optimize_xiaomi_url(self, url: str, *, private: bool = False) -> str:
        """Find best Xiaomi mirror."""
        # Skip if already using recommended mirror
        if "cdnorg" in url or "bkt-sgp-miui-ota-update-alisgp" in url:
            return url

        # Extract original host and file path (equivalent to bash logic)
        parsed = urlparse(url)
        original_host = f"{parsed.scheme}://{parsed.netloc}"

        # Extract file path after the domain (equivalent to ${URL#*d.miui.com/})
        if "d.miui.com/" in url:
            file_path = url.split("d.miui.com/", 1)[1]
        else:
            # Fallback for other formats
            file_path = parsed.path.lstrip('/')

        # Remove query strings from file path
        file_path = file_path.split('?')[0]

        # Test mirrors in order of preference
        mirrors = [
            "https://cdnorg.d.miui.com",
            "https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com",
            "https://bn.d.miui.com",
            original_host
        ]

        async with httpx.AsyncClient() as client:
            for mirror in mirrors:
                test_url = f"{mirror}/{file_path}"
                try:
                    if not private:
                        console.print(f"[blue]Testing mirror: {mirror}[/blue]")
                    response = await client.head(test_url, timeout=10.0)
                    if response.status_code != 404:
                        if not private:
                            console.print(f"[green]Using mirror: {mirror}[/green]")
                        return test_url
                except Exception as e:
                    if not private:
                        console.print(
                            f"[yellow]Mirror {mirror} failed: "
                            f"{redact_urls(e, private=False)}[/yellow]"
                        )
                    continue

        console.print("[yellow]All mirrors failed, using original URL[/yellow]")
        return url

    async def _download_by_type(
        self,
        url: str,
        on_progress: ProgressCallback | None = None,
        *,
        private: bool = False,
    ) -> str:
        """Download file based on URL type."""
        if "drive.google.com" in url:
            return await self._download_google_drive(url, private=private)
        elif "mediafire.com" in url:
            return await self._download_mediafire(url, private=private)
        elif "mega.nz" in url:
            return await self._download_mega(url, private=private)
        else:
            return await self._download_default(url, on_progress=on_progress, private=private)

    async def _download_google_drive(self, url: str, *, private: bool = False) -> str:
        """Download from Google Drive using gdown."""
        result = await run_download_command(
            "uvx", "gdown@5.2.0", "-q", url, "--fuzzy",
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from Google Drive",
            quiet=True,
        )

        if not result.success:
            raise Exception(f"Google Drive download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after Google Drive download")

        return str(latest_file)

    async def _download_mediafire(self, url: str, *, private: bool = False) -> str:
        """Download from MediaFire using mediafire-dl."""
        result = await run_download_command(
            "uvx", "--from", "git+https://github.com/Juvenal-Yescas/mediafire-dl@master",
            "mediafire-dl", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from MediaFire",
            quiet=True,
        )

        if not result.success:
            raise Exception(f"MediaFire download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after MediaFire download")

        return str(latest_file)

    async def _download_mega(self, url: str, *, private: bool = False) -> str:
        """Download from MEGA using megatools."""
        result = await run_download_command(
            "megatools", "dl", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from MEGA",
            quiet=True,
        )

        if not result.success:
            raise Exception(f"MEGA download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after MEGA download")

        return str(latest_file)

    async def _download_default(
        self,
        url: str,
        on_progress: ProgressCallback | None = None,
        *,
        private: bool = False,
    ) -> str:
        """Download using aria2 RPC (with live progress) and wget fallback."""
        # --- Try aria2 RPC first ---
        aria2_failed = False
        aria2_error = ""
        try:
            async with Aria2Manager(
                str(self.work_dir), log_download_names=False
            ) as aria2:
                async for progress in aria2.download(url, poll_interval=3.0, timeout=1800.0):
                    if on_progress:
                        try:
                            await on_progress(progress)
                        except Exception as cb_err:
                            # Don't let a Telegram/callback error kill the download
                            if not private:
                                console.print(f"[yellow]Progress callback error (ignored): {cb_err}[/yellow]")

                # Download finished successfully
                downloaded = aria2.get_downloaded_file_path()
                if downloaded:
                    return downloaded

                # aria2 reported completion but no file found
                aria2_error = "aria2 completed but no file found in work dir"
                console.print(f"[yellow]{aria2_error}[/yellow]")
                aria2_failed = True
        except Exception as e:
            # Keep the actual aria2 reason (error code + message); the console line
            # only reaches journald, so we also stash it for the final exception.
            aria2_error = str(e) or repr(e)
            if not private:
                console.print(
                    f"[yellow]aria2 RPC download failed: "
                    f"{redact_urls(aria2_error, private=False)}[/yellow]"
                )
            aria2_failed = True

        if not aria2_failed:
            raise Exception("aria2 download did not produce a file")

        # Clean up aria2c partial/sidecar artifacts before wget fallback
        for file in self.work_dir.iterdir():
            if file.suffix == ".aria2" or (file.is_file() and file.stat().st_size == 0):
                safe_remove_file(file)

        console.print("[yellow]Falling back to wget...[/yellow]")

        # --- wget fallback ---
        # Use -nv (not -q): -q silences errors too, so a failed download would
        # leave stderr empty. -nv drops the progress bar but keeps error output.
        result = await run_download_command(
            "wget", "-nv", "--no-check-certificate", url,
            cwd=self.work_dir,
            timeout=1800.0,
            description="Downloading with wget fallback",
            quiet=True,
        )

        if not result.success:
            wget_error = result.stderr.strip() or "(no stderr output)"
            raise Exception(
                "Both aria2 RPC and wget failed. "
                f"aria2: {aria2_error or '(no error captured)'}; "
                f"wget: {wget_error}"
            )

        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception(
                "wget reported success but no file was found in the work dir "
                f"(aria2 had failed first: {aria2_error or '(no error captured)'})"
            )

        return str(latest_file)
