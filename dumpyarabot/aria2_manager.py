"""aria2 RPC manager for download operations with real-time progress tracking."""

import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path
from collections.abc import AsyncIterator

import aria2p
from rich.console import Console

console = Console()


@dataclass
class DownloadProgress:
    """Snapshot of an active download's progress."""

    total_bytes: int
    completed_bytes: int
    download_speed: int  # bytes/sec
    connections: int
    status: str  # "active", "waiting", "paused", "error", "complete", "removed"
    file_name: str | None = None
    error_message: str | None = None

    @property
    def percentage(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, (self.completed_bytes / self.total_bytes) * 100)

    @property
    def eta_seconds(self) -> int | None:
        if self.download_speed <= 0 or self.total_bytes <= 0:
            return None
        remaining = self.total_bytes - self.completed_bytes
        if remaining <= 0:
            return 0
        return int(remaining / self.download_speed)

    @property
    def speed_mbps(self) -> float:
        return self.download_speed / (1024 * 1024)

    @property
    def completed_mb(self) -> float:
        return self.completed_bytes / (1024 * 1024)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    def format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    def format_eta(self) -> str:
        eta = self.eta_seconds
        if eta is None:
            return "calculating..."
        if eta <= 0:
            return "0s"
        if eta < 60:
            return f"{eta}s"
        if eta < 3600:
            return f"{eta // 60}m {eta % 60}s"
        return f"{eta // 3600}h {(eta % 3600) // 60}m"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_error(self) -> bool:
        return self.status == "error"


def _find_free_port() -> int:
    """Find a free TCP port for aria2 RPC."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Aria2Manager:
    """Manages an aria2c daemon and provides RPC-based download with progress tracking."""

    def __init__(self, download_dir: str, split: int = 16, max_connection_per_server: int = 16):
        self.download_dir = Path(download_dir)
        self.split = split
        self.max_connection_per_server = max_connection_per_server
        self._process: asyncio.subprocess.Process | None = None
        self._api: aria2p.API | None = None
        self._port: int | None = None
        self._secret: str = ""

    async def start(self) -> None:
        """Start the aria2c daemon with RPC enabled."""
        self._port = _find_free_port()
        self.download_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "aria2c",
            "--enable-rpc",
            f"--rpc-listen-port={self._port}",
            "--rpc-listen-all=false",
            f"--dir={self.download_dir}",
            f"--split={self.split}",
            f"--max-connection-per-server={self.max_connection_per_server}",
            "--check-certificate=false",
            "--file-allocation=none",
            "--auto-file-renaming=false",
            "--quiet=true",
        ]

        console.print(f"[blue]Starting aria2c RPC daemon on port {self._port}[/blue]")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Give aria2c a moment to bind
        await asyncio.sleep(0.5)

        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            raise RuntimeError(f"aria2c daemon failed to start: {stderr.decode()}")

        self._api = aria2p.API(
            aria2p.Client(
                host="http://127.0.0.1",
                port=self._port,
                secret=self._secret,
            )
        )

        console.print(f"[green]aria2c RPC daemon started (pid={self._process.pid}, port={self._port})[/green]")

    async def stop(self) -> None:
        """Shut down the aria2c daemon."""
        if self._api:
            try:
                self._api.client.shutdown()
            except Exception:
                pass
            self._api = None

        if self._process:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            else:
                # Process already exited — reap to avoid zombie
                try:
                    await self._process.wait()
                except Exception:
                    pass
            console.print("[yellow]aria2c daemon stopped[/yellow]")

        self._process = None
        self._port = None

    async def download(
        self,
        url: str,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> AsyncIterator[DownloadProgress]:
        """
        Add a download and yield progress snapshots until completion.

        Args:
            url: URL to download
            poll_interval: Seconds between progress polls
            timeout: Maximum download time in seconds

        Yields:
            DownloadProgress snapshots

        Raises:
            RuntimeError: If download fails or times out
        """
        if not self._api:
            raise RuntimeError("aria2c daemon not started - call start() first")

        loop = asyncio.get_running_loop()

        # Add the download (add_uris returns a single Download object)
        download = await loop.run_in_executor(None, self._api.add_uris, [url])
        if not download:
            raise RuntimeError(f"Failed to add download for: {url}")

        gid = download.gid
        console.print(f"[blue]Download added (gid={gid}): {url}[/blue]")

        elapsed = 0.0
        try:
            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                # Refresh download status (run in executor to avoid blocking event loop)
                download = await loop.run_in_executor(None, self._api.get_download, gid)

                file_name = None
                if download.files and download.files[0].path:
                    file_name = Path(download.files[0].path).name

                progress = DownloadProgress(
                    total_bytes=download.total_length,
                    completed_bytes=download.completed_length,
                    download_speed=download.download_speed,
                    connections=download.connections,
                    status=download.status,
                    file_name=file_name,
                    error_message=download.error_message if download.status == "error" else None,
                )

                yield progress

                if progress.is_complete:
                    console.print(f"[green]Download complete: {file_name}[/green]")
                    return

                if progress.is_error:
                    raise RuntimeError(
                        f"aria2 download error (code {download.error_code}): {download.error_message}"
                    )

            # Timed out
            await loop.run_in_executor(
                None, lambda: self._api.remove([download], force=True, files=True)
            )
            raise RuntimeError(f"Download timed out after {timeout}s")

        except (asyncio.CancelledError, Exception):
            # Clean up on cancellation or error
            try:
                download = await loop.run_in_executor(None, self._api.get_download, gid)
                if download.status in ("active", "waiting", "paused"):
                    await loop.run_in_executor(
                        None, lambda: self._api.remove([download], force=True, files=True)
                    )
            except Exception:
                pass
            raise

    def get_downloaded_file_path(self) -> str | None:
        """Get the path of the most recently downloaded file."""
        if not self.download_dir.exists():
            return None

        files = sorted(
            (f for f in self.download_dir.iterdir() if f.is_file() and not f.suffix == ".aria2"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return str(files[0]) if files else None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
