"""Process utilities for running external commands with standardized error handling."""

import asyncio
import os
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict, Any

from rich.console import Console

console = Console()


class ProcessResult:
    """Result of a process execution."""

    def __init__(
        self,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        command: List[str] = None,
        timeout_occurred: bool = False,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command or []
        self.timeout_occurred = timeout_occurred

    @property
    def success(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0

    @property
    def command_str(self) -> str:
        """Command as a string for logging."""
        return " ".join(self.command)


async def run_command(
    *args: str,
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    check: bool = False,
    env: Optional[Dict[str, str]] = None,
    description: Optional[str] = None,
    quiet: bool = False,
) -> ProcessResult:
    """
    Run an external command with standardized error handling.

    Args:
        *args: Command and arguments
        cwd: Working directory
        timeout: Command timeout in seconds
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise exception on non-zero exit
        env: Environment variables to add/override
        description: Human-readable description for logging
        quiet: Whether to suppress progress logging

    Returns:
        ProcessResult with command output and status

    Raises:
        ProcessException: If check=True and command fails
    """
    command = list(args)
    log_desc = description or f"Running {command[0]}"

    if not quiet:
        console.print(f"[blue]{log_desc}...[/blue]")

    # Prepare environment
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    # Prepare stdio redirects
    stdout_redirect = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    stderr_redirect = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL

    try:
        # Create and run process
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdout=stdout_redirect,
            stderr=stderr_redirect,
            env=process_env,
        )

        # Wait for completion with timeout
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )

        # Decode output
        stdout = stdout_bytes.decode() if stdout_bytes else ""
        stderr = stderr_bytes.decode() if stderr_bytes else ""

        result = ProcessResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            command=command,
        )

        # Log result
        if not quiet:
            if result.success:
                console.print(f"[green]{log_desc} completed successfully[/green]")
            else:
                console.print(f"[red]{log_desc} failed with exit code {result.returncode}[/red]")
                if stderr and len(stderr) < 500:  # Only log short error messages
                    console.print(f"[red]Error: {stderr.strip()}[/red]")

        # Check for errors if requested
        if check and not result.success:
            error_msg = f"Command failed: {result.command_str}"
            if result.stderr:
                error_msg += f" - {result.stderr.strip()}"
            raise ProcessException(error_msg, result)

        return result

    except asyncio.TimeoutError:
        # Kill the process if it's still running
        if process.returncode is None:
            process.kill()
            await process.wait()

        result = ProcessResult(
            returncode=-1,
            stderr="Command timed out",
            command=command,
            timeout_occurred=True,
        )

        if not quiet:
            console.print(f"[red]{log_desc} timed out after {timeout}s[/red]")

        if check:
            error_msg = f"Command timed out after {timeout}s: {result.command_str}"
            raise ProcessException(error_msg, result)

        return result

    except Exception as e:
        if not quiet:
            console.print(f"[red]{log_desc} failed with exception: {e}[/red]")

        if check:
            raise ProcessException(f"Command failed: {command[0]} - {e}")

        # Return failure result
        return ProcessResult(
            returncode=-1,
            stderr=str(e),
            command=command,
        )


