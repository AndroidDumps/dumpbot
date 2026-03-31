import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from rich.console import Console

from dumpyarabot.schemas import DumpJob
from dumpyarabot.process_utils import run_download_command
from dumpyarabot.file_utils import get_latest_file_in_directory, safe_remove_file, get_file_size_formatted

console = Console()


class FirmwareDownloader:
    """Handles firmware downloading with mirror optimization and special URL handling."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    async def download_firmware(self, job: DumpJob) -> Tuple[str, str]:
        """Download firmware and return (file_path, file_name)."""
        url = str(job.dump_args.url)

        # Clean query strings from URL
        url = url.split('?')[0]

        # Check if it's a local file
        if os.path.isfile(url):
            console.print(f"[green]Found local file: {url}[/green]")
            # Copy to work directory
            file_name = Path(url).name
            dest_path = self.work_dir / file_name
            shutil.copy2(url, dest_path)
            return str(dest_path), file_name

        # Optimize URL with mirrors
        optimized_url = await self._optimize_url(url)
        console.print(f"[blue]Downloading from: {optimized_url}[/blue]")

        # Download based on URL type
        file_path = await self._download_by_type(optimized_url)
        file_name = Path(file_path).name

        console.print(f"[green]Downloaded: {file_name} ({get_file_size_formatted(file_path)})[/green]")
        return file_path, file_name

    async def _optimize_url(self, url: str) -> str:
        """Optimize URL with best available mirrors."""
        # Clean query strings first
        url = url.split('?')[0]

        # Xiaomi mirror optimization
        if "d.miui.com" in url:
            return await self._optimize_xiaomi_url(url)

        # Pixeldrain optimization
        if "pixeldrain.com/u" in url:
            file_id = url.split("/")[-1]
            return f"https://pd.cybar.xyz/{file_id}"

        if "pixeldrain.com/d" in url:
            file_id = url.split("/")[-1]
            return f"https://pixeldrain.com/api/filesystem/{file_id}"

        return url

    async def _optimize_xiaomi_url(self, url: str) -> str:
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

        async with httpx.AsyncClient(verify=False) as client:
            for mirror in mirrors:
                test_url = f"{mirror}/{file_path}"
                try:
                    console.print(f"[blue]Testing mirror: {mirror}[/blue]")
                    response = await client.head(test_url, timeout=10.0)
                    if response.status_code != 404:
                        console.print(f"[green]Using mirror: {mirror}[/green]")
                        return test_url
                except Exception as e:
                    console.print(f"[yellow]Mirror {mirror} failed: {e}[/yellow]")
                    continue

        console.print("[yellow]All mirrors failed, using original URL[/yellow]")
        return url

    async def _download_by_type(self, url: str) -> str:
        """Download file based on URL type."""
        if "drive.google.com" in url:
            return await self._download_google_drive(url)
        elif "mediafire.com" in url:
            return await self._download_mediafire(url)
        elif "mega.nz" in url:
            return await self._download_mega(url)
        else:
            return await self._download_default(url)

    async def _download_google_drive(self, url: str) -> str:
        """Download from Google Drive using gdown."""
        result = await run_download_command(
            "uvx", "gdown@5.2.0", "-q", url, "--fuzzy",
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from Google Drive"
        )

        if not result.success:
            raise Exception(f"Google Drive download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after Google Drive download")

        return str(latest_file)

    async def _download_mediafire(self, url: str) -> str:
        """Download from MediaFire using mediafire-dl."""
        result = await run_download_command(
            "uvx", "--from", "git+https://github.com/Juvenal-Yescas/mediafire-dl@master",
            "mediafire-dl", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from MediaFire"
        )

        if not result.success:
            raise Exception(f"MediaFire download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after MediaFire download")

        return str(latest_file)

    async def _download_mega(self, url: str) -> str:
        """Download from MEGA using megatools."""
        result = await run_download_command(
            "megatools", "dl", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading from MEGA"
        )

        if not result.success:
            raise Exception(f"MEGA download failed: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after MEGA download")

        return str(latest_file)

    async def _download_default(self, url: str) -> str:
        """Download using aria2c with wget fallback."""
        # Try aria2c first
        result = await run_download_command(
            "aria2c", "-q", "-s16", "-x16", "--check-certificate=false", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading with aria2c"
        )

        if result.success:
            # Success with aria2c
            latest_file = get_latest_file_in_directory(self.work_dir)
            if latest_file:
                return str(latest_file)

        console.print("[yellow]aria2c failed, trying wget...[/yellow]")

        # Clean up any partial downloads
        for file in self.work_dir.glob("*"):
            if file.is_file():
                safe_remove_file(file)

        # Try wget fallback
        result = await run_download_command(
            "wget", "-q", "--no-check-certificate", url,
            cwd=self.work_dir,
            timeout=1800.0,  # 30 minutes for large files
            description="Downloading with wget fallback"
        )

        if not result.success:
            raise Exception(f"Both aria2c and wget failed. Last error: {result.stderr}")

        # Find downloaded file
        latest_file = get_latest_file_in_directory(self.work_dir)
        if not latest_file:
            raise Exception("No file found after wget download")

        return str(latest_file)


