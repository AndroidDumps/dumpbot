"""File utilities for path operations, glob patterns, and file management."""

import os
import shutil
from pathlib import Path
from typing import List, Optional, Union, Iterator, Tuple

from rich.console import Console

console = Console()


def expand_glob_paths(base_dir: Union[str, Path], pattern: str) -> List[Path]:
    """
    Expand glob patterns in paths and return existing files.

    Args:
        base_dir: Base directory to search in
        pattern: Glob pattern (may contain wildcards)

    Returns:
        List of existing file paths that match the pattern
    """
    base_path = Path(base_dir)

    if "*" in pattern:
        # Use glob pattern matching
        expanded = list(base_path.glob(pattern))
        return [p for p in expanded if p.is_file()]
    else:
        # Direct path check
        full_path = base_path / pattern
        return [full_path] if full_path.is_file() else []


def find_files_by_pattern(
    base_dir: Union[str, Path],
    patterns: List[str],
    recursive: bool = True
) -> List[Path]:
    """
    Find files matching any of the given patterns.

    Args:
        base_dir: Directory to search in
        patterns: List of glob patterns to match
        recursive: Whether to search recursively

    Returns:
        List of file paths matching any pattern
    """
    base_path = Path(base_dir)
    found_files = []

    for pattern in patterns:
        if recursive:
            # Use rglob for recursive search
            matches = list(base_path.rglob(pattern))
        else:
            # Use glob for single level search
            matches = list(base_path.glob(pattern))

        # Only include files (not directories)
        found_files.extend(p for p in matches if p.is_file())

    # Remove duplicates while preserving order
    unique_files = []
    seen = set()
    for file_path in found_files:
        if file_path not in seen:
            unique_files.append(file_path)
            seen.add(file_path)

    return unique_files


def find_first_file_by_patterns(
    base_dir: Union[str, Path],
    patterns: List[str],
    recursive: bool = True
) -> Optional[Path]:
    """
    Find the first file matching any of the given patterns.

    Args:
        base_dir: Directory to search in
        patterns: List of glob patterns to match (in priority order)
        recursive: Whether to search recursively

    Returns:
        First matching file path or None if not found
    """
    for pattern in patterns:
        files = find_files_by_pattern(base_dir, [pattern], recursive)
        if files:
            return files[0]
    return None


def move_file_to_root(source_path: Path, target_dir: Path) -> Optional[Path]:
    """
    Move a file to the target directory root.

    Args:
        source_path: Source file path
        target_dir: Target directory

    Returns:
        New file path or None if operation failed
    """
    if not source_path.exists() or not source_path.is_file():
        return None

    target_path = target_dir / source_path.name

    # Don't move if already in the right place
    if source_path.resolve() == target_path.resolve():
        return target_path

    try:
        # Use shutil.move for cross-filesystem moves
        shutil.move(str(source_path), str(target_path))
        console.print(f"[blue]Moved {source_path.name} to root directory[/blue]")
        return target_path
    except Exception as e:
        console.print(f"[yellow]Failed to move {source_path.name}: {e}[/yellow]")
        return None


def copy_file_to_directory(source_path: Path, target_dir: Path) -> Optional[Path]:
    """
    Copy a file to a target directory.

    Args:
        source_path: Source file path
        target_dir: Target directory

    Returns:
        New file path or None if operation failed
    """
    if not source_path.exists() or not source_path.is_file():
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source_path.name

    try:
        shutil.copy2(str(source_path), str(target_path))
        console.print(f"[green]Copied {source_path.name} to {target_dir}[/green]")
        return target_path
    except Exception as e:
        console.print(f"[yellow]Failed to copy {source_path.name}: {e}[/yellow]")
        return None


def get_file_size_formatted(file_path: Union[str, Path]) -> str:
    """
    Get formatted file size in human readable format.

    Args:
        file_path: Path to the file

    Returns:
        Formatted file size string
    """
    try:
        size = os.path.getsize(file_path)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
    except OSError:
        return "Unknown size"


def safe_remove_file(file_path: Union[str, Path]) -> bool:
    """
    Safely remove a file, ignoring errors.

    Args:
        file_path: Path to file to remove

    Returns:
        True if file was removed or didn't exist, False if error occurred
    """
    try:
        Path(file_path).unlink()
        return True
    except FileNotFoundError:
        return True  # File already doesn't exist
    except Exception:
        return False


