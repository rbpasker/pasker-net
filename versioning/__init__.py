"""versioning — Canonical app version string from git state.

Public surface:

    from versioning import canonical_version, current_os

    SERVER_VERSION = canonical_version(
        "2.0",
        repo_root=REPO_ROOT,
        os_version=current_os(),
    )
    # On main, clean:   "2.0-52287a4 (macOS-14.5)"
    # On main, dirty:   "2.0-52287a4-dirty (macOS-14.5)"
    # On branch, clean: "2.0-feature-52287a4 (macOS-14.5)"

When ``app_version`` is omitted, the base is taken from the repo's latest
version tag (``latest_version_tag``), so releases are cut with ``git tag``;
a repo with no canonical-format tag yields ``NOVERSION`` as the base.

``build_number(repo_root)`` returns ``git rev-list --count HEAD`` — a
monotonic integer for an Apple ``CFBundleVersion`` / ``CURRENT_PROJECT_VERSION``.
``python3 -m versioning`` prints the canonical version (a self-contained
PEP 723 program); ``--build-number`` prints the integer instead.

The ``emit_swift.py`` script in this package emits an equivalent
self-contained Swift file for use as an Xcode Run Script Phase output.

See versioning.py for the implementation.
"""
from .versioning import (
    CANONICAL_VERSION_RE,
    NO_VERSION,
    build_number,
    canonical_version,
    current_os,
    latest_version_tag,
)

__all__ = [
    "CANONICAL_VERSION_RE",
    "NO_VERSION",
    "build_number",
    "canonical_version",
    "current_os",
    "latest_version_tag",
]
