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
    --list             list every component's latest version (any stage,
                       pre-releases included) and the commit it was cut at: `<component>  <X.x>  <sha>`, one per line
                       (the bare lineage shows as `(bare)`)
    --build-kind       instead print this build's kind: `d` development, `a`
                       alpha, `b` beta, or an empty line for a release
    --short            instead print the short form `M.m-seq[+]` (e.g.
                       `2.0-767`; `+` = dirty)
    --build-number     instead print `git rev-list --count HEAD` (the
                       monotonic integer for Apple CFBundleVersion)
    --increment KIND   print the next tag for a pre-release: alpha, beta, or
                       release to promote the current pre-release: 0.8 -> alpha
                       0.9a1, 0.9a2 -> beta 0.9b1, 0.9b2 -> release 0.9. Refuses
                       illegal moves (e.g. alpha after beta) with exit 3.
                       Pre-release tags are `<X.x>a<N>` / `b<N>`; they sort
                       below release X.x.
    --increment-major  print the next major version (major+1, minor=0) from
                       the latest tag, e.g. 2.3 -> 3.0. For the release script.
    --increment-minor  print the next minor version (minor+1) from the latest
                       tag, e.g. 2.3 -> 2.4. For the release script.

Exit status: 0 ok; 1 not a git repo (--build-number); 2 usage error, or git timed
out on the tag lookup even after retries (error on stderr, NOTHING on stdout, so
a caller like `release.sh` fails instead of getting `NOVERSION`); 3 an illegal
tag request (`--increment` backwards / nothing to promote; `--increment-minor`
over a pre-release) — also nothing on stdout.

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
import time
from typing import List, Optional

# ---------------------------------------------------------------------------
# Inlined from versioning/versioning.py so this file is a fully self-contained
# PEP 723 script: copy it to any machine and run it with no repo / package /
# PATH setup (only runtime need is `git`). KEEP IN SYNC with versioning.py.
# (The `os_version` suffix and current_os() are intentionally omitted — this
# command emits a pure version, not a runtime stamp.)
# ---------------------------------------------------------------------------
_DEFAULT_MAIN_BRANCHES = ("main", "master")
_GIT_TIMEOUT_S = 5.0
_GIT_ATTEMPTS = 3  # timeouts are retried; then a strict lookup raises GitError


class GitError(RuntimeError):
    """git timed out on every retry for a lookup whose "nothing" result would
    be indistinguishable from a real answer (tag lookup -> NOVERSION)."""
NO_VERSION = "NOVERSION"
CANONICAL_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)+)$")
# Build kinds: `<X.x>a<N>` alpha / `b<N>` beta TAGS sort below release X.x; a build
# ahead of a tag gets suffix `d` (development), never a valid tag.
PRE_STAGES = ("a", "b")
PROGRAM_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)+)(?:(a|b)(\d+)|(d))?$")
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


def _git_raw(args: List[str], cwd: Optional[str], strict: bool = False) -> Optional[str]:
    # stdout (possibly "") on success, None on failure. Timeouts are retried;
    # strict=True raises GitError if every attempt timed out.
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


def _git(args: List[str], cwd: Optional[str], strict: bool = False) -> Optional[str]:
    return _git_raw(args, cwd, strict) or None


def _is_dirty(cwd: Optional[str]) -> bool:
    # A git FAILURE counts as dirty — never claim "clean" when git didn't answer.
    out = _git_raw(
        ["status", "--porcelain", "--untracked-files=no", "--", ".", *_GENERATED_EXCLUDES],
        cwd,
    )
    return out is None or bool(out)


def latest_version_tag(cwd: Optional[str], component: Optional[str] = None) -> Optional[str]:
    # Highest canonical-format version (version-sorted), skipping tags that
    # don't match so a stray `v1.0` / `nightly` can't shadow a real release.
    # Returns the bare version (component prefix stripped) for `<component>-X.x`.
    pattern = _version_tag_re(component)
    out = _git(["tag", "--sort=-version:refname"], cwd, strict=True)
    if not out:
        return None
    for line in out.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def _program_tag_re(component: Optional[str]):
    body = r"(\d+(?:\.\d+)+(?:(?:a|b)\d+)?)"
    if not component:
        return re.compile(r"^" + body + r"$")
    return re.compile(r"^" + re.escape(component) + r"-" + body + r"$")


def split_program_version(version: str):
    m = PROGRAM_VERSION_RE.match(version)
    if not m:
        return version, None, None
    return m.group(1), m.group(2) or m.group(4), (int(m.group(3)) if m.group(3) else None)


def build_kind(app_version: str) -> str:
    return (split_program_version(app_version)[1] or "") if PROGRAM_VERSION_RE.match(app_version) else ""


