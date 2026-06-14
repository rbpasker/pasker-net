"""Build a canonical version string from an app version + local git state.

Format: ``<app_version>[-<branch>]-<commit>[-dirty]``

- ``branch`` is omitted when HEAD is on a trunk branch (default: ``main`` or
  ``master``). Detached HEAD is also treated as no-branch.
- ``commit`` is the short HEAD hash (``git rev-parse --short HEAD``).
- ``dirty`` is appended when the worktree has any uncommitted changes
  (tracked or untracked, matching ``git status --porcelain``).

If git is unavailable or the path isn't a repo, returns
``<app_version>-unknown`` rather than raising.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Union

_DEFAULT_MAIN_BRANCHES = ("main", "master")
_GIT_TIMEOUT_S = 2.0


def canonical_version(
    app_version: str,
    repo_root: Optional[Union[str, Path]] = None,
    *,
    main_branches: tuple[str, ...] = _DEFAULT_MAIN_BRANCHES,
) -> str:
    """Return ``<app_version>[-<branch>]-<commit>[-dirty]``.

    Args:
        app_version: The app's semantic version, e.g. ``"1.3"``.
        repo_root: Path to the git repo. Defaults to the current working
            directory.
        main_branches: Branch names that suppress the branch infix. Defaults
            to ``("main", "master")``.

    Returns:
        Canonical version string. Falls back to ``f"{app_version}-unknown"``
        if git can't be invoked or the path isn't a repo.
    """
    cwd = str(repo_root) if repo_root is not None else None
    commit = _git(["rev-parse", "--short", "HEAD"], cwd)
    if commit is None:
        return f"{app_version}-unknown"

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _is_dirty(cwd)

    parts = [app_version]
    if branch and branch != "HEAD" and branch not in main_branches:
        parts.append(branch)
    parts.append(commit)
    if dirty:
        parts.append("dirty")
    return "-".join(parts)


def _git(args: list[str], cwd: Optional[str]) -> Optional[str]:
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", cwd]
    cmd += args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _is_dirty(cwd: Optional[str]) -> bool:
    # `git status --porcelain` prints one line per changed/untracked path;
    # empty output ⇔ clean worktree.
    return bool(_git(["status", "--porcelain"], cwd))
