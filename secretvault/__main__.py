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


def _service_accounts(service: str) -> list[str]:
    """All account (key) names stored under `service`, sorted and deduped.

    `security` has no "list by service" subcommand, so dump the keychain and
    filter by the `svce` attribute. dump-keychain prints one record per item,
    each starting with `keychain: "..."`, then attribute lines like:
        "svce"<blob>="edmund"
        "acct"<blob>="EDMUND_SESSION_SECRET"
    The attribute NAME is the first quoted token (svce/acct), NOT the whole
    `"svce"<blob>` left-hand side — keying on the latter never matches.
    """
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True,
    )
    accounts: list[str] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("svce") == service and current.get("acct"):
            accounts.append(current["acct"])

    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("keychain:"):
            flush()
            current.clear()
            continue
        if not line.startswith('"'):
            continue
        end = line.find('"', 1)
        eq = line.find("=", end) if end > 0 else -1
        if end <= 0 or eq < 0:
            continue
        name = line[1:end]
        val = line[eq + 1:].strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        elif val == "<NULL>":
            val = ""
        current[name] = val
    flush()
    return sorted(set(accounts))


def cmd_list(args: argparse.Namespace) -> int:
    """List all key names under the given service."""
    accounts = _service_accounts(args.service)
    if not accounts:
        print(f"no secrets found for service '{args.service}'")
        return 0
    print(f"secrets for service '{args.service}':")
    for acct in accounts:
        print(f"  - {acct}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Dump all keys under the service, for backup or as a manifest.

    Default: `KEY=VALUE` per line — the REAL secrets (hand-carry to another host,
    `import`, then delete). `--no-secrets`: key NAMES only, safe to commit (e.g.
    a secrets.txt.example manifest). A leading `#` header documents which it is.
    """
    accounts = _service_accounts(args.service)
    lines = [f"# secretvault extract{' --no-secrets' if args.no_secrets else ''} "
             f"(service: {args.service})"]
    if args.no_secrets:
        lines += accounts
    else:
        lines.append("# KEY=VALUE — REAL secrets. Do NOT commit. import, then delete.")
        vault = KeychainVault(service=args.service)
        for acct in accounts:
            lines.append(f"{acct}={vault.get(acct) or ''}")
    text = "\n".join(lines) + "\n"
    if args.output and args.output != "-":
        with open(args.output, "w") as fh:
            fh.write(text)
        print(f"wrote {len(accounts)} key(s) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Load `KEY=VALUE` lines (from `extract`) into the keychain via set().

    '#' comments and blank lines are ignored; existing keys are overwritten.
    """
    src = sys.stdin if (not args.input or args.input == "-") else open(args.input)
    vault = KeychainVault(service=args.service)
    count = 0
    try:
        for raw in src:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            vault.set(key.strip(), val)
            count += 1
    finally:
        if src is not sys.stdin:
            src.close()
    print(f"imported {count} secret(s) into service '{args.service}'", file=sys.stderr)
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

    p = subs.add_parser("extract",
                        help="Dump all keys (KEY=VALUE, or names with --no-secrets)")
    p.add_argument("--no-secrets", action="store_true",
                   help="names only, omit values (safe to commit as a manifest)")
    p.add_argument("-o", "--output", help="write to FILE instead of stdout")
    p.set_defaults(func=cmd_extract)

    p = subs.add_parser("import", help="Load KEY=VALUE lines into the keychain")
    p.add_argument("-i", "--input", help="read from FILE instead of stdin")
    p.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
