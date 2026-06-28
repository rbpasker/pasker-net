#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""CLI: print the git-derived canonical version string.

The base version is the repo's latest version tag (``git tag
--sort=-version:refname``, highest first). The canonical string composes
that base with the local git state:

    <latest-tag>[-<branch>]-<commit>[-dirty]

- ``branch`` is omitted on a trunk branch (``main``/``master``) or detached HEAD
- ``commit`` is the short HEAD hash
- ``dirty`` is appended when tracked source has uncommitted changes

The base falls back to ``NOVERSION`` when the repo has no tags, or when the
latest tag isn't in canonical version format (``X.x`` marketing form, e.g.
``2.0`` / ``1.3.4``). Outside a git repo the result is ``<base>-unknown``.

This file is a self-contained PEP 723 script — copy it anywhere and run it
with no repo / package / PATH setup (its only runtime need is ``git``):

    versioning                       # if symlinked onto PATH (uv run shebang)
    ./versioning/__main__.py         # uv run shebang (needs uv)
    python3 versioning/__main__.py   # any Python 3.9+, no uv
    PYTHONPATH=vendor/pasker-net python3 -m versioning   # package invocation

Options:
    --repo-root PATH   git repo to read (default: cwd)
    --component NAME   scope to a component's tags: `<NAME>-<X.x>` (e.g.
                       server-2.1), so sibling components (server / client)
                       don't shadow each other. With no --component, bare
                       `X.x` tags are used.
    -v, --verbose      print the derivation (component, base tag, branch,
                       commit, dirty) to stderr; stdout stays the bare result.
  mutually exclusive (default prints the canonical version):
    --list             list every component's latest version and the commit
                       it was cut at: `<component>  <X.x>  <sha>`, one per line
                       (the bare lineage shows as `(bare)`)
    --build-number     instead print `git rev-list --count HEAD` (the
                       monotonic integer for Apple CFBundleVersion)
    --increment-major  print the next major version (major+1, minor=0) from
                       the latest tag, e.g. 2.3 -> 3.0. For the release script.
    --increment-minor  print the next minor version (minor+1) from the latest
                       tag, e.g. 2.3 -> 2.4. For the release script.

`--increment-*` emit a bare, directly-taggable string for the release script
(no commit/branch/dirty/OS): `3.0`, or `<component>-3.0` with --component. With
no matching tag they bootstrap from 0.0, so a first major bump is 1.0 (or
`<component>-1.0`) and a first minor bump is 0.1.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import List, Optional

# ---------------------------------------------------------------------------
# Inlined from versioning/versioning.py so this file is a fully self-contained
# PEP 723 script: copy it to any machine and run it with no repo / package /
# PATH setup (only runtime need is `git`). KEEP IN SYNC with versioning.py.
# (The `os_version` suffix and current_os() are intentionally omitted — this
# command emits a pure version, not a runtime stamp.)
# ---------------------------------------------------------------------------
_DEFAULT_MAIN_BRANCHES = ("main", "master")
_GIT_TIMEOUT_S = 2.0
NO_VERSION = "NOVERSION"
CANONICAL_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)+)$")
# Parses any version tag into (component, version); component is None for bare.
_ANY_VERSION_TAG_RE = re.compile(r"^(?:(.+)-)?(\d+(?:\.\d+)+)$")

# Generated artifacts of this versioning system; excluded from the dirty check
# (the build rewrites them before the check runs). See versioning.py.
_GENERATED_EXCLUDES = (
    ":(exclude,glob)**/CanonicalVersion.swift",
    ":(exclude,glob)**/BuildStamp.swift",
)


def _version_tag_re(component: Optional[str]):
    # Group 1 = bare version. With a component the tag is `<component>-<X.x>`
    # (git tag names can't contain spaces; a hyphen joins them) so e.g.
    # `server-2.1` and `client-2.1` keep separate lineages.
    if not component:
        return CANONICAL_VERSION_RE
    return re.compile(r"^" + re.escape(component) + r"-(\d+(?:\.\d+)+)$")


def _git(args: List[str], cwd: Optional[str]) -> Optional[str]:
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
    return bool(_git(
        ["status", "--porcelain", "--untracked-files=no", "--", ".", *_GENERATED_EXCLUDES],
        cwd,
    ))


def latest_version_tag(cwd: Optional[str], component: Optional[str] = None) -> Optional[str]:
    # Highest canonical-format version (version-sorted), skipping tags that
    # don't match so a stray `v1.0` / `nightly` can't shadow a real release.
    # Returns the bare version (component prefix stripped) for `<component>-X.x`.
    pattern = _version_tag_re(component)
    out = _git(["tag", "--sort=-version:refname"], cwd)
    if not out:
        return None
    for line in out.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def canonical_version(cwd: Optional[str], component: Optional[str] = None) -> str:
    app_version = latest_version_tag(cwd, component) or NO_VERSION
    commit = _git(["rev-parse", "--short", "HEAD"], cwd)
    if commit is None:
        return f"{app_version}-unknown"
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _is_dirty(cwd)
    parts = [app_version]
    if branch and branch != "HEAD" and branch not in _DEFAULT_MAIN_BRANCHES:
        parts.append(branch)
    parts.append(commit)
    if dirty:
        parts.append("dirty")
    return "-".join(parts)


