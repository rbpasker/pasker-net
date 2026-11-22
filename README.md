# pasker-net

Shared utility modules for personal projects.

A note from the human: You can call this "vibe coded," but the reality
is that I designed, code reviewed, and tested everyting in this
myself, and it is all running live in production in a number of
personal projects. Each module can be used independently (except
qworkers). PRs welcome. --Bob at y0u kn0w wh3r3


## Modules

- [`secretvault/`](secretvault/) — macOS Keychain-backed secret storage with
  env-var fallback. Pluggable backends, namespace-per-app via Keychain service
  name. Ships a self-contained CLI (`secretvault/__main__.py`) that runs as a
  PEP 723 script and can `export`/`import` a service's secrets to move them
  between machines. Adapted from roscoe-mcp.
- [`versioning/`](versioning/) — git-derived app versioning. Surfaces:
  - `canonical_version("2.0", repo_root=..., os_version=current_os())`
    → `2.0[-<branch>]-<commit>[-dirty][ (<os>)]` (server/runtime stamp; branch
    omitted on `main`/`master`; `<version>-unknown` outside a repo). Omit
    `app_version` to drive the base off the repo's **latest version tag**
    (`latest_version_tag()`), so releases are cut with `git tag` — only the
    `X.x` marketing form (`2.0`, `1.3.4`) counts, and a repo with no such tag
    yields `NOVERSION`.
  - `build_number(repo_root=...)` → `git rev-list --count HEAD`, a monotonic
    integer for an Apple `CFBundleVersion` / Xcode `CURRENT_PROJECT_VERSION`.
  - `versioning/__main__.py` is a self-contained PEP 723 script (uv-run
    shebang, stdlib only): `versioning` prints the tag-derived canonical
    version; `--build-number` prints the integer instead. For the release
    script, `--increment-major` / `--increment-minor` print the next bare
    `X.x` version (e.g. `2.3` → `3.0` / `2.4`) to `git tag` with — no
    commit/OS suffix; with no tag yet they bootstrap from `0.0`.
  - `--component NAME` (also `canonical_version(..., component=)` /
    `latest_version_tag(repo, component)`) scopes tags to `<NAME>-<X.x>`
    (e.g. `server-2.1`), giving each component its own lineage so siblings
    can't shadow each other. Increments then emit the prefixed tag
    (`server-3.0`); the canonical string stays version-only (`2.1-<commit>`).
  - `short_version()` → `M.m-seq[+]` (e.g. `0.9b3-70+`; `+` = dirty), and
    `short_from_canonical(str)` derives it from either canonical form — the
    reference every port is tested against. `python3 -m versioning --short`.
  - **Build kinds.** Every build is `d` (development), `a` (alpha), `b` (beta)
    or blank (release), written as a suffix on the version and derived from the
    newest tag *reachable* from HEAD: `0.9-70` release, `0.9a1-70`, `0.9b2-70`,
    `0.10d-71` development (any build ahead of a tag; the release in flight, so
    `0.9d` after beta `0.9b2` too). A dirty tree on a tagged commit keeps the
    tag's kind (the `+` flags it). `build_kind()` / `VersionParts.build_kind` /
    `--build-kind` give the letter; what a kind means to an app (update channel,
    audience) is the app's business. Alpha/beta tags are `<X.x>a<N>` / `b<N>`
    and sort below the release. Advance with `release.sh <component>
    alpha|beta|release` (or `--increment KIND`), which refuses illegal moves
    (exit 3). A git timeout is retried, then fails loudly (exit 2) rather than
    reporting `NOVERSION`. `latest_version_tag()` stays release-only;
    `latest_program_tag()` / `latest_programs()` include pre-releases.
  Ships `emit_swift.py` to generate a `CanonicalVersion` Swift enum from an
  Xcode Run Script Phase. The Swift `build` getter renders `X.x[<stage>]-<n>`
  (marketing `X.x` from the plist + the baked build-kind `buildSuffix` +
  rev count `n`), so the in-app stamp matches Apple's `X.x (n)`; a dirty
  tree appends `-[<branch>-]dirty(<buildtime>)`. Stdlib only.
