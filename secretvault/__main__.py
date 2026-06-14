"""CLI for managing secrets in the macOS Keychain.

Usage:
    python -m secretvault --service <name> set KEY [VALUE]
    python -m secretvault --service <name> get KEY
    python -m secretvault --service <name> delete KEY
    python -m secretvault --service <name> exists KEY
    python -m secretvault --service <name> list

`--service` is required so secrets are namespaced to one consuming app
(e.g. `--service edmund`, `--service roscoe`). For `set`, omit VALUE to
be prompted with hidden input.
"""
from __future__ import annotations

import argparse
import getpass
import subprocess
import sys

from .vault import KeychainVault


def cmd_set(args: argparse.Namespace) -> int:
    vault = KeychainVault(service=args.service)
    if args.value is not None:
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


def cmd_list(args: argparse.Namespace) -> int:
    """List all keys under the given service.

    `security` doesn't have a direct "list by service" subcommand, so we
    dump the whole login keychain and filter to entries whose `svce`
    field matches `args.service`. Output sorted, deduped.
    """
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True,
    )
    current: dict[str, str] = {}
    accounts: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("keychain:"):
            if current.get("svce") == f'"{args.service}"':
                acct = current.get("acct", "").strip('"')
                if acct:
                    accounts.append(acct)
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()
    if current.get("svce") == f'"{args.service}"':
        acct = current.get("acct", "").strip('"')
        if acct:
            accounts.append(acct)
    if not accounts:
        print(f"no secrets found for service '{args.service}'")
        return 0
    print(f"secrets for service '{args.service}':")
    for acct in sorted(set(accounts)):
        print(f"  - {acct}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m secretvault",
        description="Manage secrets in the macOS Keychain.",
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
    p.set_defaults(func=cmd_set)

    p = subs.add_parser("get", help="Retrieve a secret")
    p.add_argument("key", help="Secret key")
    p.set_defaults(func=cmd_get)

    p = subs.add_parser("delete", help="Delete a secret")
    p.add_argument("key", help="Secret key")
    p.set_defaults(func=cmd_delete)

    p = subs.add_parser("exists", help="Check if a secret exists")
    p.add_argument("key", help="Secret key")
    p.set_defaults(func=cmd_exists)

    p = subs.add_parser("list", help="List all secrets under this service")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
