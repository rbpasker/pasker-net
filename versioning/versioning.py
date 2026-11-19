"""Build a canonical version string from an app version + local git state.

Format: ``<app_version>[-<branch>]-<commit>[-dirty][ (<os_version>)]``

- ``app_version`` defaults to the repo's latest version tag
  (:func:`latest_version_tag`) when not passed explicitly, so the version
  lifecycle is driven by ``git tag``. Once HEAD is ahead of that tag it becomes
  the NEXT MINOR (tag ``0.8`` -> ``0.9-<rev>`` on later commits) until the next
  release is tagged; see :func:`_resolve_app_version`. It falls back to ``"NOVERSION"`` when
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional, Union

_DEFAULT_MAIN_BRANCHES = ("main", "master")
_GIT_TIMEOUT_S = 5.0
#: A timed-out git call is retried (a loaded machine — e.g. a parallel build —
#: can stall it past the timeout); after this many attempts a STRICT call raises
#: :class:`GitError` instead of reporting "nothing found".
_GIT_ATTEMPTS = 3


class GitError(RuntimeError):
    """git did not answer (timed out on every retry) for a lookup whose
    "nothing found" result would be indistinguishable from a real answer — the
    tag lookup (would read as ``NOVERSION``) and the ahead-of-tag check. Raised
    rather than letting a transient stall publish a wrong version. A repo that
    simply isn't one, or a missing git binary, are NOT this: they keep the
    documented ``unknown`` fallback."""

#: Base version used when no canonical version tag is available.
NO_VERSION = "NOVERSION"

#: Canonical version format for a (component-less) tag: the ``X.x`` marketing
#: form — two or more dot-separated numeric components (``2.0``, ``1.3``,
#: ``10.4.1``). A tag must match this to be used as the base version; anything
#: else (``v2.0``, ``2``, ``2.0-rc1``, ``release``) is rejected in favour of
#: ``NO_VERSION``. Group 1 captures the bare version.
CANONICAL_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)+)$")


#: Parses any version tag into (component, version): group 1 is the component
#: (``None`` for a bare ``X.x`` tag), group 2 the bare version. The greedy
#: ``(.+)-`` lets component names contain hyphens (``server-side-2.1``).
_ANY_VERSION_TAG_RE = re.compile(r"^(?:(.+)-)?(\d+(?:\.\d+)+)$")


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


class VersionParts(NamedTuple):
    """The decomposed pieces of a canonical version string. :func:`_build`
    renders these into a string and :func:`parse_canonical_version` recovers
    them from one — the two are inverses. Fields absent from a given form are
    None/False: ``commit`` is None for the client form; ``build_time`` is set
    only for a dirty client build; ``branch`` is populated only when the build
    is dirty (a clean build is identified by its rev + commit, so the branch is
    omitted). ``_asdict()`` gives a plain dict of the pieces."""
    app_version: str
    branch: Optional[str] = None
    rev_number: Optional[int] = None
    commit: Optional[str] = None
    dirty: bool = False
    build_time: Optional[str] = None
    os_version: Optional[str] = None


def _build(parts: VersionParts) -> str:
    """Render a :class:`VersionParts` into the canonical string — the SINGLE
    place the format is defined, so :func:`canonical_version`,
    :func:`canonical_version_client`, and the Swift emitter's mirror can't
    drift. Layout:

        ``<app>[-<rev>][-<branch>][-<commit>][-dirty][ (<build_time>)][ (<os>)]``

    The branch renders whenever it is present in ``parts`` — :func:`_gather`
    decides presence per form (server: any non-trunk; client: only when dirty).
    ``commit`` distinguishes the server form from the client form; ``build_time``
    (a dirty client build; a Zulu-time ISO stamp) rides in its own parens before
    the OS suffix."""
    segs = [parts.app_version]
    if parts.rev_number is not None:
        segs.append(str(parts.rev_number))
    if parts.branch:
        segs.append(parts.branch)
    if parts.commit:
        segs.append(parts.commit)
    if parts.dirty:
        segs.append("dirty")
    out = "-".join(segs)
    if parts.build_time:
        out += f" ({parts.build_time})"
    if parts.os_version:
        out += f" ({parts.os_version})"
    return out


# A commit segment is a short/full hex SHA; a rev segment is decimal digits.
# A build_time is a Zulu-time ISO stamp (distinct in shape from an OS tag like
# "macOS-26.1"), so parse_canonical_version can classify a trailing "(…)" group
# by shape rather than position — robust even when the OS suffix is absent.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_BUILD_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z$")
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def parse_canonical_version(version: str) -> VersionParts:
    """Reverse of :func:`_build`: decompose a canonical version STRING back into
    its :class:`VersionParts`. For a version that arrived from elsewhere (e.g. a
    health check reading a server's ``/api/version``, where the local repo isn't
    the one that produced the string), so :func:`_gather` — which reads local
    git — can't help.

    Best-effort, anchored on the reliable pieces: the leading ``X.x`` app, a
    digit rev, a hex commit, a trailing ``-dirty``, and the parenthesised
    build-time / OS. It round-trips every string :func:`_build` produces. A
    branch name that is itself all-hex-or-digits could be mis-split — acceptable
    for the display/telemetry use this serves, not for security."""
    s = version
    os_version: Optional[str] = None
    build_time: Optional[str] = None
    # Peel up to two trailing "(…)" groups, classifying EACH by shape: a
    # Zulu-time ISO stamp is the build_time, anything else is the OS tag. Shape,
    # not position — so a lone group (a dirty build with no OS suffix) still
    # classifies correctly.
    for _ in range(2):
        m = _TRAILING_PAREN_RE.search(s)
        if m is None:
            break
        group = m.group(1)
        if _BUILD_TIME_RE.match(group):
            build_time = group
        else:
            os_version = group
        s = s[: m.start()]
    dirty = s.endswith("-dirty")
    if dirty:
        s = s[: -len("-dirty")]
    app_version, _, rest = s.partition("-")
    branch: Optional[str] = None
    rev_number: Optional[int] = None
    commit: Optional[str] = None
    if rest:
        segs = rest.split("-")
        if segs and segs[0].isdigit():
            rev_number = int(segs[0])
            segs = segs[1:]
        if segs and _COMMIT_RE.match(segs[-1]):
            commit = segs[-1]
            segs = segs[:-1]
        if segs:
            branch = "-".join(segs)
    return VersionParts(
        app_version=app_version, branch=branch, rev_number=rev_number,
        commit=commit, dirty=dirty, build_time=build_time, os_version=os_version,
    )


def _next_minor(version: str) -> str:
    """``0.8`` -> ``0.9``; ``1.3.4`` -> ``1.4`` (extras dropped). Matches
    ``next_version(..., "minor")`` in ``__main__`` — the number ``release.sh``
    will tag next."""
    major, minor = (int(n) for n in version.split(".")[:2])
    return f"{major}.{minor + 1}"


def _resolve_app_version(
    repo_root: Optional[Union[str, Path]], component: Optional[str],
) -> str:
    """Base version when the caller didn't pass one: the latest tag — but once
    HEAD is AHEAD of that tag (any commit after it), the NEXT MINOR, since a
    tagged release is frozen and further commits are development toward the
    next one: tag ``0.8`` at commit 63 builds as ``0.8-63``, the next commit as
    ``0.9-64``, until ``release.sh`` tags ``0.9``. At the tagged commit itself
    (clean or dirty) the tag version is kept. Major bumps stay a deliberate
    ``release.sh <c> major`` — development always previews the next minor.
    No canonical tag -> :data:`NO_VERSION`."""
    tag = latest_version_tag(repo_root, component)
    if tag is None:
        return NO_VERSION
    cwd = str(repo_root) if repo_root is not None else None
    ref = f"refs/tags/{component}-{tag}" if component else f"refs/tags/{tag}"
    since = _git(["rev-list", "--count", f"{ref}..HEAD"], cwd, strict=True)
    if since is not None and since.isdigit() and int(since) > 0:
        return _next_minor(tag)
    return tag


def _gather(
    app_version: Optional[str], repo_root: Optional[Union[str, Path]],
    main_branches: tuple[str, ...], os_version: Optional[str],
    component: Optional[str], *, with_commit: bool, with_build_time: bool,
) -> VersionParts:
    """Read local git state into a :class:`VersionParts`. ``with_commit``
    includes the short HEAD hash (server form); ``with_build_time`` stamps a
    Zulu-time build stamp on a DIRTY build (client form — a dirty build has no
    commit to point at). The branch is kept per form so the parts match what
    :func:`_build` renders (and the reverse parse round-trips): the SERVER form
    keeps it on any non-trunk branch; the CLIENT form only when dirty."""
    cwd = str(repo_root) if repo_root is not None else None
    if app_version is None:
        app_version = _resolve_app_version(repo_root, component)
    commit = _git(["rev-parse", "--short", "HEAD"], cwd) if with_commit else None
    if with_commit and commit is None:  # git unavailable / not a repo
        return VersionParts(app_version=app_version, commit="unknown", os_version=os_version)
    rev = build_number(cwd)
    if not with_commit and rev is None:
        return VersionParts(app_version=app_version, commit="unknown", os_version=os_version)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _is_dirty(cwd)
    on_named_branch = bool(branch and branch != "HEAD" and branch not in main_branches)
    build_time = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        if with_build_time and dirty else None
    )
    return VersionParts(
        app_version=app_version,
        # Server (with_commit): shown on any non-trunk. Client: only when dirty.
        branch=branch if (on_named_branch and (with_commit or dirty)) else None,
        rev_number=rev,
        commit=commit,
        dirty=dirty,
        build_time=build_time,
        os_version=os_version,
    )


@functools.lru_cache(maxsize=None)
def canonical_version(
    app_version: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
    *,
    main_branches: tuple[str, ...] = _DEFAULT_MAIN_BRANCHES,
    os_version: Optional[str] = None,
    component: Optional[str] = None,
) -> str:
    """The SERVER canonical form: ``<app>[-<rev>][-<branch>]-<commit>[-dirty][ (<os>)]``
    — e.g. ``2.0-767-50a5c3b (macOS-26.1)``. Keeps the commit hash for exact
    identification (``-dirty`` alone flags an unclean tree; the commit already
    pins the base). See :func:`canonical_version_client` for the Apple/iOS form
    (rev, no hash) and :func:`parse_canonical_version` to reverse either.

    ``app_version`` defaults to the repo's latest version tag (falling back to
    :data:`NO_VERSION`); ``component`` scopes which tag lineage is read;
    ``os_version`` (e.g. from :func:`current_os`) appends `` (<os>)``. Falls
    back to ``f"{app_version}-unknown"`` when git is unavailable. Cached by args
    (a stable per-process snapshot); ``cache_clear()`` forces a re-read."""
    return _build(_gather(
        app_version, repo_root, main_branches, os_version, component,
        with_commit=True, with_build_time=False,
    ))


@functools.lru_cache(maxsize=None)
def canonical_version_client(
    app_version: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
    *,
    main_branches: tuple[str, ...] = _DEFAULT_MAIN_BRANCHES,
    os_version: Optional[str] = None,
    component: Optional[str] = None,
) -> str:
    """The CLIENT / Apple canonical form:
    ``<app>[-<rev>][-<branch>][-dirty][ (<build_time>)][ (<os>)]`` — e.g.
    ``1.0-767 (iOS-26.5.1)``. No commit hash (matches ``CFBundleVersion``'s
    ``<X.x> (<rev>)`` and the Swift ``CanonicalVersion.full`` from
    ``emit_swift.py``); a DIRTY build appends a wall-clock ``build_time`` in
    place of the absent commit. Same args and caching as
    :func:`canonical_version`."""
    return _build(_gather(
        app_version, repo_root, main_branches, os_version, component,
        with_commit=False, with_build_time=True,
    ))


def _short(app_version: str, seq: Optional[int], dirty: bool) -> str:
    """Render ``M.m-seq[+]`` — the SINGLE place the short format is defined
    (used by :func:`short_version` and :func:`short_from_canonical`). A missing
    ``seq`` renders as ``unknown``; ``NOVERSION`` has no dots, so it is kept
    whole."""
    mm = ".".join(app_version.split(".")[:2])
    if seq is None:
        return f"{mm}-unknown"
    return f"{mm}-{seq}" + ("+" if dirty else "")


def short_from_canonical(version: str) -> str:
    """Derive the short form ``M.m-seq[+]`` from a canonical version STRING
    (either form; via :func:`parse_canonical_version`). It is the reference a
    port of the short form (Swift, a CLI copy, a client) can be tested against.

    Edge cases (the contract):

    * ``"0.7-63-9151ff8-dirty"`` -> ``"0.7-63+"`` (server, dirty)
    * ``"2.0.1-3-abc1234 (macOS-26.1)"`` -> ``"2.0-3"`` (patch dropped, OS/hash dropped)
    * ``"1.0-767-feat-dirty (2026-11-19T14:29Z)"`` -> ``"1.0-767+"`` (client dirty)
    * ``"2.0"`` (bare tag, no rev) -> ``"2.0-unknown"`` — no sequence to show
    * ``"NOVERSION-46-c7caad6"`` -> ``"NOVERSION-46"`` (no ``M.m`` to cut)
    * ``"NOVERSION-unknown"`` -> ``"NOVERSION-unknown"`` (git unavailable)
    * ``"2.0-unknown"`` -> ``"2.0-unknown"``
    """
    p = parse_canonical_version(version)
    return _short(p.app_version, p.rev_number, p.dirty)


@functools.lru_cache(maxsize=None)
def short_version(
    app_version: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
    *,
    component: Optional[str] = None,
) -> str:
    """The SHORT display form: ``M.m-seq[+]`` — e.g. ``2.0-767`` clean,
    ``2.0-767+`` dirty. ``M.m`` is the app version cut to major.minor,
    ``seq`` is :func:`build_number` (the commit count), and a trailing ``+``
    marks uncommitted changes (same dirty rule as :func:`canonical_version`).
    No branch, hash, or OS. ``app_version`` / ``component`` behave as in
    :func:`canonical_version`. Falls back to ``<M.m>-unknown`` when git is
    unavailable. Cached by args; ``cache_clear()`` forces a re-read."""
    cwd = str(repo_root) if repo_root is not None else None
    if app_version is None:
        app_version = _resolve_app_version(repo_root, component)
    seq = build_number(cwd)
    return _short(app_version, seq, seq is not None and _is_dirty(cwd))


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

    Returns ``None`` if no matching tag exists or git is unavailable. If git
    TIMES OUT (after retries) it raises :class:`GitError` — a stalled lookup is
    not the same answer as "no tags".

    Args:
        repo_root: Path to the git repo. Defaults to the current directory.
        component: Restrict to ``<component>-<X.x>`` tags. ``None`` matches
            bare ``X.x`` tags.
    """
    cwd = str(repo_root) if repo_root is not None else None
    pattern = _version_tag_re(component)
    out = _git(["tag", "--sort=-version:refname"], cwd, strict=True)
    if not out:
        return None
    for line in out.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def latest_versions(repo_root: Optional[Union[str, Path]] = None) -> "dict[Optional[str], str]":
    """Map each component to its highest released version, from the repo's tags.

    Scans every tag, parses ``<component>-<X.x>`` (and bare ``<X.x>``), and
    keeps the highest version per component — the all-components overview that
    :func:`latest_version_tag` gives for one. The bare lineage (tags with no
    component) is keyed by ``None``. Returns an empty dict if there are no
    version tags or git is unavailable.

    Because ``git tag --sort=-version:refname`` orders by version semantics,
    the first tag seen for a component is its highest, so one pass suffices.

    Args:
        repo_root: Path to the git repo. Defaults to the current directory.
    """
    cwd = str(repo_root) if repo_root is not None else None
    out = _git(["tag", "--sort=-version:refname"], cwd, strict=True)
    result: "dict[Optional[str], str]" = {}
    if not out:
        return result
    for line in out.splitlines():
        match = _ANY_VERSION_TAG_RE.match(line.strip())
        if not match:
            continue
        component, version = match.group(1), match.group(2)
        result.setdefault(component, version)  # first seen = highest
    return result


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