def build_number(cwd: Optional[str]) -> Optional[int]:
    count = _git(["rev-list", "--count", "HEAD"], cwd)
    if count is None:
        return None
    try:
        return int(count)
    except ValueError:
        return None


def latest_versions(cwd: Optional[str]):
    # Highest released version per component (None key = bare lineage). Sorted
    # by version, so the first tag seen for a component is its highest.
    out = _git(["tag", "--sort=-version:refname"], cwd)
    result = {}
    if not out:
        return result
    for line in out.splitlines():
        match = _ANY_VERSION_TAG_RE.match(line.strip())
        if match:
            result.setdefault(match.group(1), match.group(2))
    return result
# --------------------------- end inlined block -----------------------------


def next_version(cwd: Optional[str], part: str, component: Optional[str] = None) -> str:
    """Next tag value derived from the latest tag, for the RELEASE SCRIPT.

    ``part="major"`` bumps major and resets minor to 0 (``2.3`` -> ``3.0``);
    ``part="minor"`` bumps minor (``2.3`` -> ``2.4``). Extra components are
    dropped (``1.3.4`` -> ``2.0`` / ``1.4``). With no canonical tag the base
    is ``0.0``, so the first bump is ``1.0`` (major) or ``0.1`` (minor).

    The result is the bare, directly-taggable string: ``3.0`` with no
    component, or ``<component>-3.0`` when ``component`` is set (so the
    release script can ``git tag`` it as-is). It carries no commit / branch /
    dirty / OS — it's a tag value, not a runtime version string.
    """
    base = latest_version_tag(cwd, component)
    if base:
        major, minor = (int(n) for n in base.split(".")[:2])
    else:
        major, minor = 0, 0
    version = f"{major + 1}.0" if part == "major" else f"{major}.{minor + 1}"
    return f"{component}-{version}" if component else version


def _describe(cwd: Optional[str], component: Optional[str], stream) -> None:
    """Print the version derivation to `stream` (for --verbose); stdout stays clean."""
    tag = latest_version_tag(cwd, component)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    commit = _git(["rev-parse", "--short", "HEAD"], cwd)
    on_trunk = branch in _DEFAULT_MAIN_BRANCHES or branch in (None, "HEAD")
    base = (f"{tag} (from tag {component + '-' if component else ''}{tag})"
            if tag else f"{NO_VERSION} (no canonical-format tag)")
    print("versioning:", file=stream)
    print(f"  component: {component or '<none>'}", file=stream)
    print(f"  base:      {base}", file=stream)
    print(f"  branch:    {branch or '<unknown>'}"
          f"{' (omitted — trunk)' if on_trunk else ''}", file=stream)
    print(f"  commit:    {commit or '<unknown>'}", file=stream)
    print(f"  dirty:     {'yes' if _is_dirty(cwd) else 'no'}", file=stream)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="versioning",
        description="Print the git-derived canonical version "
                    "(base = latest version tag, else NOVERSION).",
    )
    ap.add_argument("--repo-root", default=None,
                    help="Path to the git repo. Defaults to the current directory.")
    ap.add_argument("--component", default=None, metavar="NAME",
                    help="Scope to a component's tags (`<NAME>-<X.x>`, e.g. "
                         "server-2.1), so sibling components don't shadow each "
                         "other. Increments emit the component-prefixed tag.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print the derivation (component, base tag, branch, "
                         "commit, dirty) to stderr; stdout stays the bare result.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="List every component's latest version (one per line); "
                           "the bare lineage shows as '(bare)'.")
    mode.add_argument("--build-number", action="store_true",
                      help="Instead print `git rev-list --count HEAD` "
                           "(the monotonic integer for Apple CFBundleVersion).")
    mode.add_argument("--increment-major", action="store_true",
                      help="Print the next major version (major+1, minor=0) from "
                           "the latest tag, e.g. 2.3 -> 3.0. For the release script.")
    mode.add_argument("--increment-minor", action="store_true",
                      help="Print the next minor version (minor+1) from the latest "
                           "tag, e.g. 2.3 -> 2.4. For the release script.")
    args = ap.parse_args()

    cwd = args.repo_root
    component = args.component

    if args.verbose:
        _describe(cwd, component, sys.stderr)

    if args.list:
        versions = latest_versions(cwd)
        if not versions:
            print("(no version tags)")
            return 0

        def _sha(comp: Optional[str], version: str) -> str:
            tag = f"{comp}-{version}" if comp else version
            return _git(["rev-parse", "--short", f"{tag}^{{commit}}"], cwd) or "?"

        named = sorted(c for c in versions if c is not None)
        labels = named + ([None] if None in versions else [])
        name_w = max((len(c) for c in named), default=6)
        name_w = max(name_w, len("(bare)") if None in versions else 0)
        ver_w = max(len(v) for v in versions.values())
        for comp in labels:
            label = comp if comp is not None else "(bare)"
            print(f"{label:<{name_w}}  {versions[comp]:<{ver_w}}  {_sha(comp, versions[comp])}")
        return 0

    if args.build_number:
        n = build_number(cwd)
        if n is None:
            print("error: not a git repo, or git unavailable", file=sys.stderr)
            return 1
        print(n)
        return 0

    if args.increment_major:
        print(next_version(cwd, "major", component))
        return 0

    if args.increment_minor:
        print(next_version(cwd, "minor", component))
        return 0

    print(canonical_version(cwd, component))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
