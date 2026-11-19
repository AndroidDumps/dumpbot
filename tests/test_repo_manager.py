"""Tests for publishing dumps into local bare repositories (served by cgit).

These run real git against temporary bare repositories; only the settings
paths and the external binaries/commands are pointed at test values.
"""

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dumpyarabot import repo_manager
from dumpyarabot.config import Settings, settings
from dumpyarabot.repo_manager import (
    BranchAlreadyExistsError,
    InvalidRepoNameError,
    RepoManager,
    RepoPublishError,
    repo_web_url,
)

BRANCH = "lagos_g-user-15-VVOB35.78-71-9-ecbb89-release-keys"
DESCRIPTION = "lagos_g-user 15 VVOB35.78-71-9 ecbb89 release-keys"


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch):
    """Keep the developer's git config (signing, hooks, aliases) out of tests.

    Args:
        monkeypatch: pytest's monkeypatch fixture, used to set the git
            environment variables for the test only.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", raising=False)


@pytest.fixture
def repo_settings(tmp_path):
    """Point the repo settings at a temp root and a recording repolist script.

    Args:
        tmp_path: pytest's per-test temp dir, which holds the repo root, the
            script and its marker file.

    Yields:
        A ``(repo_root, marker)`` tuple; the script appends to ``marker``.
    """
    root = tmp_path / "git"
    (root / "dumps").mkdir(parents=True)
    marker = tmp_path / "repolist-ran"
    script = tmp_path / "cgit-repolist"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    script.chmod(0o755)
    with (
        patch.object(settings, "REPO_ROOT", str(root)),
        patch.object(settings, "CGIT_REPOLIST_COMMAND", str(script)),
        patch.object(settings, "REPO_PUBLIC_BASE_URL", "https://dumps.example.org/"),
    ):
        yield root, marker


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git synchronously for test setup and assertions.

    Args:
        *args: git arguments.
        cwd: Working directory.

    Returns:
        git's stripped stdout.

    Raises:
        subprocess.CalledProcessError: If git fails.
    """
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_work_dir(tmp_path: Path, name: str, content: str) -> Path:
    """Create a dump work dir holding one ``build.prop`` file.

    Args:
        tmp_path: Parent directory.
        name: Name of the work dir.
        content: Content of ``build.prop``.

    Returns:
        The new work dir.
    """
    work_dir = tmp_path / name
    work_dir.mkdir()
    (work_dir / "build.prop").write_text(content)
    return work_dir


def _props(**overrides):
    """Build device properties for a dump, with optional overrides.

    Args:
        **overrides: Keys to replace in the default properties.

    Returns:
        The device properties dict.
    """
    props = {
        "repo_subgroup": "motorola",
        "repo_name": "lagos",
        "branch": BRANCH,
        "description": DESCRIPTION,
    }
    props.update(overrides)
    return props


def _ref(bare: Path, branch: str = BRANCH) -> str:
    """Return the commit a branch of a bare repository points at.

    Args:
        bare: The bare repository.
        branch: Branch name.

    Returns:
        The commit hash.

    Raises:
        subprocess.CalledProcessError: If the branch does not exist.
    """
    return _git(f"--git-dir={bare}", "rev-parse", f"refs/heads/{branch}")


def _tree(bare: Path, branch: str = BRANCH) -> str:
    """Return the tree a branch of a bare repository points at.

    Args:
        bare: The bare repository.
        branch: Branch name.

    Returns:
        The tree hash.
    """
    return _git(f"--git-dir={bare}", "rev-parse", f"refs/heads/{branch}^{{tree}}")


def _git_add_tree(work_dir: Path) -> str:
    """Return the tree ``git add -A`` builds from a work dir, as the old flow did.

    Args:
        work_dir: Directory to add; it is not modified.

    Returns:
        The tree hash.
    """
    git_dir = work_dir.parent / f"{work_dir.name}-reference.git"
    _git("init", "--quiet", "--bare", str(git_dir))
    args = (f"--git-dir={git_dir}", f"--work-tree={work_dir}", "-c", "core.bare=false")
    _git(*args, "add", "-A")
    return _git(*args, "write-tree")


