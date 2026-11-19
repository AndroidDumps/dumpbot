"""Tests for pushing dumps to local bare repositories (served by cgit).

These run real git against temporary bare repositories; only the settings
paths and the external binaries/commands are pointed at test values.
"""

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dumpyarabot.config import settings
from dumpyarabot.repo_manager import (
    BranchAlreadyExistsError,
    InvalidRepoNameError,
    RepoManager,
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
        patch.object(settings, "REPO_INDEX_GIT_BINARY", "git"),
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


async def test_push_new_repo_then_second_branch(tmp_path, repo_settings):
    """A first dump creates and lists the repo; a second branch moves HEAD."""
    root, marker = repo_settings
    bare = root / "dumps" / "motorola" / "lagos.git"

    repo_url, repo_path = await _dump(tmp_path, "first")

    assert repo_path == "dumps/motorola/lagos"
    assert repo_url == f"https://dumps.example.org/dumps/motorola/lagos/tree/?h={BRANCH}"
    assert _ref(bare) == _git("rev-parse", "HEAD", cwd=tmp_path / "first")
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert marker.read_text() == "ran\n"

    await _dump(tmp_path, "second", branch="newer-build")

    assert _ref(bare)  # first branch kept
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == "refs/heads/newer-build"
    assert marker.read_text() == "ran\n"  # existing repo: list not refreshed


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
    assert _ref(bare) == _git("rev-parse", "HEAD", cwd=tmp_path / "third")


async def test_push_into_repo_owned_by_another_user(tmp_path, repo_settings, monkeypatch):
    """A full dump works on a repo that git considers foreign-owned."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    _git(f"--git-dir={bare}", "config", "receive.unpackLimit", "1")
    work_dir = _make_work_dir(tmp_path, "work", "content\n")

    # Git's own test knob: treat every repository as owned by someone else,
    # except the work dir, which the bot owns in production. Trust it via the
    # environment, which a local push does not pass on to receive-pack.
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(work_dir))

    # Without safe.directory, git refuses the target repo.
    plain = subprocess.run(  # noqa: ASYNC221 - test-only sanity check
        ["git", "rev-parse"], cwd=bare, capture_output=True, text=True, check=False
    )
    assert "dubious ownership" in plain.stderr

    await RepoManager(str(work_dir)).create_and_push_repository(_props())

    monkeypatch.delenv("GIT_TEST_ASSUME_DIFFERENT_OWNER")
    assert _ref(bare) == _git("rev-parse", "HEAD", cwd=work_dir)
    assert _git(f"--git-dir={bare}", "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert (bare / "objects" / "pack" / "multi-pack-index").exists()


async def test_push_does_not_run_auto_gc(tmp_path, repo_settings):
    """Pushes never trigger git's automatic repack of the target repo."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    # Keep each push as its own pack, and make `gc --auto` repack as soon as
    # there are two packs, in the foreground.
    for key, value in (
        ("receive.unpackLimit", "1"),
        ("gc.autoPackLimit", "1"),
        ("gc.autoDetach", "false"),
    ):
        _git(f"--git-dir={bare}", "config", key, value)

    await _dump(tmp_path, "first", branch="one")
    await _dump(tmp_path, "second", branch="two")

    assert len(list((bare / "objects" / "pack").glob("*.pack"))) == 2


async def test_push_does_not_run_target_repo_hooks(tmp_path, repo_settings):
    """Hooks inside an existing target repo never run as the bot."""
    root, _ = repo_settings
    bare = _init_bare(root / "dumps" / "motorola" / "lagos.git")
    marker = tmp_path / "hook-ran"
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    await _dump(tmp_path, "hooked", branch="one")

    assert _ref(bare, "one")
    assert not marker.exists()


async def test_push_through_symlinked_repo(tmp_path, repo_settings):
    """An existing repo symlinked to other storage receives the push and indexes."""
    root, _ = repo_settings
    storage = _init_bare(tmp_path / "other-storage" / "ab" / "cd.git")
    # Keep the received pack so there is something to build an index from.
    _git(f"--git-dir={storage}", "config", "receive.unpackLimit", "1")
    link = root / "dumps" / "motorola" / "lagos.git"
    link.parent.mkdir()
    link.symlink_to(storage)

    await _dump(tmp_path, "work")

    assert link.is_symlink()
    assert _ref(storage) == _git("rev-parse", "HEAD", cwd=tmp_path / "work")
    assert (storage / "objects" / "pack" / "multi-pack-index").exists()
    assert list((storage / "objects" / "pack").glob("*.bitmap"))


async def test_case_insensitive_lookup_uses_existing_repo_and_subgroup(tmp_path, repo_settings):
    """kddi/imnj lands in KDDI/imnj, asus/zs620kl in asus/ZS620KL, a new kddi repo in KDDI/."""
    root, _ = repo_settings
    existing = _init_bare(root / "dumps" / "KDDI" / "imnj.git")
    upper_repo = _init_bare(root / "dumps" / "asus" / "ZS620KL.git")

    repo_url, repo_path = await _dump(tmp_path, "one", repo_subgroup="kddi", repo_name="imnj")

    assert repo_path == "dumps/KDDI/imnj"
    assert "/dumps/KDDI/imnj/tree/" in repo_url
    assert _ref(existing) == _git("rev-parse", "HEAD", cwd=tmp_path / "one")

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
    """Concurrent dumps of one new device: one creates the repo, all push."""
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
            assert _ref(sub / f"{device}.git", branch)
    # One repo-list refresh per created repo.
    assert marker.read_text() == "ran\n" * len(devices)


async def test_index_and_repolist_failures_only_warn(tmp_path, repo_settings):
    """A failing index rebuild or repo-list command does not fail the dump."""
    root, _ = repo_settings
    with (
        patch.object(settings, "REPO_INDEX_GIT_BINARY", str(tmp_path / "no-such-git")),
        patch.object(settings, "CGIT_REPOLIST_COMMAND", "false"),
    ):
        await _dump(tmp_path, "work")

    assert _ref(root / "dumps" / "motorola" / "lagos.git")


def test_repo_web_url_encodes_branch():
    """Branch names are URL-encoded in cgit links."""
    with patch.object(settings, "REPO_PUBLIC_BASE_URL", "https://dumps.tadiphone.dev"):
        assert repo_web_url("dumps/oneplus/op1", "a b/c+d") == (
            "https://dumps.tadiphone.dev/dumps/oneplus/op1/tree/?h=a%20b%2Fc%2Bd"
        )
