"""versioning — Canonical app version string from git state.

Public surface:

    from versioning import canonical_version

    SERVER_VERSION = canonical_version("1.3", repo_root=REPO_ROOT)
    # On main, clean:   "1.3-52287a4"
    # On main, dirty:   "1.3-52287a4-dirty"
    # On branch, clean: "1.3-feature-52287a4"
    # On branch, dirty: "1.3-feature-52287a4-dirty"

See versioning.py for the implementation.
"""
from .versioning import canonical_version

__all__ = ["canonical_version"]