- [`queuing/`](queuing/) — in-memory named FIFO queues backed by a
  projection of the consumer's durable state. The library holds no SQL
  of its own; the consumer's audit tables are the source of truth and
  `populate()` re-derives the in-memory cache from a projection
  callable at boot or as a self-heal sweep. Per-queue `stats` and a
  `snapshot()` (depth, enq/deq, wait avg/min/max, plus stage-added
  counters) feed dashboards; `QueuePriority` adds a static-priority gate
  over a queue group. Multi-thread native (single-process); the small
  `Queue` interface ports cleanly to a real broker when multi-process is
  needed. Stdlib only.
- [`qworkers/`](qworkers/) — thread workers that loop on
  `queuing.Queue` instances, one daemon thread per `(qname, process)`
  pair. Blocks in `Queue.dequeue(timeout=...)` when idle (no polling),
  supports worker pools by repeating pairs, and provides clean
  start/stop/join lifecycle plus a context manager. An `observer` hook
  reports every enq/deq/drop event and per-worker `WorkerStats`
  (throughput, service time, live busy flag) complement the queue stats.
  Stdlib only.

## Use as a git submodule

In the consuming repo:

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

Then make Python able to find it — either add `vendor/pasker-net/` to
`sys.path` early in your app's entrypoint, or symlink the module under
your project.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from secretvault import default_vault, SECRET_SENTINEL
```

To sync to the latest pasker-net commit:

```bash
git submodule update --remote vendor/pasker-net
git add vendor/pasker-net
git commit -m "bump pasker-net submodule"
```

## secretvault quick start

```python
from secretvault import default_vault

vault = default_vault(service="myapp")
api_key = vault.get("ANTHROPIC_API_KEY")
if api_key is None:
    raise SystemExit(
        "ANTHROPIC_API_KEY not in keychain. Set it with:\n"
        "  python -m secretvault --service myapp set ANTHROPIC_API_KEY"
    )
```

CLI:

```bash
python -m secretvault --service myapp set ANTHROPIC_API_KEY     # prompts (hidden)
python -m secretvault --service myapp get ANTHROPIC_API_KEY     # prints to stdout
python -m secretvault --service myapp exists ANTHROPIC_API_KEY  # exit 0 if present
python -m secretvault --service myapp delete ANTHROPIC_API_KEY
python -m secretvault --service myapp list                       # all under service
```

`set` and `delete` won't clobber an existing secret silently: if the key
already exists they prompt for confirmation (or, when stdin isn't a TTY, refuse
with a non-zero exit). Pass `--force`/`-f` to overwrite or delete without
prompting. `import` likewise skips keys that already exist and reports them;
`import --force` overwrites them.

`default_vault()` returns `KeychainVault` on macOS, `EnvironmentVault`
elsewhere. Service name namespaces the secrets — each consuming app
passes its own (`"edmund"`, `"roscoe"`, etc.) so keys don't collide.

### Standalone CLI (run anywhere)

`secretvault/__main__.py` is fully self-contained — it inlines `KeychainVault`
and depends only on `/usr/bin/security`, so a single copy of the file runs on
any Mac with no repo, package, or `PATH` setup. It carries a PEP 723 header and
a `uv run` shebang. Three ways to invoke it, in order of convenience:

```bash
secretvault --service myapp list          # if copied/symlinked onto your PATH
./secretvault/__main__.py --service myapp list   # uv shebang (needs uv)
python3 secretvault/__main__.py --service myapp list   # any Python 3.9+, no uv
```

Install it as a `secretvault` command on your PATH:

```bash
ln -sf "$PWD/secretvault/__main__.py" ~/.local/bin/secretvault
```

(The symlink points back into this repo; if you move the repo, recreate it. Or
just copy the file itself onto the other machine — it has no dependency on the
repo.)

### Syncing through Bitwarden (`sync`)

`secretvault sync` mirrors secrets, and whole files that other programs read
from fixed paths (e.g. `~/.cloudflared/cert.pem`), through Bitwarden, which
syncs to all your machines. In the Bitwarden folder `secretvault`, each service
is one secure note (the `export` JSON bundle) and each file is one secure note
named by its path under your home directory, which is where `sync` writes it
back (mode 0600). Needs the Bitwarden CLI (`brew install bitwarden-cli`),
logged in and unlocked:

```bash
export BW_SESSION="$(bw unlock --raw)"
secretvault --service myapp sync                  # add a service (first time only)
secretvault sync --file ~/.cloudflared/cert.pem   # add a file (first time only)
secretvault sync                                  # sync everything, both ways
```

Keys or files only on one side are copied to the other. Ones that differ are
reported and left alone; `sync --prefer local` or `--prefer bitwarden` says
which side wins. Files must live under your home directory, so the same
`~/` path works on every machine. Deletes don't propagate: a key or file
deleted locally comes back on the next sync unless you also remove it from
Bitwarden. Only `sync` uses `bw`; apps reading secrets never need it.

### Moving secrets between machines (export / import)

The login Keychain doesn't sync to iCloud for command-line tools (that needs a
signed, provisioned app), so `export` / `import` move a service's secrets
yourself. Bundles are JSON: `{"service": ..., "secrets": {key: value}}`.

```bash
# export to a file — written 0600, then locked to root:wheel 0400, so reading
# or importing it back requires sudo
secretvault --service myapp export myapp-secrets.json

