# versioning

Git-derived app versioning. Part of [pasker-net](../README.md). Paths are
relative to the pasker-net checkout (or `vendor/pasker-net/` in a consumer).
Stdlib only; the CLI is a single self-contained PEP 723 file.

A git **tag** is the source of truth for the version number. The library *reads*
the newest tag reachable from `HEAD` and stamps it with live git state; the CLI
and `release.sh` *compute* the next tag. Between releases a build reads as
"development build of the release in flight".

## What a version looks like

Every build has a **build kind**, written as a suffix on the version, derived
from the newest tag reachable from `HEAD`:

| kind | meaning | version | short | server canonical |
|---|---|---|---|---|
| blank | release (`HEAD` is on tag `0.9`) | `0.9` | `0.9-6` | `0.9-6-23e668c` |
| `a` | alpha (`HEAD` is on tag `0.9a1`) | `0.9a1` | `0.9a1-4` | `0.9a1-4-64edc5d` |
| `b` | beta (`HEAD` is on tag `0.9b1`) | `0.9b1` | `0.9b1-5` | `0.9b1-5-c0cd419` |
| `d` | development: any commit **after** a tag | `0.10d` | `0.10d-7` | `0.10d-7-3115697` |

- A development build is a build of the **release in flight**: the next minor
  after a release tag (`0.9` → `0.10d`), the same release after an alpha/beta tag
  (`0.9b1` → `0.9d`). It can never be mistaken for a production `0.9`.
- A dirty tree keeps the tag's kind and gains a `+` (short) / `-dirty` (long):
  `0.9b1-5+`. The kind says what was tagged; the `+` says the tree isn't it.
- No tags at all → `NOVERSION` (e.g. `NOVERSION-2`), no kind.
- Only tags **reachable from `HEAD`** count. A tag on an unmerged branch, or on a
  commit after `HEAD` (an older checkout), never shapes this build's version.
- What a kind *means to an app* (update channel, audience, badge) is the app's
  business; this library only says what kind of build it is.

`seq` is `git rev-list --count HEAD` — a monotonic integer, the same number an
Apple `CFBundleVersion` / `CURRENT_PROJECT_VERSION` carries.

## Python API

```python
from versioning import (canonical_version, canonical_version_client, short_version,
                        build_kind, build_number, parse_canonical_version,
                        short_from_canonical, current_os)

canonical_version(repo_root=ROOT)                 # server:  "0.10d-7-3115697[-dirty]"
canonical_version(repo_root=ROOT, os_version=current_os())
                                                  #          "0.10d-7-3115697 (macOS-27.2)"
canonical_version_client(repo_root=ROOT)          # client:  "0.10d-7[-dirty (<time>)]" — no hash
short_version(repo_root=ROOT)                     # short:   "0.10d-7[+]"
build_number(ROOT)                                # 7
```

- **Server form** `<version>-<seq>[-<branch>]-<commit>[-dirty][ (<os>)]` keeps the
  commit hash; the branch shows on any non-trunk branch (`main`/`master` omit it).
- **Client / Apple form** `<version>-<seq>[-<branch>][-dirty (<buildtime>)][ (<os>)]`
  has no hash; the branch shows only when dirty, and a dirty build stamps a
  Zulu build time in place of the missing commit.
- **Short form** `M.m-seq[+]` — major.minor, sequence, `+` if dirty. A patch
  component is dropped (`1.3.4` → `1.3`); the kind is kept (`0.9b1-5`, `0.10d-7`).
  `short_from_canonical("0.10d-7-3115697-dirty") == "0.10d-7+"` derives it from
  either canonical string — the reference every port (Swift, JS, …) is tested
  against. A bare tag with no seq is `2.0-unknown`; `NOVERSION-unknown` stays.
- **Parsing.** `parse_canonical_version(s)` returns a `VersionParts`
  (`app_version`, `branch`, `rev_number`, `commit`, `dirty`, `build_time`,
  `os_version`) plus derived `release_version`, `stage`, `pre_number` and
  `build_kind` (`"d"`, `"a"`, `"b"`, `""`). `.as_dict()` includes the derived
  fields.
- **Component lineages.** `component="server"` (CLI `--component server`) reads
  only `server-<X.x>` tags, so `server` and `client` release independently and
  never shadow each other. The version string stays version-only.
- **Explicit base.** `canonical_version("2.1", ...)` skips the tags; kinds are
  then up to you.
