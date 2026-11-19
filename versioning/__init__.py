"""versioning — Canonical app version string from git state.

Public surface:

    from versioning import canonical_version, current_os

    SERVER_VERSION = canonical_version(
        "2.0",
        repo_root=REPO_ROOT,
        os_version=current_os(),
    )
    # On main, clean:   "2.0-767-52287a4 (macOS-14.5)"        (server: rev + hash)
    # On main, dirty:   "2.0-767-52287a4-dirty (macOS-14.5)"
    # On branch, dirty: "2.0-767-feature-52287a4-dirty (macOS-14.5)"

Two forms share one format definition (:func:`_build`), so they can't drift:

  * :func:`canonical_version` — SERVER form, keeps the commit hash (above).
  * :func:`canonical_version_client` — CLIENT / Apple form, rev but NO hash
    (``"2.0-767 (iOS-17.5)"``, dirty appends a build-time), mirroring the Swift
    ``CanonicalVersion`` emitted by ``emit_swift.py``.

:func:`parse_canonical_version` is the inverse — it decomposes either form's
STRING back into a :class:`VersionParts` (``._asdict()`` for a plain dict), for
a version received from elsewhere (a health check reading ``/api/version``).

:func:`short_version` is the compact display form ``M.m-seq[+]``
(``"2.0-767"``; trailing ``+`` = dirty); :func:`short_from_canonical`
derives it from a canonical string, as the reference for ports to test against.

When ``app_version`` is omitted, the base is taken from the repo's latest
version tag (``latest_version_tag``), so releases are cut with ``git tag``
(see ``release.sh`` / CLAUDE.md); a repo with no canonical-format tag yields
``NOVERSION`` as the base.

``build_number(repo_root)`` returns ``git rev-list --count HEAD`` — the
monotonic integer used in both forms and in an Apple ``CFBundleVersion`` /
``CURRENT_PROJECT_VERSION``. ``python3 -m versioning`` prints the canonical
version (a self-contained PEP 723 program); ``--build-number`` prints the
integer instead.

The ``emit_swift.py`` script in this package emits an equivalent
self-contained Swift file for use as an Xcode Run Script Phase output.

See versioning.py for the implementation.
"""
from .versioning import (
    CANONICAL_VERSION_RE,
    NO_VERSION,
    VersionParts,
    build_number,
    canonical_version,
    canonical_version_client,
    current_os,
    latest_version_tag,
    latest_versions,
    parse_canonical_version,
    short_from_canonical,
    short_version,
)

__all__ = [
    "CANONICAL_VERSION_RE",
    "NO_VERSION",
    "VersionParts",
    "build_number",
    "canonical_version",
    "canonical_version_client",
    "current_os",
    "latest_version_tag",
    "latest_versions",
    "parse_canonical_version",
    "short_from_canonical",
    "short_version",
]