# export to stdout for piping / heredocs (no file on disk)
secretvault --service myapp export

# import from a file (uses sudo automatically if root-locked), stdin, or heredoc
secretvault --service myapp import myapp-secrets.json
secretvault --service myapp import <<'EOF'
{"ANTHROPIC_API_KEY": "sk-..."}
EOF
```

One-shot transfer over SSH, nothing touching disk:

```bash
secretvault --service myapp export | \
    ssh othermac 'python3 - --service myapp import' < secretvault/__main__.py
```

Notes:

- Export/import are **per `--service`** — the login Keychain holds many system
  secrets the vault doesn't own, so there's no safe "export everything".
- Bundles are **plaintext** JSON; the file destination is root-locked for this
  reason. Delete bundles when done.
- Non-ASCII secret values round-trip as hex (a `security -w` quirk); ASCII
  secrets such as API keys/tokens are unaffected.
- `import` does **not** overwrite keys already present in the target service —
  it skips them and lists what it skipped. Re-run with `import --force` to
  overwrite.

## versioning quick start

A git **tag** is the source of truth for the version number.
`canonical_version()` *reads* the latest tag and stamps it with live git
state (for display); `--increment-*` *computes* the next tag (for the release
script). Between releases the version reads as "last tag + commits since".

### In code — for display

Import `canonical_version` and point `repo_root` at **your app's repo** (the
repo whose release tags you cut — in a vendored setup that's the *consumer's*
repo, not the `pasker-net` submodule):

```python
from pathlib import Path
from versioning import canonical_version, current_os

REPO_ROOT = Path(__file__).resolve().parent  # your app's git root

# Single-component repo — base comes from the latest `X.x` tag:
VERSION = canonical_version(repo_root=REPO_ROOT)
#   on main, clean:   "2.1-3f9ab2c"
#   on main, dirty:   "2.1-3f9ab2c-dirty"
#   on a branch:      "2.1-feature-3f9ab2c-dirty"
#   no release tag:   "NOVERSION-3f9ab2c"

# Multi-component repo (e.g. server/client) — scope to your lineage:
VERSION = canonical_version(repo_root=REPO_ROOT, component="server")
#   reads server-* tags only -> "2.1-3f9ab2c"

# Runtime/server stamp that also records the host OS:
VERSION = canonical_version(repo_root=REPO_ROOT, component="server",
                            os_version=current_os())