def _git_raw(args: list[str], cwd: Optional[str], *, strict: bool = False) -> Optional[str]:
    """Run git; return its stripped stdout (``""`` when it succeeded with no
    output) or ``None`` when git FAILED (not installed, non-zero exit, timeout).
    The distinction matters to callers where empty output means something —
    see :func:`_is_dirty`.

    A TIMEOUT is retried (``_GIT_ATTEMPTS`` times, short backoff); a definite
    failure (non-zero exit, no git binary) is not. With ``strict=True`` a call
    that timed out on every attempt raises :class:`GitError` instead of
    returning ``None``."""
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", cwd]
    cmd += args
    for attempt in range(_GIT_ATTEMPTS):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=_GIT_TIMEOUT_S,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        except subprocess.TimeoutExpired:
            if attempt + 1 < _GIT_ATTEMPTS:
                time.sleep(0.25 * (attempt + 1))
    if strict:
        raise GitError(f"git {' '.join(args)} timed out after {_GIT_ATTEMPTS} attempts")
    return None


def _git(args: list[str], cwd: Optional[str], *, strict: bool = False) -> Optional[str]:
    # Success-with-no-output and failure both collapse to None here; use
    # _git_raw when they must be told apart. strict: see _git_raw.
    return _git_raw(args, cwd, strict=strict) or None


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
    #   * If git FAILS (timeout, error) we cannot know the tree is clean, so it
    #     counts as DIRTY: a build must never claim "clean" when git didn't answer.
    out = _git_raw(
        ["status", "--porcelain", "--untracked-files=no", "--", ".", *_GENERATED_EXCLUDES],
        cwd,
    )
    return out is None or bool(out)