async def run_command_with_file_output(
    *args: str,
    output_file: Union[str, Path],
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    description: Optional[str] = None,
    quiet: bool = False,
) -> ProcessResult:
    """
    Run a command and redirect stdout to a file.

    Args:
        *args: Command and arguments
        output_file: File to write stdout to
        cwd: Working directory
        timeout: Command timeout in seconds
        env: Environment variables to add/override
        description: Human-readable description for logging
        quiet: Whether to suppress progress logging

    Returns:
        ProcessResult (stdout will be empty since it was redirected)
    """
    command = list(args)
    log_desc = description or f"Running {command[0]}"

    if not quiet:
        console.print(f"[blue]{log_desc} (output to {output_file})...[/blue]")

    # Prepare environment
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    try:
        # Open output file
        with open(output_file, 'w') as output_f:
            # Create and run process
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd) if cwd else None,
                stdout=output_f,
                stderr=asyncio.subprocess.PIPE,
                env=process_env,
            )

            # Wait for completion with timeout
            _, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )

        # Decode stderr
        stderr = stderr_bytes.decode() if stderr_bytes else ""

        result = ProcessResult(
            returncode=process.returncode,
            stdout="",  # Redirected to file
            stderr=stderr,
            command=command,
        )

        # Log result
        if not quiet:
            if result.success:
                console.print(f"[green]{log_desc} completed successfully[/green]")
            else:
                console.print(f"[red]{log_desc} failed with exit code {result.returncode}[/red]")
                if stderr and len(stderr) < 500:
                    console.print(f"[red]Error: {stderr.strip()}[/red]")

        return result

    except asyncio.TimeoutError:
        # Kill the process if it's still running
        if process.returncode is None:
            process.kill()
            await process.wait()

        result = ProcessResult(
            returncode=-1,
            stderr="Command timed out",
            command=command,
            timeout_occurred=True,
        )

        if not quiet:
            console.print(f"[red]{log_desc} timed out after {timeout}s[/red]")

        return result

    except Exception as e:
        if not quiet:
            console.print(f"[red]{log_desc} failed with exception: {e}[/red]")

        return ProcessResult(
            returncode=-1,
            stderr=str(e),
            command=command,
        )


# Specialized command runners for common tools

async def run_git_command(
    *args: str,
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 30.0,
    check: bool = True,
    description: Optional[str] = None,
) -> ProcessResult:
    """Run a git command with standard settings."""
    return await run_command(
        "git", *args,
        cwd=cwd,
        timeout=timeout,
        check=check,
        description=description or f"Git {args[0] if args else 'command'}",
    )


async def run_extraction_command(
    tool: str,
    *args: str,
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 300.0,
    quiet: bool = True,
    description: Optional[str] = None,
) -> ProcessResult:
    """Run an extraction tool command with standard settings."""
    return await run_command(
        tool, *args,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        check=False,  # Extraction tools often have non-zero exit codes for warnings
        quiet=quiet,
        description=description or f"Extraction with {tool}",
    )


async def run_download_command(
    tool: str,
    *args: str,
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 600.0,
    description: Optional[str] = None,
) -> ProcessResult:
    """Run a download command with standard settings."""
    return await run_command(
        tool, *args,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        check=False,  # Allow handling download failures gracefully
        description=description or f"Download with {tool}",
    )


async def run_analysis_command(
    tool: str,
    *args: str,
    output_file: Optional[Union[str, Path]] = None,
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 60.0,
    description: Optional[str] = None,
) -> ProcessResult:
    """Run an analysis tool command."""
    if output_file:
        return await run_command_with_file_output(
            tool, *args,
            output_file=output_file,
            cwd=cwd,
            timeout=timeout,
            description=description or f"Analysis with {tool}",
        )
    else:
        return await run_command(
            tool, *args,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            check=False,
            description=description or f"Analysis with {tool}",
        )


class ProcessException(Exception):
    """Exception raised by process execution failures."""

    def __init__(self, message: str, result: Optional[ProcessResult] = None):
        super().__init__(message)
        self.result = result


# Utility functions for common subprocess patterns

def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


async def check_tool_available(tool: str) -> bool:
    """Check if a command-line tool is available."""
    try:
        result = await run_command(
            "which", tool,
            capture_output=True,
            timeout=5.0,
            quiet=True,
        )
        return result.success
    except Exception:
        return False


async def find_files_in_directory(
    directory: Union[str, Path],
    pattern: str = "*",
    file_type: str = "f",  # f=files, d=directories
    max_depth: Optional[int] = None,
) -> List[Path]:
    """Find files/directories using the find command."""
    find_args = [".", "-type", file_type, "-name", pattern]

    if max_depth is not None:
        find_args.extend(["-maxdepth", str(max_depth)])

    result = await run_command(
        "find", *find_args,
        cwd=directory,
        timeout=30.0,
        quiet=True,
    )

    if result.success and result.stdout:
        paths = []
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                # Convert relative path to absolute
                abs_path = Path(directory) / line.strip().lstrip('./')
                paths.append(abs_path)
        return paths

    return []