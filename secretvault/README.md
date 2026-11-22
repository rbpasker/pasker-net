# secretvault

macOS Keychain-backed secret storage with env-var fallback. Pluggable backends, namespace-per-app via Keychain service name. Ships a self-contained CLI (`secretvault/__main__.py`) that runs as a PEP 723 script and can `export`/`import` a service's secrets to move them between machines. Adapted from roscoe-mcp. Part of [pasker-net](../README.md). Paths are relative to the pasker-net checkout (or `vendor/pasker-net/` in a consumer).

## Quick start

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
python -m secretvault --service myapp set APNS_KEY --file AuthKey.p8   # value read verbatim from a file
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

`set --file PATH` (`-F`) reads the value **verbatim** from a file, so multi-line
secrets — a PEM private key, an APNs `.p8`, a Cloudflare `cert.pem` — keep their
newlines and never land in shell history. Without it, `set` prompts (hidden) or
takes the value as an argument.

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

# names only — every value null, a plain world-readable file (no root-lock), safe
# to commit as a manifest of the keys an app expects (e.g. secrets.json.example)
secretvault --service myapp export --no-secrets secrets.json.example

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
- **Multi-line and non-ASCII values round-trip.** macOS `security -w` hands back
  a value hex-encoded when it contains a newline or a non-ASCII/control byte, so
  such values are stored base64-encoded behind a marker prefix
  (`secretvault$b64$1$…`) and decoded on read. Single-line printable-ASCII values
  (API keys, tokens) are stored verbatim, so existing secrets read back
  unchanged. Read secrets through `secretvault get` / `vault.get()`: a raw
  `security find-generic-password -w` on a wrapped value shows the marker and
  base64, not the plaintext.
- `import` does **not** overwrite keys already present in the target service —
  it skips them and lists what it skipped. Re-run with `import --force` to
  overwrite. It accepts the full envelope above or a bare `{key: value}` map
  (as in the heredoc example), and notes when a bundle came from a different
  service.