- **Lookups.** `latest_version_tag()` / `latest_versions()` stay **release-only**;
  `latest_program_tag()` / `latest_programs()` include alpha/beta tags
  (`merged=True` limits to tags reachable from `HEAD`, which is what builds use).
- **Cached.** `canonical_version*` / `short_version` memoize by their arguments;
  call `.cache_clear()` if git state changes mid-process.
- **Reads git at runtime** (`.git` must be present). For a deployed binary with
  no git, or an Apple app, bake the value at build time (see Swift below).

## Failure behaviour

A version must not claim more than git told it.

- **Dirty:** if `git status` fails or times out, the tree counts as **dirty**,
  never clean.
- **Timeouts are retried** (3 attempts, 5 s each). If the tag lookup still can't
  be answered it raises `versioning.GitError` — and the CLI exits 2 with nothing
  on stdout — instead of reporting `NOVERSION`.
- **Not a repo / no git binary:** the documented fallback, `<version>-unknown`
  (`NOVERSION-unknown`).

## Command line

`versioning/__main__.py` is a self-contained PEP 723 script (uv-run shebang,
stdlib only). Invoke it whichever way suits the box:

```bash
# vendored submodule — package form needs it importable:
PYTHONPATH=vendor/pasker-net python3 -m versioning --component server

# standalone PEP 723 file — no PYTHONPATH, no install (uv runs it):
./vendor/pasker-net/versioning/__main__.py --component server

# symlinked onto PATH:
versioning --component server
```

| flag | prints |
|---|---|
| *(none)* | the canonical (server) version, e.g. `0.10d-7-3115697-dirty` |
| `--short` | the short form `M.m-seq[+]` |
| `--build-kind` | `d`, `a`, `b`, or an empty line for a release |
| `--build-number` | `git rev-list --count HEAD` |
| `--list` | every component's latest tag of any stage and its commit |
| `--increment alpha\|beta\|release` | the next pre-release tag (see below) |
| `--increment-major` / `--increment-minor` | the next release tag: `2.3` → `3.0` / `2.4` |
| `-v` | the derivation (component, base tag, branch, commit, dirty) on stderr |

Options: `--repo-root PATH` (default: cwd), `--component NAME`.

Exit status: `0` ok · `1` not a git repo (`--build-number`) · `2` usage error, or
git timed out on the tag lookup (nothing on stdout) · `3` an illegal tag request
(nothing on stdout).

## Cutting a release

Land the change on `main` first, then run `./release.sh` from the repo root. It
asks the CLI for the next tag, tags `HEAD`, and pushes the tag:

```bash
./release.sh <component> [major|minor|alpha|beta|release]   # default: minor
```

| from the latest tag | command | tags |
|---|---|---|
| `0.8` (release) | `alpha` | `0.9a1` |
| `0.9a1` | `alpha` | `0.9a2` |
| `0.9a2` | `beta` | `0.9b1` |
| `0.8` (release) | `beta` | `0.9b1` (skipping alpha is fine) |
| `0.9a2` | `release` | `0.9` (straight from alpha is fine; no beta needed) |
| `0.9b1` | `release` | `0.9` (promotes it) |
| `0.9` (release) | `minor` / `major` | `0.10` / `1.0` |
| *(no tags)* | `alpha` / `minor` / `major` | `0.1a1` / `0.1` / `1.0` |

With `--component`/`release.sh <component>` every tag is prefixed
(`server-0.9a1`). Illegal moves are refused with exit 3 and tag nothing:

- going backwards (`alpha` after `0.9b1`),
- `release` when the latest tag is already a release (nothing to promote),
- `minor` while a pre-release is the latest tag (use `release` to promote it;
  `major` is allowed as a deliberate jump).

Tags are `<X.x>a<N>` and `<X.x>b<N>` (`rc`, and anything hyphenated like
`2.0-rc1`, are not tag kinds and are ignored). A pre-release sorts **below** the
release it leads to. Numbers are computed from **all** tags (so none is issued
twice), while builds read only tags reachable from `HEAD`. Any pre-release can be promoted
by `release`, alpha included. A stage never
advances by itself — alpha → beta → release is always a deliberate tag.

## Lifecycle

A repo using the `server` lineage, with real output (`--short` unless shown):

