"""Build a canonical version string from an app version + local git state.

Format: ``<app_version>[-<branch>]-<commit>[-dirty][ (<os_version>)]``

- ``app_version`` defaults to the repo's latest version tag
  (:func:`latest_version_tag`) when not passed explicitly, so the version
  lifecycle is driven by ``git tag``. It falls back to ``"NOVERSION"`` when
  the repo has no tags, or when the latest tag isn't in canonical version
  format (``CANONICAL_VERSION_RE`` — the ``X.x`` marketing form, e.g.
  ``2.0`` / ``1.3.4``).
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

import functools
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional, Union

_DEFAULT_MAIN_BRANCHES = ("main", "master")
_GIT_TIMEOUT_S = 2.0

#: Base version used when no canonical version tag is available.
NO_VERSION = "NOVERSION"

#: Canonical version format for a (component-less) tag: the ``X.x`` marketing
#: form — two or more dot-separated numeric components (``2.0``, ``1.3``,
#: ``10.4.1``). A tag must match this to be used as the base version; anything
#: else (``v2.0``, ``2``, ``2.0-rc1``, ``release``) is rejected in favour of
#: ``NO_VERSION``. Group 1 captures the bare version.
CANONICAL_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)+)$")


def _version_tag_re(component: Optional[str]) -> "re.Pattern[str]":
    """Regex whose group 1 is the version, for a component's tags.

    With a ``component`` the tag form is ``<component>-<X.x>`` (e.g.
    ``server-2.1``) — git tag names can't contain spaces, so a hyphen joins
    the component and the version. With no component it's the bare ``X.x``
    form (:data:`CANONICAL_VERSION_RE`). This namespaces tags per component
    so e.g. ``server-2.1`` and ``client-2.1`` can't shadow each other.
    """
    if not component:
        return CANONICAL_VERSION_RE
    return re.compile(r"^" + re.escape(component) + r"-(\d+(?:\.\d+)+)$")


@functools.lru_cache(maxsize=None)
def canonical_version(
    app_version: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
    *,
    main_branches: tuple[str, ...] = _DEFAULT_MAIN_BRANCHES,
    os_version: Optional[str] = None,
    component: Optional[str] = None,
) -> str:
    """Return ``<app_version>[-<branch>]-<commit>[-dirty][ (<os_version>)]``.

    Args:
        app_version: The app's semantic version, e.g. ``"1.3"``. When
            ``None`` (the default), the base is derived from the repo's
            latest version tag (:func:`latest_version_tag`); if there is no
            tag, or the latest tag isn't in canonical format
            (:data:`CANONICAL_VERSION_RE`), it falls back to
            :data:`NO_VERSION` (``"NOVERSION"``). Drives the version off
            ``git tag``.
        repo_root: Path to the git repo. Defaults to the current working
            directory.
        main_branches: Branch names that suppress the branch infix. Defaults
            to ``("main", "master")``.
        os_version: Optional runtime OS tag (e.g. ``"macOS-14.5"``). When
            non-None and non-empty, appended as `` (<os_version>)``. Use
            :func:`current_os` for a sensible default on the host.
        component: When set, the derived base comes from this component's
            tags only (``<component>-<X.x>``, e.g. ``server-2.1``), so
            sibling components don't shadow each other. Ignored when
            ``app_version`` is given. The component scopes which tag is read;
            it is *not* added to the returned string.

    Returns:
        Canonical version string. Falls back to ``f"{app_version}-unknown"``
        (still followed by the OS suffix if given) if git can't be invoked
        or the path isn't a repo.

    The result is cached (``functools.lru_cache``) keyed by the arguments, so
    a version shown many times in one process shells out to git only once.
    The version is treated as a snapshot for the process's lifetime: if git
    state changes afterwards (a new commit, the tree going dirty), the cached
    value is intentionally stable. Call ``canonical_version.cache_clear()`` to
    force a fresh read. (All arguments must be hashable for caching —
    ``main_branches`` is a tuple for this reason; pass ``repo_root`` as a
    ``str`` or ``Path``, both of which hash.)
    """
    cwd = str(repo_root) if repo_root is not None else None
    if app_version is None:
        app_version = latest_version_tag(repo_root, component) or NO_VERSION
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


def build_number(repo_root: Optional[Union[str, Path]] = None) -> Optional[int]:
    """Return the total commit count on HEAD (``git rev-list --count HEAD``).

    This is a monotonic, history-derived integer suitable for an Apple
    ``CFBundleVersion`` / ``CURRENT_PROJECT_VERSION``: it increases with every
    commit, never collides, and is reproducible for a given commit. Returns
    ``None`` if git can't be invoked or the path isn't a repo (caller decides
    the fallback).

    Note: a shallow clone undercounts. Callers that need a trustworthy number
    (e.g. cutting a release) should verify the worktree is not shallow.

    Args:
        repo_root: Path to the git repo. Defaults to the current directory.
    """
    cwd = str(repo_root) if repo_root is not None else None
    count = _git(["rev-list", "--count", "HEAD"], cwd)
    if count is None:
        return None
    try:
        return int(count)
    except ValueError:
        return None


def latest_version_tag(
    repo_root: Optional[Union[str, Path]] = None,
    component: Optional[str] = None,
) -> Optional[str]:
    """Return the highest *canonical-format* version, or ``None``.

    Lists tags by version order (``git tag --sort=-version:refname``, so
    ``2.10`` sorts above ``2.9`` — not lexically) and returns the bare
    version (``"2.1"``) of the first tag matching the canonical form. Tags
    that don't match (``v1.0``, ``nightly``, ``2.0-rc1``) are skipped, so a
    stray tag can't shadow a real release.

    With a ``component`` only that component's tags count
    (``<component>-<X.x>``, e.g. ``server-2.1``) and the returned value is
    still the bare version with the ``<component>-`` prefix stripped — so
    sibling components (``server`` / ``client``) keep separate lineages.

    Returns ``None`` if no matching tag exists or git is unavailable.

    Args:
        repo_root: Path to the git repo. Defaults to the current directory.
        component: Restrict to ``<component>-<X.x>`` tags. ``None`` matches
            bare ``X.x`` tags.
    """
    cwd = str(repo_root) if repo_root is not None else None
    pattern = _version_tag_re(component)
    out = _git(["tag", "--sort=-version:refname"], cwd)
    if not out:
        return None
    for line in out.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


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


# Generated artifacts of THIS versioning system. They are tracked (a committed
# placeholder must exist so Xcode's fileSystemSynchronizedGroups include them at
# project-load), and the build rewrites them on every run — BuildStamp.swift
# gets a fresh timestamp, CanonicalVersion.swift is regenerated — BEFORE the
# emit phase checks dirty. Excluding them stops the build from flagging itself
# dirty over its own outputs. They are never hand-edited source.
_GENERATED_EXCLUDES = (
    ":(exclude,glob)**/CanonicalVersion.swift",
    ":(exclude,glob)**/BuildStamp.swift",
)


def _is_dirty(cwd: Optional[str]) -> bool:
    # "Dirty" means uncommitted changes to TRACKED SOURCE (staged or unstaged).
    #   * `--untracked-files=no` drops untracked paths (build outputs, scratch
    #     docs) — not source changes.
    #   * the `_GENERATED_EXCLUDES` pathspecs drop this system's own generated
    #     stamp files, which the build rewrites before this check runs.
    # Empty output ⇔ no tracked source modifications.
    return bool(_git(
        ["status", "--porcelain", "--untracked-files=no", "--", ".", *_GENERATED_EXCLUDES],
        cwd,
    ))


def _with_os(base: str, os_version: Optional[str]) -> str:
    return f"{base} ({os_version})" if os_version else base