def _packs(bare: Path) -> set[str]:
    """List the pack directory of a repository.

    Args:
        bare: The bare repository.

    Returns:
        The names of all files in ``objects/pack``.
    """
    return {p.name for p in (bare / "objects" / "pack").iterdir()}


def _fsck(bare: Path) -> None:
    """Check that every branch of a repository has all its objects.

    Args:
        bare: The bare repository.

    Raises:
        subprocess.CalledProcessError: If an object is missing or corrupt.
    """
    _git(f"--git-dir={bare}", "fsck", "--full", "--no-dangling")


def _init_bare(bare: Path) -> Path:
    """Create an existing bare repository (and its parent dirs).

    Args:
        bare: Path of the repository.

    Returns:
        The repository path.
    """
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    return bare


async def _dump(tmp_path: Path, name: str, force: bool = False, **props) -> tuple[str, str]:
    """Run one dump of a fresh work dir through ``create_and_push_repository``.

    Args:
        tmp_path: Parent of the work dir.
        name: Name of the work dir; also used as its file content.
        force: Replace an existing branch.
        **props: Device property overrides.

    Returns:
        The ``(repo_url, repo_path)`` result.
    """
    work_dir = _make_work_dir(tmp_path, name, f"{name}\n")
    return await RepoManager(str(work_dir)).create_and_push_repository(
        _props(**props), force=force
    )


@pytest.mark.parametrize(
    "field,value,valid",
    [
        ("repo_subgroup", "..", False),
        ("repo_name", "a/b", False),
        ("repo_name", "Bad!", False),
        ("repo_name", "dev.v2_x-1", True),
    ],
)
async def test_repo_names_are_validated(tmp_path, repo_settings, field, value, valid):
    """Unsafe names from firmware are rejected before anything is created."""
    root, _ = repo_settings
    if valid:
        _, repo_path = await _dump(tmp_path, "work", **{field: value})
        assert repo_path == f"dumps/motorola/{value}"
        return

    with pytest.raises(InvalidRepoNameError, match=field):
        await _dump(tmp_path, "work", **{field: value})
    assert list((root / "dumps").iterdir()) == []
    assert not (tmp_path / "work" / ".git").exists()


async def test_publish_new_repo_then_second_branch(tmp_path, repo_settings):
    """A first dump creates and lists the repo; a second branch moves HEAD."""
    root, marker = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"

    repo_url, repo_path = await _dump(tmp_path, "first")

    assert repo_path == "dumps/motorola/lagos"
    assert repo_url == f"https://dumps.example.org/dumps/motorola/lagos/tree/?h={BRANCH}"
    assert _tree(bare) == _git_add_tree(tmp_path / "first")
    assert _git(f"--git-dir={bare}", "log", "-1", "--format=%an <%ae>|%cn <%ce>|%B", BRANCH) == (
        "dumper <dumper@dumps.example.org>|dumper <dumper@dumps.example.org>|"
        f"{DESCRIPTION}\n\nSigned-off-by: dumper <dumper@dumps.example.org>"
    )
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert marker.read_text() == "ran\n"
    assert not [p for p in (tmp_path / "first").iterdir() if p.name != "build.prop"]

    await _dump(tmp_path, "second", branch="newer-build")

    assert _ref(bare)  # first branch kept
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == "refs/heads/newer-build"
    assert marker.read_text() == "ran\n"  # existing repo: list not refreshed
    assert (bare / "objects" / "pack" / "multi-pack-index").exists()