def safe_remove_directory(dir_path: Union[str, Path]) -> bool:
    """
    Safely remove a directory tree, ignoring errors.

    Args:
        dir_path: Path to directory to remove

    Returns:
        True if directory was removed or didn't exist, False if error occurred
    """
    try:
        shutil.rmtree(dir_path)
        return True
    except FileNotFoundError:
        return True  # Directory already doesn't exist
    except Exception:
        return False


def ensure_directory_exists(dir_path: Union[str, Path]) -> Path:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        dir_path: Directory path to create

    Returns:
        Path object for the directory
    """
    path = Path(dir_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_latest_file_in_directory(
    directory: Union[str, Path],
    pattern: str = "*"
) -> Optional[Path]:
    """
    Get the most recently modified file in a directory.

    Args:
        directory: Directory to search
        pattern: File pattern to match

    Returns:
        Path to the latest file or None if no files found
    """
    dir_path = Path(directory)

    if not dir_path.exists() or not dir_path.is_dir():
        return None

    try:
        files = [f for f in dir_path.glob(pattern) if f.is_file()]
        if not files:
            return None

        # Sort by modification time, most recent first
        latest_file = max(files, key=lambda f: f.stat().st_mtime)
        return latest_file
    except Exception:
        return None


def clean_filename(filename: str, replacement: str = "_") -> str:
    """
    Clean a filename by removing/replacing invalid characters.

    Args:
        filename: Original filename
        replacement: Character to replace invalid chars with

    Returns:
        Cleaned filename safe for filesystem use
    """
    # Characters that are problematic in filenames
    invalid_chars = '<>:"/\\|?*'

    cleaned = filename
    for char in invalid_chars:
        cleaned = cleaned.replace(char, replacement)

    # Remove leading/trailing dots and spaces
    cleaned = cleaned.strip('. ')

    # Ensure we don't end up with an empty filename
    if not cleaned:
        cleaned = "untitled"

    return cleaned


def get_relative_path_list(
    base_dir: Union[str, Path],
    exclude_patterns: Optional[List[str]] = None
) -> List[str]:
    """
    Get a list of all files relative to base directory.

    Args:
        base_dir: Base directory to scan
        exclude_patterns: List of patterns to exclude

    Returns:
        List of relative file paths as strings
    """
    base_path = Path(base_dir)
    exclude_patterns = exclude_patterns or []

    if not base_path.exists():
        return []

    relative_paths = []

    # Walk through all files recursively
    for file_path in base_path.rglob("*"):
        if file_path.is_file():
            # Get relative path
            try:
                rel_path = file_path.relative_to(base_path)
                rel_path_str = str(rel_path).replace("\\", "/")  # Normalize separators

                # Check exclude patterns
                excluded = False
                for pattern in exclude_patterns:
                    if pattern in rel_path_str:
                        excluded = True
                        break

                if not excluded:
                    relative_paths.append(rel_path_str)
            except ValueError:
                # Path is not relative to base_dir, skip it
                continue

    return sorted(relative_paths)


def partition_files_by_type(
    files: List[Path],
    extensions: Optional[List[str]] = None
) -> Tuple[List[Path], List[Path]]:
    """
    Partition files into two groups based on file extensions.

    Args:
        files: List of file paths
        extensions: List of extensions to match (e.g., ['.img', '.bin'])

    Returns:
        Tuple of (matching_files, other_files)
    """
    if not extensions:
        return files, []

    # Normalize extensions to lowercase
    extensions = [ext.lower() for ext in extensions]

    matching = []
    other = []

    for file_path in files:
        if file_path.suffix.lower() in extensions:
            matching.append(file_path)
        else:
            other.append(file_path)

    return matching, other


def create_file_manifest(
    base_dir: Union[str, Path],
    output_file: Union[str, Path],
    exclude_patterns: Optional[List[str]] = None
) -> bool:
    """
    Create a file manifest listing all files in a directory.

    Args:
        base_dir: Directory to scan
        output_file: Output file for the manifest
        exclude_patterns: Patterns to exclude from manifest

    Returns:
        True if manifest was created successfully
    """
    try:
        file_list = get_relative_path_list(base_dir, exclude_patterns)

        with open(output_file, 'w') as f:
            for file_path in file_list:
                f.write(f"{file_path}\n")

        console.print(f"[green]Created file manifest with {len(file_list)} files[/green]")
        return True
    except Exception as e:
        console.print(f"[red]Failed to create file manifest: {e}[/red]")
        return False