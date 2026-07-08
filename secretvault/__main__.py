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
            "  use `export` / `import`.\n"
            "\n"
            "  `export FILE` writes a plaintext JSON bundle, then locks it to\n"
            "  root (mode 0400) so it can't be read or imported without sudo;\n"
            "  `export` with no FILE writes to stdout for piping/heredocs.\n"
            "  `import` reads a FILE (using sudo if the file is root-locked),\n"
            "  or stdin/heredoc when no FILE is given."
        ),
    )
    parser.add_argument(
        "--service", "-s", required=True,
        help="Keychain service name (namespace your app's secrets)",
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