async def test_published_tree_matches_git_add(tmp_path, repo_settings):
    """Modes, symlinks, odd names and large files give the tree `git add -A` gives."""
    root, _ = repo_settings
    work = tmp_path / "work"
    (work / "system" / "bin").mkdir(parents=True)
    (work / "system" / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
    (work / "system" / "bin" / "sh").chmod(0o755)
    (work / "group-exec-only").write_text("x\n")
    (work / "group-exec-only").chmod(0o654)
    (work / "empty").write_bytes(b"")
    (work / "with space ünïcode.txt").write_text("ünïcode\n")
    (work / os.fsdecode(b"latin1-\xe9")).write_bytes(b"not utf-8 name\n")
    # Larger than the read size, so it is streamed in several pieces.
    (work / "big.img").write_bytes(os.urandom(repo_manager.READ_CHUNK * 3 + 5))
    (work / "link-to-file").symlink_to("system/bin/sh")
    (work / "link-to-dir").symlink_to("system")
    (work / "dangling").symlink_to("../does/not/exist")
    (work / "empty-dir" / "nested").mkdir(parents=True)
    (work / "vendor" / ".git").mkdir(parents=True)
    (work / "vendor" / ".git" / "config").write_text("not published\n")
    (work / "vendor" / "file").write_text("published\n")
    bare = root / "dumps" / "motorola" / "lagos.git"

    with patch.object(settings, "REPO_IMPORT_JOBS", 3):
        await RepoManager(str(work)).create_and_push_repository(_props())

    assert _tree(bare) == _git_add_tree(work)
    assert _git(f"--git-dir={bare}", "cat-file", "blob", f"{BRANCH}:dangling") == "../does/not/exist"
    _fsck(bare)


async def test_small_imports_become_packs(tmp_path, repo_settings):
    """Every import process writes a pack, even for a few bytes, so nothing is lost."""
    root, _ = repo_settings
    work = tmp_path / "work"
    work.mkdir()
    for i in range(7):
        (work / f"prop{i}").write_text(f"ro.value={i}\n")
    (work / "big.img").write_bytes(os.urandom(repo_manager.READ_CHUNK + 1))
    bare = root / "dumps" / "motorola" / "lagos.git"

    with patch.object(settings, "REPO_IMPORT_JOBS", 4):
        await RepoManager(str(work)).create_and_push_repository(_props())

    # Four blob packs plus one for the trees and the commit; no loose objects.
    assert len([p for p in _packs(bare) if p.endswith(".pack")]) == 5
    assert "count: 0" in _git(f"--git-dir={bare}", "count-objects", "-v")
    assert _tree(bare) == _git_add_tree(work)
    _fsck(bare)


@pytest.mark.parametrize(
    "break_repo, step",
    [
        ("not-a-repo", "checking the branch"),
        ("read-only-refs", "publishing the dump"),
    ],
)
async def test_repo_failures_hide_git_details(tmp_path, repo_settings, break_repo, step):
    """A failing git step reports only the step, never the command or paths."""
    root, _ = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"
    if break_repo == "not-a-repo":
        bare.mkdir(parents=True)
    else:
        _init_bare(bare)
        (bare / "refs" / "heads").chmod(0o555)

    try:
        with pytest.raises(RepoPublishError) as excinfo:
            await _dump(tmp_path, "broken")
    finally:
        if break_repo == "read-only-refs":
            (bare / "refs" / "heads").chmod(0o755)

    message = str(excinfo.value)
    assert step in message
    assert str(tmp_path) not in message and "git " not in message
    assert excinfo.value.__suppress_context__


async def test_existing_branch_needs_force(tmp_path, repo_settings):
    """An existing branch is refused without force and replaced with force."""
    root, _ = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"
    await _dump(tmp_path, "first")
    original = _ref(bare)

    with pytest.raises(BranchAlreadyExistsError):
        await _dump(tmp_path, "second")
    assert _ref(bare) == original

    await _dump(tmp_path, "third", force=True)
    assert _ref(bare) != original
    assert _tree(bare) == _git_add_tree(tmp_path / "third")
    _fsck(bare)


async def test_branch_published_meanwhile_is_not_overwritten(tmp_path, repo_settings, monkeypatch):
    """If another worker publishes the branch during the import, its branch is kept intact."""
    root, _ = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"
    real_import = RepoManager._import
    winner: dict[str, set[str]] = {}

    async def import_then_lose_race(self, store, description):
        commit = await real_import(self, store, description)
        if not winner:
            winner["packs"] = set()  # the winner's own import must not race again
            await _dump(tmp_path, "winner")
            winner["packs"] = _packs(bare)
        return commit

    monkeypatch.setattr(RepoManager, "_import", import_then_lose_race)

    with pytest.raises(BranchAlreadyExistsError):
        await _dump(tmp_path, "loser")

    assert not list((tmp_path / "loser").glob(".publish-*"))  # private store removed
    assert _tree(bare) == _git_add_tree(tmp_path / "winner")
    _fsck(bare)
    # Our packs stay behind, but nothing references them.
    leftovers = {p for p in _packs(bare) - winner["packs"] if p.endswith(".pack")}
    assert leftovers
    reachable = {
        line.split()[0]
        for line in _git(f"--git-dir={bare}", "rev-list", "--objects", "--all").splitlines()
    }
    for pack in leftovers:
        index = (bare / "objects" / "pack" / pack.replace(".pack", ".idx")).read_bytes()
        contents = subprocess.run(  # noqa: ASYNC221 - test-only check
            ["git", "show-index"], input=index, check=True, capture_output=True
        ).stdout.decode()
        assert not {line.split()[1] for line in contents.splitlines()} & reachable


async def test_packs_are_copied_across_filesystems(tmp_path, repo_settings, monkeypatch):
    """Without a shared filesystem, packs are copied (writable, not 0444 links)."""
    root, _ = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"
    await _dump(tmp_path, "linked", branch="linked")
    linked = {p: (bare / "objects" / "pack" / p).stat() for p in _packs(bare)}
    monkeypatch.setattr(repo_manager, "same_filesystem", lambda a, b: False)

    await _dump(tmp_path, "copied", branch="copied")

    copied = {
        p: (bare / "objects" / "pack" / p).stat()
        for p in _packs(bare) - set(linked)
        if p.endswith((".pack", ".idx"))
    }
    assert copied
    assert all(not s.st_mode & stat.S_IWUSR for p, s in linked.items() if p.endswith(".pack"))
    assert all(s.st_mode & stat.S_IWUSR for s in copied.values())
    assert not [p for p in _packs(bare) if p.startswith("tmp_")]
    assert _tree(bare, "copied") == _git_add_tree(tmp_path / "copied")
    _fsck(bare)


async def test_publish_into_repo_owned_by_another_user(tmp_path, repo_settings, monkeypatch):
    """A full dump works on a repo that git considers foreign-owned."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    work_dir = _make_work_dir(tmp_path, "work", "content\n")

    # Git's own test knob: treat every repository as owned by someone else.
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    # Without safe.directory, git refuses the target repo.
    plain = subprocess.run(  # noqa: ASYNC221 - test-only sanity check
        ["git", "rev-parse"], cwd=bare, capture_output=True, text=True, check=False
    )
    assert "dubious ownership" in plain.stderr

    await RepoManager(str(work_dir)).create_and_push_repository(_props())

    monkeypatch.delenv("GIT_TEST_ASSUME_DIFFERENT_OWNER")
    assert _tree(bare) == _git_add_tree(work_dir)
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert (bare / "objects" / "pack" / "multi-pack-index").exists()


async def test_publish_does_not_run_target_repo_hooks(tmp_path, repo_settings):
    """Ref-update hooks inside an existing target repo never run as the bot."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    marker = tmp_path / "hook-ran"
    hook = bare / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    await _dump(tmp_path, "hooked", branch="one")

    assert _ref(bare, "one")
    assert not marker.exists()
    # The hook is live: a plain ref update runs it.
    _git(f"--git-dir={bare}", "update-ref", "refs/heads/two", _ref(bare, "one"))
    assert marker.exists()


async def test_publish_through_symlinked_repo(tmp_path, repo_settings):
    """An existing repo symlinked to other storage receives the packs and indexes."""
    root, _ = repo_settings
    storage = _init_bare(tmp_path / "other-storage" / "ab" / "cd.git")
    link = root / "dumps" / "motorola" / "lagos.git"
    link.parent.mkdir()
    link.symlink_to(storage)

    await _dump(tmp_path, "work")

    assert link.is_symlink()
    assert _tree(storage) == _git_add_tree(tmp_path / "work")
    assert (storage / "objects" / "pack" / "multi-pack-index").exists()
    assert list((storage / "objects" / "pack").glob("*.bitmap"))
    _fsck(storage)


async def test_case_insensitive_lookup_uses_existing_repo_and_subgroup(tmp_path, repo_settings):
    """kddi/imnj lands in KDDI/imnj, asus/zs620kl in asus/ZS620KL, a new kddi repo in KDDI/."""
    root, _ = repo_settings
    existing = _init_bare(root / "dumps" / "KDDI" / "imnj.git")
    upper_repo = _init_bare(root / "dumps" / "asus" / "ZS620KL.git")

    repo_url, repo_path = await _dump(tmp_path, "one", repo_subgroup="kddi", repo_name="imnj")

    assert repo_path == "dumps/KDDI/imnj"
    assert "/dumps/KDDI/imnj/tree/" in repo_url
    assert _tree(existing) == _git_add_tree(tmp_path / "one")

    _, repo_path = await _dump(tmp_path, "two", repo_subgroup="kddi", repo_name="newdev")

    assert repo_path == "dumps/KDDI/newdev"
    assert _ref(root / "dumps" / "KDDI" / "newdev.git")

    _, repo_path = await _dump(tmp_path, "three", repo_subgroup="asus", repo_name="zs620kl")

    assert repo_path == "dumps/asus/ZS620KL"
    assert _ref(upper_repo)
    assert sorted(p.name for p in (root / "dumps").iterdir()) == ["KDDI", "asus"]
    assert [p.name for p in (root / "dumps" / "asus").iterdir()] == ["ZS620KL.git"]


async def test_exact_case_match_wins(tmp_path, repo_settings):
    """With both kddi/ and KDDI/ present, a kddi dump goes to kddi/."""
    root, _ = repo_settings
    upper = _init_bare(root / "dumps" / "KDDI" / "imnj.git")
    lower = _init_bare(root / "dumps" / "kddi" / "imnj.git")

    await _dump(tmp_path, "work", repo_subgroup="kddi", repo_name="imnj")

    assert _ref(lower)
    assert _git(f"--git-dir={upper}", "for-each-ref") == ""


async def test_concurrent_dumps_into_same_new_repo(tmp_path, repo_settings):
    """Concurrent dumps of one new device: one creates the repo, all publish."""
    root, marker = repo_settings
    devices = [f"dev{i}" for i in range(8)]
    branches = ["a", "b", "c"]

    await asyncio.gather(*(
        _dump(tmp_path, f"{device}-{branch}", repo_name=device, branch=branch)
        for device in devices for branch in branches
    ))

    sub = root / "dumps" / "motorola"
    assert sorted(p.name for p in sub.iterdir()) == sorted(f"{d}.git" for d in devices)
    for device in devices:
        for branch in branches:
            assert _tree(sub / f"{device}.git", branch) == _git_add_tree(
                tmp_path / f"{device}-{branch}"
            )
        _fsck(sub / f"{device}.git")
    # One repo-list refresh per created repo.
    assert marker.read_text() == "ran\n" * len(devices)


async def test_index_and_repolist_failures_only_warn(tmp_path, repo_settings):
    """A failing index rebuild or repo-list command does not fail the dump."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    # A stale lock, as a concurrent rebuild leaves it, makes the rebuild fail.
    lock = bare / "objects" / "pack" / "multi-pack-index.lock"
    lock.write_text("")
    with patch.object(settings, "CGIT_REPOLIST_COMMAND", "false"):
        await _dump(tmp_path, "work")

    assert _tree(bare) == _git_add_tree(tmp_path / "work")
    assert lock.exists()
    assert not (bare / "objects" / "pack" / "multi-pack-index").exists()


def test_removed_settings_in_env_file_are_ignored(tmp_path):
    """Old keys left in a server .env do not stop the bot from starting."""
    env_file = tmp_path / ".env"
    env_file.write_text("REPO_INDEX_GIT_BINARY=/path/to/git\nREPO_GIT_BINARY=git\n")

    loaded = Settings(_env_file=env_file)

    assert not hasattr(loaded, "REPO_INDEX_GIT_BINARY")
    assert not hasattr(loaded, "REPO_GIT_BINARY")


def test_repo_web_url_encodes_branch():
    """Branch names are URL-encoded in cgit links."""
    with patch.object(settings, "REPO_PUBLIC_BASE_URL", "https://dumps.tadiphone.dev"):
        assert repo_web_url("dumps/oneplus/op1", "a b/c+d") == (
            "https://dumps.tadiphone.dev/dumps/oneplus/op1/tree/?h=a%20b%2Fc%2Bd"
        )