def _stage_suffix(stage, n) -> str:
    return f"{stage}{n if n is not None else ''}" if stage else ""


def _program_key(version: str):
    base, stage, n = split_program_version(version)
    rank = (-1 if stage == "d" else PRE_STAGES.index(stage)) if stage else len(PRE_STAGES)
    return (tuple(int(x) for x in base.split(".")), rank, n or 0)


def _short_app(app_version: str) -> str:
    if not PROGRAM_VERSION_RE.match(app_version):
        return app_version
    base, stage, n = split_program_version(app_version)
    mm = ".".join(base.split(".")[:2])
    return mm + _stage_suffix(stage, n)


def latest_program_tag(cwd: Optional[str], component: Optional[str] = None,
                       merged: bool = False) -> Optional[str]:
    # Highest version of ANY stage (release or pre-release), compared in Python
    # (git's version sort would put 0.9 below 0.9b1). Mirrors versioning.py.
    # merged=True: only tags reachable from HEAD (what a BUILD's version uses);
    # the default (all tags) picks the NEXT tag number so none is issued twice.
    pattern = _program_tag_re(component)
    out = _git(["tag", *(["--merged", "HEAD"] if merged else [])], cwd, strict=True)
    if not out:
        return None
    found = [m.group(1) for line in out.splitlines()
             if (m := pattern.match(line.strip()))]
    return max(found, key=_program_key) if found else None


def _next_minor(version: str) -> str:
    major, minor = (int(n) for n in version.split(".")[:2])
    return f"{major}.{minor + 1}"


def _resolve_app_version(cwd: Optional[str], component: Optional[str]) -> str:
    # Latest tag of any stage, or — once HEAD is ahead of it — a DEVELOPMENT
    # build (suffix d) of the release in flight: 0.8 -> 0.9d, 0.9b2 -> 0.9d.
    # Mirrors versioning.py's _resolve_app_version.
    tag = latest_program_tag(cwd, component, merged=True)
    if tag is None:
        return NO_VERSION
    ref = f"refs/tags/{component}-{tag}" if component else f"refs/tags/{tag}"
    since = _git(["rev-list", "--count", f"{ref}..HEAD"], cwd, strict=True)
    if since is not None and since.isdigit() and int(since) > 0:
        base, stage, _ = split_program_version(tag)
        # Ahead of a tag = a DEVELOPMENT build (suffix d) of the release in flight.
        return f"{base}d" if stage else f"{_next_minor(tag)}d"
    return tag


def canonical_version(cwd: Optional[str], component: Optional[str] = None) -> str:
    app_version = _resolve_app_version(cwd, component)
    commit = _git(["rev-parse", "--short", "HEAD"], cwd)
    if commit is None:
        return f"{app_version}-unknown"
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _is_dirty(cwd)
    rev = build_number(cwd)
    parts = [app_version]
    if rev is not None:
        parts.append(str(rev))
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


def short_version(cwd: Optional[str], component: Optional[str] = None) -> str:
    mm = _short_app(_resolve_app_version(cwd, component))
    seq = build_number(cwd)
    if seq is None:
        return f"{mm}-unknown"
    return f"{mm}-{seq}" + ("+" if _is_dirty(cwd) else "")


def latest_versions(cwd: Optional[str]):
    # Highest released version per component (None key = bare lineage). Sorted
    # by version, so the first tag seen for a component is its highest.
    out = _git(["tag", "--sort=-version:refname"], cwd, strict=True)
    result = {}
    if not out:
        return result
    for line in out.splitlines():
        match = _ANY_VERSION_TAG_RE.match(line.strip())
        if match:
            result.setdefault(match.group(1), match.group(2))
    return result
_ANY_PROGRAM_TAG_RE = re.compile(r"^(?:(.+)-)?(\d+(?:\.\d+)+(?:(?:a|b)\d+)?)$")


def latest_programs(cwd: Optional[str]):
    # Highest version of ANY stage per component (None key = bare lineage).
    out = _git(["tag"], cwd, strict=True)
    result = {}
    if not out:
        return result
    for line in out.splitlines():
        m = _ANY_PROGRAM_TAG_RE.match(line.strip())
        if not m:
            continue
        comp, version = m.group(1), m.group(2)
        if comp not in result or _program_key(version) > _program_key(result[comp]):
            result[comp] = version
    return result
# --------------------------- end inlined block -----------------------------


