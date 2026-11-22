#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""CLI for managing secrets in the macOS Keychain.

Usage:
    python -m secretvault --service <name> set KEY [VALUE]
    python -m secretvault --service <name> get KEY
    python -m secretvault --service <name> delete KEY
    python -m secretvault --service <name> exists KEY
    python -m secretvault --service <name> list
    python -m secretvault --service <name> export [FILE]
    python -m secretvault --service <name> import [FILE]
    python -m secretvault [--service <name>] sync [--file PATH ...] [--prefer local|bitwarden]

`--service` is required so secrets are namespaced to one consuming app
(e.g. `--service edmund`, `--service roscoe`). For `set`, omit VALUE to
be prompted with hidden input, or pass `--file PATH` to read the value
verbatim from a file (the way to store a multi-line secret like a PEM
private key without it landing in your shell history).

Destructive operations require explicit intervention: `set` and `delete`
prompt for confirmation when the key already exists (or refuse with a
non-zero exit when stdin isn't a TTY), and `import` skips keys that already
exist. Pass `--force`/`-f` to overwrite or delete without prompting.

`export` / `import` move a whole service's secrets between machines (the
Keychain itself doesn't sync to iCloud for command-line tools). `export`
writes a JSON bundle to a file (mode 0600) or stdout; `import` reads it
back from a file, a pipe, or a heredoc:

    # machine A
    python -m secretvault --service myapp export myapp-secrets.json
    # machine B
    python -m secretvault --service myapp import myapp-secrets.json
    # or pipe / heredoc, no file on disk:
    python -m secretvault --service myapp export | \\
        ssh other 'python -m secretvault --service myapp import'

`sync` does the same through Bitwarden, which syncs to every machine, and
also carries whole files (e.g. ~/.cloudflared/cert.pem) that other programs
read from fixed paths. In Bitwarden folder `secretvault`, each service is one
secure note (named after it, body = the export bundle) and each file is one
secure note (named by its `~/` path, body = its contents). `sync` syncs
everything already there, both ways; `--service NAME sync` adds a service and
`sync --file PATH` adds a file under $HOME. Restored files are written mode
0600. Keys or files that differ on the two sides are reported and left alone
unless `--prefer local|bitwarden` says which side wins. Deletes don't
propagate (delete it in Bitwarden too). Needs the `bw` CLI unlocked:
export BW_SESSION="$(bw unlock --raw)".
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


# Keychain value encoding — inlined to keep this CLI self-contained. macOS
# `security -w` returns a stored value HEX-ENCODED (not verbatim) when it
# contains a newline / non-ASCII / control byte, corrupting multi-line secrets
# like a PEM key. So such values are stored base64 behind a marker and decoded
# on read; single-line printable-ASCII values are stored verbatim (backward-
# compatible). Keep in sync with secretvault/vault.py.
_B64_MARKER = "secretvault$b64$1$"


def _keychain_safe(value: str) -> bool:
    return value.isascii() and value.isprintable() and value == value.strip()


def _encode_for_keychain(value: str) -> str:
    if _keychain_safe(value):
        return value
    return _B64_MARKER + base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_from_keychain(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith(_B64_MARKER):
        try:
            return base64.b64decode(
                raw[len(_B64_MARKER):], validate=True
            ).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return raw
    return raw


# KeychainVault is inlined (rather than imported from secretvault.vault) so
# this file is fully self-contained: copy it to any Mac and run it — no repo,
# no package, no PATH setup. The only runtime requirement is /usr/bin/security,
# which ships with macOS. Keep this in sync with secretvault/vault.py.
class KeychainVault:
    """macOS Keychain backend via `/usr/bin/security`, bound to one service."""

    def __init__(self, service: str):
        if not service:
            raise ValueError("KeychainVault requires a non-empty service name")
        self._service = service

    @property
    def service(self) -> str:
        return self._service

    def get(self, key: str) -> "str | None":
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", self._service, "-a", key, "-w"],
                capture_output=True, text=True, check=True,
            )
            return _decode_from_keychain(result.stdout)
        except subprocess.CalledProcessError:
            return None

    def set(self, key: str, value: str) -> None:
        # `-U` updates in place if the entry exists. Multi-line / non-ASCII
        # values are base64-wrapped so `security -w` round-trips them.
        subprocess.run(
            ["security", "add-generic-password",
             "-s", self._service, "-a", key, "-w",
             _encode_for_keychain(value), "-U"],
            capture_output=True, check=True,
        )

    def delete(self, key: str) -> bool:
        try:
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", self._service, "-a", key],
                capture_output=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def exists(self, key: str) -> bool:
        try:
            subprocess.run(
                ["security", "find-generic-password",
                 "-s", self._service, "-a", key],
                capture_output=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False


def cmd_set(args: argparse.Namespace) -> int:
    vault = KeychainVault(service=args.service)
    # Overwriting an existing secret is destructive (the old value is gone),
    # so require explicit intervention when the key already exists: --force
    # always overwrites; otherwise prompt (interactive) or refuse (non-tty).
    # Check before asking for the value so the operator isn't made to type a
    # secret only to have it refused.
    if not args.force and vault.exists(args.key):
        if sys.stdin.isatty():
            resp = input(
                f"'{args.key}' already exists (service={args.service}). Overwrite? [y/N] "
            ).strip().lower()
            if resp not in ("y", "yes"):
                print("aborted; existing value left unchanged", file=sys.stderr)
                return 1
        else:
            print(
                f"error: '{args.key}' already exists (service={args.service}); "
                f"pass --force to overwrite",
                file=sys.stderr,
            )
            return 1
    if args.file is not None and args.value is not None:
        print("error: pass either VALUE or --file, not both", file=sys.stderr)
        return 1
    if args.file is not None:
        # Read the file VERBATIM (no strip) so a multi-line secret — e.g. a PEM
        # private key — round-trips byte-for-byte. The Keychain layer
        # base64-wraps it transparently (see _encode_for_keychain).
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                value = fh.read()
        except OSError as exc:
            print(f"error: cannot read --file {args.file!r}: {exc}", file=sys.stderr)
            return 1
        if not value:
            print(f"error: --file {args.file!r} is empty", file=sys.stderr)
            return 1
    elif args.value is not None:
        value = args.value
    else:
        value = getpass.getpass(f"Enter value for '{args.key}' (service={args.service}): ")
        if not value:
            print("error: empty value", file=sys.stderr)
            return 1
    vault.set(args.key, value)
    print(f"stored '{args.key}' in keychain (service={args.service})")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    vault = KeychainVault(service=args.service)
    value = vault.get(args.key)
    if value is None:
        print(f"error: '{args.key}' not found in keychain (service={args.service})",
              file=sys.stderr)
        return 1
    print(value)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    vault = KeychainVault(service=args.service)
    # Deleting is destructive too, so require intervention: --force always
    # deletes; otherwise prompt (interactive) or refuse (non-tty).
    if not args.force and vault.exists(args.key):
        if sys.stdin.isatty():
            resp = input(
                f"delete '{args.key}' (service={args.service})? [y/N] "
            ).strip().lower()
            if resp not in ("y", "yes"):
                print("aborted; nothing deleted", file=sys.stderr)
                return 1
        else:
            print(
                f"error: refusing to delete '{args.key}' (service={args.service}); "
                f"pass --force to confirm",
                file=sys.stderr,
            )
            return 1
    if vault.delete(args.key):
        print(f"deleted '{args.key}' (service={args.service})")
        return 0
    print(f"error: '{args.key}' not found (service={args.service})", file=sys.stderr)
    return 1


def cmd_exists(args: argparse.Namespace) -> int:
    vault = KeychainVault(service=args.service)
    if vault.exists(args.key):
        print(f"'{args.key}' exists (service={args.service})")
        return 0
    print(f"'{args.key}' does not exist (service={args.service})")
    return 1


def _accounts_for_service(service: str) -> list[str]:
    """All account (key) names stored under `service`, sorted and deduped.

    `security` doesn't have a direct "list by service" subcommand, so we
    dump the whole login keychain and filter to entries whose `svce`
    field matches `service`.
    """
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True,
    )
    # dump-keychain prints one item per `keychain:` block; attributes look
    # like `"svce"<blob>="myapp"` / `"acct"<blob>="MY_KEY"`, so we key off the
    # quoted attribute name and keep the value (still quoted) verbatim.
    current: dict[str, str] = {}
    accounts: list[str] = []

    def flush() -> None:
        if current.get("svce") == f'"{service}"':
            acct = current.get("acct", "").strip('"')
            if acct and acct != "<NULL>":
                accounts.append(acct)

    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("keychain:"):
            flush()
            current.clear()
        elif line.startswith('"svce"'):
            current["svce"] = line.split("=", 1)[1]
        elif line.startswith('"acct"'):
            current["acct"] = line.split("=", 1)[1]
    flush()
    return sorted(set(accounts))


def cmd_list(args: argparse.Namespace) -> int:
    """List all keys under the given service."""
    accounts = _accounts_for_service(args.service)
    if not accounts:
        print(f"no secrets found for service '{args.service}'")
        return 0
    print(f"secrets for service '{args.service}':")
    for acct in accounts:
        print(f"  - {acct}")
    return 0


def _lock_to_root(path: str) -> bool:
    """Hand `path` to root and drop all other access (chmod 0400).

    Makes an exported plaintext bundle unreadable without sudo. Uses sudo for
    the chown/chmod (and will prompt for a password). Returns False if sudo is
    unavailable or declined — the caller then warns and leaves the file 0600.
    """
    try:
        subprocess.run(["sudo", "chown", "root:wheel", path], check=True)
        subprocess.run(["sudo", "chmod", "0400", path], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _read_maybe_root(path: str) -> str:
    """Read `path`, falling back to `sudo cat` when it's root-locked."""
    try:
        with open(path, "r") as fh:
            return fh.read()
    except PermissionError:
        result = subprocess.run(
            ["sudo", "cat", path], capture_output=True, text=True, check=True,
        )
        return result.stdout


def cmd_export(args: argparse.Namespace) -> int:
    """Dump every secret under the service as a JSON bundle.

    These are plaintext secrets, so a file destination is created mode 0600
    and then locked to root (mode 0400) — reading or importing it back needs
    sudo. The bundle records its source service for a sanity check on import.
    The summary line goes to stderr so stdout stays clean for piping.
    """
    vault = KeychainVault(service=args.service)
    accounts = _accounts_for_service(args.service)
    if args.no_secrets:
        # Names only: values are null. The result carries no secrets, so it's a
        # plain world-readable file — a committable manifest (e.g. secrets.json.example).
        secrets: dict[str, "str | None"] = {acct: None for acct in accounts}
    else:
        secrets = {}
        for acct in accounts:
            value = vault.get(acct)
            if value is not None:
                secrets[acct] = value
    payload = {"service": args.service, "secrets": secrets}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if args.file and args.file != "-":
        if args.no_secrets:
            with open(args.file, "w") as fh:
                fh.write(text)
            print(f"wrote {len(secrets)} key name(s) (no values) for service "
                  f"'{args.service}' to {args.file}", file=sys.stderr)
        else:
            fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            if _lock_to_root(args.file):
                locked = "root-owned, mode 0400 — needs sudo to read/import"
            else:
                locked = "WARNING: could not lock with sudo; left mode 0600 (readable by you)"
            print(f"exported {len(secrets)} secret(s) for service '{args.service}' "
                  f"to {args.file} ({locked})", file=sys.stderr)
    else:
        sys.stdout.write(text)
        kind = "key name(s) (no values)" if args.no_secrets else "secret(s)"
        print(f"exported {len(secrets)} {kind} for service '{args.service}'",
              file=sys.stderr)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Load secrets from a JSON bundle (file, stdin, or heredoc) into the service.

    Accepts the `export` envelope ({"service":..,"secrets":{..}}) or a bare
    {key: value} object for hand-rolled heredocs. Keys that already exist are
    left untouched and reported unless --force is given (overwriting a secret
    discards its old value, so it takes explicit intervention).
    """
    if args.file and args.file != "-":
        raw = _read_maybe_root(args.file)
    else:
        raw = sys.stdin.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 1

    if isinstance(data, dict) and isinstance(data.get("secrets"), dict):
        secrets = data["secrets"]
        src = data.get("service")
        if src and src != args.service:
            print(f"note: bundle was exported from service '{src}'; "
                  f"importing into '{args.service}'", file=sys.stderr)
    elif isinstance(data, dict):
        secrets = data
    else:
        print("error: expected a JSON object", file=sys.stderr)
        return 1

    vault = KeychainVault(service=args.service)
    count = 0
    skipped: list[str] = []
    for key, value in secrets.items():
        if not isinstance(value, str):
            print(f"skipping '{key}': value is not a string", file=sys.stderr)
            continue
        if not args.force and vault.exists(key):
            skipped.append(key)
            continue
        vault.set(key, value)
        count += 1
    print(f"imported {count} secret(s) into service '{args.service}'",
          file=sys.stderr)
    if skipped:
        print(f"skipped {len(skipped)} existing key(s) (pass --force to overwrite): "
              f"{', '.join(sorted(skipped))}", file=sys.stderr)
    return 0


# --- Bitwarden sync -------------------------------------------------------
# `sync` keeps each service's secrets mirrored in Bitwarden (which syncs to
# every machine) as one secure note per service — named after the service, in
# folder `secretvault`, body = the same JSON bundle `export` writes. Files are
# one secure note each in the same folder, named by their `~/` path (that
# prefix is what tells a file from a service), body = the file's contents.
# Only `sync` touches `bw`; apps reading secrets never need it.

_BW_FOLDER = "secretvault"


class BwError(RuntimeError):
    pass


def _bw(*args: str) -> str:
    try:
        result = subprocess.run(["bw", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise BwError("Bitwarden CLI `bw` not found — brew install bitwarden-cli")
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        raise BwError(f"bw {args[0]} failed: {msg}")
    return result.stdout


def _bw_json(*args: str):
    return json.loads(_bw(*args))


def _bw_encode(obj: dict) -> str:
    # Equivalent to piping the JSON through `bw encode`.
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _bundle_text(service: str, secrets: dict) -> str:
    return json.dumps({"service": service, "secrets": secrets},
                      indent=2, sort_keys=True) + "\n"


def _sync_service(service: str, item: "dict | None", folder_id: str,
                  prefer: "str | None") -> bool:
    """Two-way merge of one service. Returns False if anything was left unresolved."""
    remote: dict[str, str] = {}
    if item is not None:
        try:
            remote = json.loads(item.get("notes") or "{}").get("secrets", {})
        except (json.JSONDecodeError, AttributeError):
            print(f"{service}: Bitwarden note is not a secretvault bundle; skipped",
                  file=sys.stderr)
            return False
        remote = {k: v for k, v in remote.items() if isinstance(v, str)}

    vault = KeychainVault(service=service)
    local = {}
    for acct in _accounts_for_service(service):
        value = vault.get(acct)
        if value is not None:
            local[acct] = value

    merged = dict(remote)
    pushed, pulled, conflicts = [], [], []
    for key in sorted(set(local) | set(remote)):
        lv, rv = local.get(key), remote.get(key)
        if lv == rv:
            continue
        if rv is None or (lv is not None and prefer == "local"):
            merged[key] = lv
            pushed.append(key)
        elif lv is None or prefer == "bitwarden":
            vault.set(key, rv)
            pulled.append(key)
        else:
            conflicts.append(key)

    if item is None or merged != remote:
        _upsert_note(item, service, _bundle_text(service, merged), folder_id)

    parts = []
    if pushed:
        parts.append(f"→ bitwarden: {', '.join(pushed)}")
    if pulled:
        parts.append(f"→ keychain: {', '.join(pulled)}")
    print(f"{service}: " + ("; ".join(parts) if parts else "in sync"))
    if conflicts:
        print(f"{service}: differ in Keychain and Bitwarden, left unchanged "
              f"(rerun with --prefer local|bitwarden): {', '.join(conflicts)}",
              file=sys.stderr)
    return not conflicts


def _note_name(path: str) -> str:
    """``~/rel/path`` for a file under $HOME; refuses anything outside it."""
    home = Path.home().resolve()
    p = Path(path).expanduser().resolve()
    try:
        rel = p.relative_to(home)
    except ValueError:
        raise BwError(f"{path}: not under your home directory ({home})")
    return f"~/{rel.as_posix()}"


def _file_path(name: str) -> Path:
    """Inverse of _note_name; refuses a note name that escapes $HOME."""
    home = Path.home().resolve()
    p = (home / name[2:]).resolve()
    if home not in p.parents:
        raise BwError(f"refusing note {name!r}: path escapes home directory")
    return p


def _upsert_note(item: "dict | None", name: str, body: str, folder_id: str) -> None:
    if item is None:
        stored = _bw_json("create", "item", _bw_encode({
            "type": 2, "secureNote": {"type": 0}, "folderId": folder_id,
            "organizationId": None, "name": name, "notes": body,
            "favorite": False, "fields": [], "reprompt": 0,
        }))
    else:
        stored = _bw_json("edit", "item", item["id"],
                          _bw_encode({**item, "notes": body}))
    if stored.get("notes") != body:
        print(f"{name}: WARNING Bitwarden did not store it byte-for-byte",
              file=sys.stderr)


def _sync_file(name: str, item: "dict | None", folder_id: str,
               prefer: "str | None") -> bool:
    """Two-way sync of one file. Returns False if left unresolved."""
    try:
        path = _file_path(name)
        local = path.read_text(encoding="utf-8") if path.exists() else None
    except (BwError, OSError, UnicodeDecodeError) as exc:
        print(f"{name}: {exc}; skipped", file=sys.stderr)
        return False
    remote = item.get("notes") if item is not None else None
    if local is None and remote is None:
        print(f"{name}: no such file here or in Bitwarden", file=sys.stderr)
        return False
    if local == remote:
        print(f"{name}: in sync")
        return True
    if remote is None or (local is not None and prefer == "local"):
        if not local:
            print(f"{name}: empty file; skipped", file=sys.stderr)
            return False
        _upsert_note(item, name, local, folder_id)
        print(f"{name}: → bitwarden")
    elif local is None or prefer == "bitwarden":
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".secretvault-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(remote)
        os.replace(tmp, path)
        print(f"{name}: → file (mode 0600)")
    else:
        print(f"{name}: differs locally and in Bitwarden, left unchanged "
              f"(rerun with --prefer local|bitwarden)", file=sys.stderr)
        return False
    return True


def cmd_sync(args: argparse.Namespace) -> int:
    """Two-way sync of every service and file in Bitwarden, plus any added."""
    nothing = ("nothing to sync yet; add a service with `secretvault --service "
               "NAME sync` or a file with `secretvault sync --file PATH`")
    try:
        added_files = {_note_name(f) for f in args.files}
        status = _bw_json("status").get("status")
        if status != "unlocked":
            hint = ("bw login" if status == "unauthenticated"
                    else 'export BW_SESSION="$(bw unlock --raw)"')
            raise BwError(f"Bitwarden vault is {status}; run: {hint}")
        _bw("sync")
        folder_id = next((f["id"] for f in _bw_json("list", "folders", "--search", _BW_FOLDER)
                          if f.get("name") == _BW_FOLDER), None)
        if folder_id is None:
            if not args.service and not added_files:
                print(nothing)
                return 0
            folder_id = _bw_json("create", "folder", _bw_encode({"name": _BW_FOLDER}))["id"]
        items = {i["name"]: i for i in _bw_json("list", "items", "--folderid", folder_id)
                 if i.get("type") == 2}
        files = sorted({n for n in items if n.startswith("~/")} | added_files)
        services = sorted({n for n in items if not n.startswith("~/")}
                          | ({args.service} if args.service else set()))
        if not services and not files:
            print(nothing)
            return 0
        ok = True
        for service in services:
            ok = _sync_service(service, items.get(service), folder_id, args.prefer) and ok
        for name in files:
            ok = _sync_file(name, items.get(name), folder_id, args.prefer) and ok
        return 0 if ok else 1
    except BwError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m secretvault",
        description="Manage secrets in the macOS Keychain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "storage:\n"
            "  Secrets are stored in your macOS LOGIN keychain via\n"
            "  /usr/bin/security. The login keychain does NOT sync to iCloud\n"
            "  (command-line tools can't write the iCloud Keychain without a\n"
            "  signed, provisioned app), so to move secrets to another machine\n"
            "  use `sync` (via Bitwarden) or `export` / `import`.\n"
            "\n"
            "  `sync` mirrors every service and file in Bitwarden folder\n"
            "  'secretvault' both ways (needs the `bw` CLI, logged in and\n"
            "  unlocked via BW_SESSION); `--service NAME sync` adds a service,\n"
            "  `sync --file PATH` adds a file under $HOME (restored mode 0600).\n"
            "  Deleting a key or file locally does not delete it from\n"
            "  Bitwarden — the next sync restores it.\n"
            "\n"
            "  `export FILE` writes a plaintext JSON bundle, then locks it to\n"
            "  root (mode 0400) so it can't be read or imported without sudo;\n"
            "  `export` with no FILE writes to stdout for piping/heredocs.\n"
            "  `import` reads a FILE (using sudo if the file is root-locked),\n"
            "  or stdin/heredoc when no FILE is given."
        ),
    )
    parser.add_argument(
        "--service", "-s", default=None,
        help="Keychain service name (namespace your app's secrets); required "
             "except for `sync`, where it adds that service to the sync set",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("set", help="Store a secret")
    p.add_argument("key", help="Secret key")
    p.add_argument("value", nargs="?", default=None,
                   help="Secret value (prompts if omitted)")
    p.add_argument("--file", "-F", default=None, metavar="PATH",
                   help="read the value verbatim from a file (preserves "
                        "newlines — for multi-line secrets like a PEM key)")
    p.add_argument("--force", "-f", action="store_true",
                   help="overwrite an existing key without prompting")
    p.set_defaults(func=cmd_set)

    p = subs.add_parser("get", help="Retrieve a secret")
    p.add_argument("key", help="Secret key")
    p.set_defaults(func=cmd_get)

    p = subs.add_parser("delete", help="Delete a secret")
    p.add_argument("key", help="Secret key")
    p.add_argument("--force", "-f", action="store_true",
                   help="delete without prompting")
    p.set_defaults(func=cmd_delete)

    p = subs.add_parser("exists", help="Check if a secret exists")
    p.add_argument("key", help="Secret key")
    p.set_defaults(func=cmd_exists)

    p = subs.add_parser("list", help="List all secrets under this service")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("export", help="Export all secrets under this service as JSON")
    p.add_argument("file", nargs="?", default=None,
                   help="Output file (created mode 0600); omit or '-' for stdout")
    p.add_argument("--no-secrets", action="store_true",
                   help="values null (names only); world-readable, safe to commit as a manifest")
    p.set_defaults(func=cmd_export)

    p = subs.add_parser("import", help="Import secrets from JSON (file, stdin, or heredoc)")
    p.add_argument("file", nargs="?", default=None,
                   help="Input file; omit or '-' to read stdin")
    p.add_argument("--force", "-f", action="store_true",
                   help="overwrite keys that already exist (default: skip them)")
    p.set_defaults(func=cmd_import)

    p = subs.add_parser("sync", help="two-way sync with Bitwarden (all services and "
                                     "files there, plus --service / --file)")
    p.add_argument("--file", dest="files", action="append", default=[], metavar="PATH",
                   help="also sync this file under $HOME (repeatable); adds it "
                        "to Bitwarden the first time")
    p.add_argument("--prefer", choices=("local", "bitwarden"), default=None,
                   help="which side wins for keys or files that differ "
                        "(default: report them and change neither)")
    p.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    if args.service is None and args.command != "sync":
        parser.error("--service is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
