# pasker-net

Shared utility modules for personal projects.

A note from the human: You can call this "vibe coded," but the reality
is that I designed, code reviewed, and tested everyting in this
myself, and it is all running live in production in a number of
personal projects. Each module can be used independently (except
qworkers). PRs welcome. --Bob at y0u kn0w wh3r3


## Modules

- [`secretvault/`](secretvault/README.md) — macOS Keychain-backed secret storage
  with env-var fallback, a self-contained CLI, and export/import between
  machines. Adapted from roscoe-mcp. Usage: [`secretvault/README.md`](secretvault/README.md).
- [`versioning/`](versioning/README.md) — git-derived app versioning: canonical
  and short version strings, build kinds (`d` development / `a` alpha / `b` beta /
  release) derived from tags, a self-contained CLI, release tagging, and a Swift
  emitter. Stdlib only. Usage: [`versioning/README.md`](versioning/README.md).
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

## Module instructions

Usage for each module is in its own README:
[`secretvault/README.md`](secretvault/README.md),
[`versioning/README.md`](versioning/README.md),
[`queuing/README.md`](queuing/README.md),
[`qworkers/README.md`](qworkers/README.md).

## Releasing

This repo's own modules are versioned per-component with the tool itself.
[`release.sh`](release.sh) wraps the increment-and-tag flow:

```bash
./release.sh <component> [major|minor|alpha|beta|release]   # default: minor
```

It computes the next tag with `versioning --component <component> --increment-{major,minor}`
or `--increment {alpha,beta,release}`, creates an annotated `<component>-<tag>` tag
on `HEAD`, pushes it to `origin`, and prints the resulting version (`-v`). A minor
bump is `+.1` (`2.3 → 2.4`); a major bump is `+1` with minor reset (`2.3 → 3.0`).
With no prior tag they bootstrap from `0.0`, so a first major bump lands at `1.0`
and a first minor bump at `0.1`. `alpha` / `beta` tag pre-releases (`1.2a1`,
`1.2b1`) and `release` promotes one (`1.2b1` → `1.2`); illegal moves are refused
and tag nothing. Land the change on `main` **first** — the tag goes on `HEAD`.
The stages, build kinds (`d` / `a` / `b` / release) and exit codes are in
[`versioning/README.md`](versioning/README.md); or run `./release.sh` for usage.

```bash
./release.sh secretvault major     # first release -> secretvault-1.0
./release.sh queuing minor         # next minor     -> queuing-1.1
```

Each module releases on its own lineage, so `queuing-1.0`, `qworkers-1.0`,
`secretvault-1.2b1`, and `versioning-1.5b3` advance independently and never
shadow each other. `--list` shows every component's latest tag — pre-releases
included — and the commit it was cut at, at a glance:

```bash
$ python3 -m versioning --list
queuing      1.0    4ed7dad
qworkers     1.0    4ed7dad
secretvault  1.2b1  80a7e30
versioning   1.5b3  717bd42
```

To read one module's live version, scope the query to its component (this is
what a consumer that vendors a single module uses, via `--component` /
`component=`). A build ahead of the newest tag is a *development* build, kind `d`:

```bash
$ python3 -m versioning --component secretvault           # canonical (server) form
1.2d-56-717bd42-dirty
$ python3 -m versioning --component secretvault --short   # M.m-seq[+]
1.2d-56+
$ python3 -m versioning --component secretvault --build-kind
d
```