```text
$ versioning --component server --short            # fresh repo, no tags
NOVERSION-2
$ git tag -a server-0.8 -m 0.8                     # a first release
$ versioning --component server --short
0.8-2
  ... 2 commits land ...
$ versioning --component server --short            # development build of 0.9
0.9d-4
$ ./release.sh server alpha                        # -> server-0.9a1
$ versioning --component server --short
0.9a1-4
  ... a commit lands ...
$ versioning --component server --short
0.9d-5
$ ./release.sh server beta                         # -> server-0.9b1
$ versioning --component server --short
0.9b1-5
  ... uncommitted edit ...
$ versioning --component server --short            # tag's kind kept, flagged dirty
0.9b1-5+
$ versioning --component server --increment alpha  # can't go back
error: cannot go back from server-0.9b1 to alpha      [exit 3]
$ ./release.sh server release                      # promote -> server-0.9
$ versioning --component server --short
0.9-6
  ... a commit lands ...
$ versioning --component server --short            # next release in flight
0.10d-7
$ versioning --component server --build-kind
d
$ versioning --list
server  0.9  23e668c
```

## Quick start

### In code — for display

Point `repo_root` at **your app's repo** (the repo whose tags you cut — in a
vendored setup that's the *consumer's* repo, not the `pasker-net` submodule):

```python
from pathlib import Path
from versioning import (canonical_version, short_version, parse_canonical_version,
                        current_os)

REPO_ROOT = Path(__file__).resolve().parent  # your app's git root

VERSION = canonical_version(repo_root=REPO_ROOT, component="server",
                            os_version=current_os())
#   on a development build:   "0.10d-7-3115697 (macOS-27.2)"
#   dirty:                    "0.10d-7-3115697-dirty (macOS-27.2)"
#   on a non-trunk branch:    "0.10d-8-feature-6c6f6b2 (macOS-27.2)"
#   on tag server-0.9:        "0.9-6-23e668c (macOS-27.2)"
#   no tags:                  "NOVERSION-2-5a03450 (macOS-27.2)"

SHORT = short_version(repo_root=REPO_ROOT, component="server")   # "0.10d-7"

# Tell a production build from a development one without string-matching:
kind = parse_canonical_version(VERSION).build_kind   # "d" | "a" | "b" | "" (release)
IS_PRODUCTION = kind == ""
```

Show `VERSION` in `--version` output, an about box, a `/health` endpoint, or
logs.

### In scripts — for cutting a release

Prefer `./release.sh`. To drive the CLI yourself, it prints a bare,
directly-taggable string (no commit/branch/OS) and exits non-zero on a refusal,
so `set -e` stops before tagging:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
COMPONENT="server"          # drop --component below for a single-component repo

NEXT="$(python3 -m versioning --repo-root "$REPO_ROOT" --component "$COMPONENT" --increment beta)"

echo "cutting $NEXT"
git tag -a "$NEXT" -m "release $NEXT"
git push origin "$NEXT"
```

For an Apple `CFBundleVersion` / Xcode `CURRENT_PROJECT_VERSION`, the sequence is
separate from the marketing tag:

```bash
versioning --build-number   # -> e.g. 412  (git rev-list --count HEAD)
```

## Swift (`emit_swift.py`)

Generates a self-contained `CanonicalVersion` Swift enum from an Xcode Run Script
phase (mark the output path as the phase's output and set `alwaysOutOfDate = 1`):

```bash
python3 "${SRCROOT}/../../vendor/pasker-net/versioning/emit_swift.py" \
    --repo-root "${SRCROOT}/../.." \
    --out "${SRCROOT}/MyApp/CanonicalVersion.swift"      # [--component NAME]
```

Baked at build time: `gitBranch`, `gitCommit`, `gitDirty`, `revNumber`,
`buildTime` (dirty builds only), **`buildKind`** (`"d"`/`"a"`/`"b"`/`""`),
**`buildSuffix`** (kind + tag number: `""`, `"d"`, `"a1"`, `"b3"`) and
**`tagAppVersion`** (what the tags imply, e.g. `"0.10d"`). The marketing version
still comes from the plist's `CFBundleShortVersionString` at runtime — Apple
requires it to be digits and dots only, which is why the kind is baked
separately. `build` renders `"<plist version><buildSuffix>-<revNumber>"`, e.g.
`0.10d-71` (a dirty build appends `-[<branch>-]dirty (<buildTime>)`), and `full`
appends ` (<os>)`. `appVersionMatchesTags` is `false` when the plist's
`MARKETING_VERSION` has drifted from the tags. A stalled tag lookup fails the
emit loudly instead of baking `NOVERSION`. Compare `buildKind` — don't parse the
suffix — to tell a development build from a production one.
