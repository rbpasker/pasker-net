"""CLI: print the git-derived build number (``git rev-list --count HEAD``).

Single source of truth for the Apple ``CFBundleVersion`` / Xcode
``CURRENT_PROJECT_VERSION`` value, so a release script and ``emit_swift.py``
agree on the number. Mirrors the package's ``-m`` invocation convention:

    PYTHONPATH=vendor/pasker-net python3 -m versioning            # count for cwd
    PYTHONPATH=vendor/pasker-net python3 -m versioning --repo-root PATH

Prints the integer to stdout and exits 0, or an error to stderr and exits 1
if the path isn't a git repo / git is unavailable.
"""
from __future__ import annotations

import argparse
import sys

from .versioning import build_number


def main() -> int:
    ap = argparse.ArgumentParser(prog="versioning", description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--repo-root", default=None,
                    help="Path to the git repo. Defaults to the current directory.")
    args = ap.parse_args()

    n = build_number(args.repo_root)
    if n is None:
        print("error: not a git repo, or git unavailable", file=sys.stderr)
        return 1
    print(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
