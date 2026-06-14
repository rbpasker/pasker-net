"""Build a canonical version string from an app version + local git state.

Format: ``<app_version>[-<branch>]-<commit>[-dirty][ (<os_version>)]``

- ``branch`` is omitted when HEAD is on a trunk branch (default: ``main`` or
  ``master``). Detached HEAD is also treated as no-branch.
- ``commit`` is the short HEAD hash (``git rev-parse --short HEAD``).
- ``dirty`` is appended when the worktree has any uncommitted changes
  (tracked or untracked, matching ``git status --porcelain``).
- ``os_version`` is an optional caller-supplied runtime tag (e.g.
  ``"macOS-14.5"``); when given, it appears as `` (<os_version>)`` at the
  end. ``current_os()`` is a convenience that builds one for the host.

If git is unavailable or the path isn't a repo, returns
``<app_version>-unknown`` (still followed by `` (<os_version>)`` if given)
rather than raising.
"""
from __future__ import annotations

import platform
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
    os_version: Optional[str] = None,
) -> str:
    """Return ``<app_version>[-<branch>]-<commit>[-dirty][ (<os_version>)]``.

    Args:
        app_version: The app's semantic version, e.g. ``"1.3"``.
        repo_root: Path to the git repo. Defaults to the current working
            directory.
        main_branches: Branch names that suppress the branch infix. Defaults
            to ``("main", "master")``.
        os_version: Optional runtime OS tag (e.g. ``"macOS-14.5"``). When
            non-None and non-empty, appended as `` (<os_version>)``. Use
            :func:`current_os` for a sensible default on the host.

    Returns:
        Canonical version string. Falls back to ``f"{app_version}-unknown"``
        (still followed by the OS suffix if given) if git can't be invoked
        or the path isn't a repo.
    """
    cwd = str(repo_root) if repo_root is not None else None
    commit = _git(["rev-parse", "--short", "HEAD"], cwd)
    if commit is None:
        return _with_os(f"{app_version}-unknown", os_version)

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _is_dirty(cwd)

    parts = [app_version]
    if branch and branch != "HEAD" and branch not in main_branches:
        parts.append(branch)
    parts.append(commit)
    if dirty:
        parts.append("dirty")
    return _with_os("-".join(parts), os_version)


def current_os() -> str:
    """Return a short OS tag for the host, e.g. ``"macOS-14.5"`` or
    ``"Linux-6.5.0-21-generic"``.

    Intended as a convenience for the ``os_version`` parameter of
    :func:`canonical_version`. Never raises; falls back to
    ``"<system>-<release>"`` for anything not specifically handled.
    """
    system = platform.system()
    if system == "Darwin":
        mac = platform.mac_ver()[0]
        return f"macOS-{mac}" if mac else f"macOS-{platform.release()}"
    return f"{system}-{platform.release()}"


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


def _with_os(base: str, os_version: Optional[str]) -> str:
    return f"{base} ({os_version})" if os_version else base