def next_version(cwd: Optional[str], part: str, component: Optional[str] = None) -> str:
    """Next RELEASE tag for ``part`` major/minor, from the latest tag of any
    stage. ``minor`` while a pre-release is latest raises ValueError (promote it
    with ``--increment release``); ``major`` is allowed. No tag bootstraps from
    ``0.0``. Mirrors versioning.py."""
    latest = latest_program_tag(cwd, component)
    pre = f"{component}-" if component else ""
    if latest is None:
        major, minor = 0, 0
    else:
        base, stage, _ = split_program_version(latest)
        if stage and part == "minor":
            raise ValueError(
                f"{pre}{latest} is a pre-release; use `release` to promote it "
                f"(a minor bump would skip it)")
        major, minor = (int(n) for n in base.split(".")[:2])
    return pre + (f"{major + 1}.0" if part == "major" else f"{major}.{minor + 1}")


_KINDS = {"alpha": "a", "beta": "b"}


def next_program_version(cwd: Optional[str], kind: str, component: Optional[str] = None) -> str:
    """Next tag for advancing a program, for the RELEASE SCRIPT. ``kind`` is
    ``alpha`` / ``beta`` / ``release``; raises ``ValueError`` on an
    illegal move. From the latest tag of any stage:

    * release ``0.8``: alpha/beta start the next minor (``0.9a1`` / ``0.9b1``);
      ``release`` is refused (nothing to promote).
    * pre-release ``0.9a2``: same kind -> ``0.9a3``; a LATER stage starts it
      (``beta`` -> ``0.9b1``); ``release`` promotes it to ``0.9``; an EARLIER
      stage is refused (no going backwards).
    * no tag: bootstraps from ``0.0`` (``alpha`` -> ``0.1a1``); ``release``
      is refused.

    Returned bare (or ``<component>-…``), directly taggable."""
    if kind != "release" and kind not in _KINDS:
        raise ValueError(f"unknown build kind {kind!r} (alpha|beta|release)")
    latest = latest_program_tag(cwd, component)
    pre = "" if component is None else f"{component}-"
    if latest is None:
        if kind == "release":
            raise ValueError("no tag to promote to a release")
        return f"{pre}0.1{_KINDS[kind]}1"
    base, stage, n = split_program_version(latest)
    if stage is None:  # latest is a release
        if kind == "release":
            raise ValueError(f"{pre}{latest} is already a release; nothing to promote")
        return f"{pre}{_next_minor(latest)}{_KINDS[kind]}1"
    if kind == "release":
        return f"{pre}{base}"
    want = _KINDS[kind]
    if PRE_STAGES.index(want) < PRE_STAGES.index(stage):
        raise ValueError(f"cannot go back from {pre}{latest} to {kind}")
    return f"{pre}{base}{want}{n + 1 if want == stage else 1}"


def _describe(cwd: Optional[str], component: Optional[str], stream) -> None:
    """Print the version derivation to `stream` (for --verbose); stdout stays clean."""
    tag = latest_program_tag(cwd, component, merged=True)
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


#: Exit statuses: 0 ok; 1 not a git repo (--build-number); 2 usage error or git
#: timed out (no version printed); 3 an illegal tag request (see --increment).
EXIT_ILLEGAL_MOVE = 3


def main() -> int:
    try:
        return _main()
    except GitError as e:
        print(f"error: {e}; refusing to report a version", file=sys.stderr)
        return 2


def _main() -> int:
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
                      help="List every component's latest version, pre-releases included "
                           "(one per line); the bare lineage shows as '(bare)'.")
    mode.add_argument("--build-kind", action="store_true",
                      help="Print this build's kind letter: d (development), a "
                           "(alpha), b (beta), or an empty line for a release.")
    mode.add_argument("--short", action="store_true",
                      help="Print the short form M.m-seq[+] (+ = dirty).")
    mode.add_argument("--build-number", action="store_true",
                      help="Instead print `git rev-list --count HEAD` "
                           "(the monotonic integer for Apple CFBundleVersion).")
    mode.add_argument("--increment", choices=["alpha", "beta", "release"],
                      metavar="KIND",
                      help="Print the next tag for a pre-release: alpha, beta, or "
                           "release (promote the current pre-release). E.g. 0.8 -> "
                           "alpha 0.9a1; 0.9a2 -> beta 0.9b1; 0.9b2 -> release 0.9. "
                           "Refuses illegal moves (exit 3).")
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
        versions = latest_programs(cwd)  # any stage: matches what builds use
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

    if args.build_kind:
        print(build_kind(_resolve_app_version(cwd, component)))
        return 0

    if args.short:
        print(short_version(cwd, component))
        return 0

    if args.build_number:
        n = build_number(cwd)
        if n is None:
            print("error: not a git repo, or git unavailable", file=sys.stderr)
            return 1
        print(n)
        return 0

    if args.increment:
        try:
            print(next_program_version(cwd, args.increment, component))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ILLEGAL_MOVE
        return 0

    if args.increment_major or args.increment_minor:
        try:
            print(next_version(cwd, "major" if args.increment_major else "minor", component))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ILLEGAL_MOVE
        return 0

    print(canonical_version(cwd, component))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
