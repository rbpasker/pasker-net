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
- [`versioning/`](versioning/) — git-derived app versioning. Two surfaces:
  - `canonical_version("2.0", repo_root=..., os_version=current_os())`
    → `2.0[-<branch>]-<commit>[-dirty][ (<os>)]` (server/runtime stamp; branch
    omitted on `main`/`master`; `<version>-unknown` outside a repo).
  - `build_number(repo_root=...)` → `git rev-list --count HEAD`, a monotonic
    integer for an Apple `CFBundleVersion` / Xcode `CURRENT_PROJECT_VERSION`.
    `python3 -m versioning` prints it for a shell / release script.
  Ships `emit_swift.py` to generate a `CanonicalVersion` Swift enum from an
  Xcode Run Script Phase. The Swift `build` getter renders `X.x-<n>` (marketing
  `X.x` + rev count `n`), so the in-app stamp matches Apple's `X.x (n)`; a dirty
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