#   -> "2.1-3f9ab2c (macOS-14.5)"
```

Show `VERSION` in `--version` output, an about box, a `/health` endpoint, or
logs. Notes:

- **Cached.** `canonical_version` memoizes by its arguments, so a version
  shown many times shells out to git only once and stays a stable snapshot
  for the process. If git state changes and you need a fresh read, call
  `canonical_version.cache_clear()`.
- **Reads git at runtime**, so `.git` must be present — fine for a server run
  from a checkout. For a deployed binary with no git (or an Apple app), bake
  the value at build time instead (`emit_swift.py` does this for Swift).
- **Override the base** with an explicit `canonical_version("2.1", ...)` if the
  version lives in a manifest rather than tags.

### In scripts — for cutting a release

The release script asks the CLI for the next tag, then applies and pushes it.
`--increment-*` prints a bare, directly-taggable string (no commit/branch/OS):

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
COMPONENT="server"          # drop --component below for a single-component repo
BUMP="--increment-minor"    # or --increment-major

# Next tag: "2.4" (bare) or "server-2.4" (component-scoped)
NEXT="$(python3 -m versioning --repo-root "$REPO_ROOT" --component "$COMPONENT" "$BUMP")"

echo "cutting $NEXT"
git tag -a "$NEXT" -m "release $NEXT"
git push origin "$NEXT"
```

- `--increment-minor`: `2.3 → 2.4` (`server-2.3 → server-2.4`)
- `--increment-major`: `2.3 → 3.0` (resets minor)
- First release (no tag yet): bootstraps to `1.0` / `0.1` (`server-1.0` /
  `server-0.1`)

Invocation forms, by what's on the box:

```bash
# vendored submodule — package form needs it importable:
PYTHONPATH=vendor/pasker-net python3 -m versioning --component server --increment-minor

# standalone PEP 723 file — no PYTHONPATH, no install (uv runs it):
./vendor/pasker-net/versioning/__main__.py --component server --increment-minor

# symlinked onto PATH:
versioning --component server --increment-minor
```

For an Apple `CFBundleVersion` / Xcode `CURRENT_PROJECT_VERSION`, the build
counter is separate from the marketing tag:

```bash
versioning --build-number   # -> e.g. 412  (git rev-list --count HEAD)
```

### Lifecycle

```text
$ versioning --component server                      # fresh repo, no tags
NOVERSION-a1b2c3d
$ versioning --component server --increment-minor    # bootstrap first release
server-0.1
$ git tag server-0.1 && git push origin server-0.1
$ versioning --component server                      # now reads the tag
0.1-a1b2c3d
  ... commits land ...
$ versioning --component server                      # same base, new commit, dirty
0.1-e4f5a6b-dirty
$ versioning --component server --increment-minor    # cut the next one
server-0.2
$ git tag server-0.2 && git push origin server-0.2
$ versioning --component server
0.2-e4f5a6b
```

Meanwhile `client` releases on its own `client-*` tags, never shadowing
`server`.

## Releasing

This repo's own modules are versioned per-component with the tool itself.
[`release.sh`](release.sh) wraps the increment-and-tag flow:

```bash
./release.sh <component> [major|minor]      # default bump: minor
```

It computes the next tag with
`versioning --component <component> --increment-{major,minor}`, creates an
annotated `<component>-<X.x>` tag on `HEAD`, pushes it to `origin`, and prints
the resulting version (`-v`). A minor bump is `+.1` (`2.3 → 2.4`); a major bump
is `+1` with minor reset (`2.3 → 3.0`). With no prior tag they bootstrap from
`0.0`, so a first major bump lands at `1.0` and a first minor bump at `0.1`.

```bash
./release.sh secretvault major     # first release -> secretvault-1.0
./release.sh queuing minor         # next minor     -> queuing-1.1
```

Each module releases on its own lineage, so `queuing-1.0`, `qworkers-1.0`,
`secretvault-1.0`, and `versioning-1.0` advance independently and never shadow
each other. `--list` shows every component's latest version and the commit it
was cut at, at a glance:

```bash
$ python3 -m versioning --list
queuing      1.0  4ed7dad
qworkers     1.0  4ed7dad
secretvault  1.0  4ed7dad
versioning   1.1  fe38cee
```

To read one module's live version, scope the query to its component (this is
what a consumer that vendors a single module uses, via `--component` /
`component=`):

```bash
python3 -m versioning --component secretvault         # e.g. 1.0-4ed7dad
```
