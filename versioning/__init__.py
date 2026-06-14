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

The ``emit_swift.py`` script in this package emits an equivalent
self-contained Swift file for use as an Xcode Run Script Phase output.

See versioning.py for the implementation.
"""
from .versioning import canonical_version, current_os

__all__ = ["canonical_version", "current_os"]
