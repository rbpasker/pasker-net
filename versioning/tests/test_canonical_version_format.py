"""Tests for CANONICAL_VERSION_RE — which git tags count as a version base.

A tag must be the ``X.x`` marketing form (two or more dot-separated numeric
components) to be used as the canonical base; anything else falls back to
NOVERSION. These cases pin that contract down, and exercise
``latest_version_tag`` / ``canonical_version`` against real git repos to
verify a stray non-canonical tag can't shadow a real release.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from versioning import (
    CANONICAL_VERSION_RE,
    NO_VERSION,
    canonical_version,
    latest_version_tag,
    latest_versions,
)
from versioning.__main__ import next_version

# Root holding the importable `versioning` package, for `-m versioning` runs.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class _TempRepo:
    """A throwaway git repo with one commit and caller-supplied tags."""

    def __init__(self, *tags: str) -> None:
        self._tags = tags

    def __enter__(self) -> str:
        self._dir = tempfile.TemporaryDirectory()
        repo = self._dir.name
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (Path(repo) / "a").write_text("a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        for tag in self._tags:
            _git(repo, "tag", tag)
        return repo

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


def _is_canonical(tag: str) -> bool:
    return bool(CANONICAL_VERSION_RE.match(tag))


class CanonicalVersionFormat(unittest.TestCase):
    def test_accepts_marketing_versions(self) -> None:
        # Two or more numeric components, no prefix/suffix.
        for tag in ("2.0", "1.3", "0.0", "10.4.1", "1.2.3.4", "12.34"):
            with self.subTest(tag=tag):
                self.assertTrue(_is_canonical(tag), f"{tag!r} should be canonical")

    def test_rejects_non_canonical_tags(self) -> None:
        # Leading 'v', single component, pre-release/build suffixes, words,
        # whitespace, empty — none are the bare X.x marketing form.
        for tag in (
            "v2.0",        # prefix
            "2",           # single component
            "2.0-rc1",     # pre-release suffix
            "2.0.0+build", # build metadata
            "2.0 ",        # trailing space
            " 2.0",        # leading space
            "2..0",        # empty component
            "2.0.",        # trailing dot
            "release",     # not numeric
            "",            # empty
        ):
            with self.subTest(tag=tag):
                self.assertFalse(_is_canonical(tag), f"{tag!r} should be rejected")

    def test_match_is_anchored_full_string(self) -> None:
        # A version embedded in a larger string must not match (re.match is
        # left-anchored; CANONICAL_VERSION_RE's trailing ``$`` anchors the right).
        self.assertFalse(_is_canonical("2.0\nmalicious"))
        self.assertFalse(_is_canonical("prefix-2.0"))

    def test_no_version_sentinel(self) -> None:
        # The fallback base is itself not a canonical version (so a tag literally
        # named "NOVERSION" wouldn't be mistaken for a real version base).
        self.assertEqual(NO_VERSION, "NOVERSION")
        self.assertFalse(_is_canonical(NO_VERSION))


class LatestVersionTagSelection(unittest.TestCase):
    def test_picks_highest_canonical_tag(self) -> None:
        # Version-sorted, not lexical: 2.0 > 1.10 > 1.9.
        with _TempRepo("1.9", "1.10", "2.0") as repo:
            self.assertEqual(latest_version_tag(repo), "2.0")

    def test_non_canonical_tag_does_not_shadow_release(self) -> None:
        # The regression that motivated filtering: a stray `v1.0` (or other
        # non-canonical tag) sorts above `2.0` but must NOT be chosen.
        with _TempRepo("v1.0", "2.0", "1.9", "nightly") as repo:
            self.assertEqual(latest_version_tag(repo), "2.0")
            self.assertTrue(canonical_version(repo_root=repo).startswith("2.0-"))

    def test_no_canonical_tag_yields_noversion(self) -> None:
        with _TempRepo("v1.0", "nightly", "2.0-rc1") as repo:
            self.assertIsNone(latest_version_tag(repo))
            self.assertTrue(canonical_version(repo_root=repo).startswith("NOVERSION-"))

    def test_no_tags_yields_noversion(self) -> None:
        with _TempRepo() as repo:
            self.assertIsNone(latest_version_tag(repo))
            self.assertTrue(canonical_version(repo_root=repo).startswith("NOVERSION-"))


class NextVersionIncrement(unittest.TestCase):
    def test_increment_major_resets_minor(self) -> None:
        with _TempRepo("2.3") as repo:
            self.assertEqual(next_version(repo, "major"), "3.0")

    def test_increment_minor_bumps_minor(self) -> None:
        with _TempRepo("2.3") as repo:
            self.assertEqual(next_version(repo, "minor"), "2.4")

    def test_extra_components_are_dropped(self) -> None:
        # A three-part tag is canonical; increments collapse to major.minor.
        with _TempRepo("1.3.4") as repo:
            self.assertEqual(next_version(repo, "major"), "2.0")
            self.assertEqual(next_version(repo, "minor"), "1.4")

    def test_increment_uses_latest_canonical_base(self) -> None:
        # Non-canonical tags are ignored, and the base is version-sorted.
        with _TempRepo("v9.9", "1.9", "1.10", "2.0", "nightly") as repo:
            self.assertEqual(next_version(repo, "major"), "3.0")
            self.assertEqual(next_version(repo, "minor"), "2.1")

    def test_bootstrap_from_zero_when_no_canonical_tag(self) -> None:
        with _TempRepo("nightly", "v1.0") as repo:
            self.assertEqual(next_version(repo, "major"), "1.0")
            self.assertEqual(next_version(repo, "minor"), "0.1")


class ComponentTags(unittest.TestCase):
    """A component scopes the scan to `<component>-<X.x>` tags."""

    def test_scan_is_isolated_per_component(self) -> None:
        with _TempRepo("server-2.1", "server-1.9", "client-3.0", "1.5") as repo:
            self.assertEqual(latest_version_tag(repo, "server"), "2.1")
            self.assertEqual(latest_version_tag(repo, "client"), "3.0")
            # A bare scan ignores component tags; component scans ignore bare.
            self.assertEqual(latest_version_tag(repo), "1.5")

    def test_returns_bare_version_with_prefix_stripped(self) -> None:
        with _TempRepo("server-2.1") as repo:
            # The component prefix is stripped from the returned version, and
            # the canonical string is the version only (component not added).
            self.assertEqual(latest_version_tag(repo, "server"), "2.1")
            self.assertTrue(
                canonical_version(repo_root=repo, component="server").startswith("2.1-")
            )

    def test_version_sort_within_component(self) -> None:
        with _TempRepo("server-1.9", "server-1.10", "server-2.0") as repo:
            self.assertEqual(latest_version_tag(repo, "server"), "2.0")

    def test_no_matching_component_tag(self) -> None:
        # Sibling component tags exist, but none for "server".
        with _TempRepo("client-3.0", "1.5") as repo:
            self.assertIsNone(latest_version_tag(repo, "server"))
            self.assertTrue(
                canonical_version(repo_root=repo, component="server").startswith("NOVERSION-")
            )

    def test_increment_emits_component_prefixed_tag(self) -> None:
        with _TempRepo("server-2.3", "client-9.9") as repo:
            self.assertEqual(next_version(repo, "major", "server"), "server-3.0")
            self.assertEqual(next_version(repo, "minor", "server"), "server-2.4")

    def test_increment_bootstraps_first_component_release(self) -> None:
        with _TempRepo("client-9.9") as repo:
            self.assertEqual(next_version(repo, "major", "server"), "server-1.0")
            self.assertEqual(next_version(repo, "minor", "server"), "server-0.1")


class LatestVersionsListing(unittest.TestCase):
    """latest_versions() gives the highest version per component."""

    def test_highest_per_component_plus_bare(self) -> None:
        with _TempRepo("queuing-1.0", "qworkers-1.0", "secretvault-1.0",
                       "versioning-1.0", "0.9", "1.2") as repo:
            self.assertEqual(latest_versions(repo), {
                "queuing": "1.0",
                "qworkers": "1.0",
                "secretvault": "1.0",
                "versioning": "1.0",
                None: "1.2",          # bare lineage, highest of 0.9 / 1.2
            })

    def test_picks_version_sorted_max_within_component(self) -> None:
        with _TempRepo("svc-1.9", "svc-1.10", "svc-2.0") as repo:
            self.assertEqual(latest_versions(repo), {"svc": "2.0"})

    def test_hyphenated_component_name(self) -> None:
        # The greedy parse keeps hyphens in the component, splitting on the
        # final `-<version>`.
        with _TempRepo("server-side-2.1") as repo:
            self.assertEqual(latest_versions(repo), {"server-side": "2.1"})

    def test_ignores_non_version_tags(self) -> None:
        with _TempRepo("nightly", "v1.0", "rc-2.0-beta") as repo:
            self.assertEqual(latest_versions(repo), {})

    def test_empty_repo(self) -> None:
        with _TempRepo() as repo:
            self.assertEqual(latest_versions(repo), {})


class CanonicalVersionCaching(unittest.TestCase):
    """canonical_version memoizes by args; a process sees a stable snapshot."""

    def tearDown(self) -> None:
        canonical_version.cache_clear()

    def test_result_is_cached_until_cleared(self) -> None:
        canonical_version.cache_clear()
        with _TempRepo("2.0") as repo:
            first = canonical_version(repo_root=repo)
            self.assertTrue(first.startswith("2.0-"))
            # A higher tag would change the base on a fresh read...
            _git(repo, "tag", "3.0")
            # ...but the cached call returns the original snapshot.
            self.assertEqual(canonical_version(repo_root=repo), first)
            # Clearing forces a re-read, now reflecting the new tag.
            canonical_version.cache_clear()
            self.assertTrue(canonical_version(repo_root=repo).startswith("3.0-"))

    def test_repeat_call_is_a_cache_hit(self) -> None:
        canonical_version.cache_clear()
        with _TempRepo("2.0") as repo:
            canonical_version(repo_root=repo)
            before = canonical_version.cache_info().hits
            canonical_version(repo_root=repo)
            self.assertEqual(canonical_version.cache_info().hits, before + 1)


class IncrementCLI(unittest.TestCase):
    """End-to-end: the release script invokes `-m versioning --increment-*`."""

    def _run(self, repo: str, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "versioning", "--repo-root", repo, *flags],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

    def test_emits_bare_version_only(self) -> None:
        # No commit/branch/dirty in the output — just the number to tag with.
        with _TempRepo("2.3") as repo:
            major = self._run(repo, "--increment-major")
            self.assertEqual(major.returncode, 0)
            self.assertEqual(major.stdout.strip(), "3.0")
            minor = self._run(repo, "--increment-minor")
            self.assertEqual(minor.stdout.strip(), "2.4")

    def test_increment_flags_are_mutually_exclusive(self) -> None:
        with _TempRepo("2.3") as repo:
            result = self._run(repo, "--increment-major", "--increment-minor")
            self.assertEqual(result.returncode, 2)  # argparse usage error
            self.assertIn("not allowed with", result.stderr)

    def test_component_increment_emits_taggable_string(self) -> None:
        with _TempRepo("server-2.3", "client-9.9") as repo:
            result = self._run(repo, "--component", "server", "--increment-major")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "server-3.0")

    def test_list_shows_every_component(self) -> None:
        with _TempRepo("queuing-1.0", "versioning-2.0", "1.5") as repo:
            result = self._run(repo, "--list")
            self.assertEqual(result.returncode, 0)
            lines = result.stdout.splitlines()
            # Named components first (alphabetical), bare lineage last.
            self.assertEqual(lines[0].split(), ["queuing", "1.0"])
            self.assertEqual(lines[1].split(), ["versioning", "2.0"])
            self.assertEqual(lines[-1].split(), ["(bare)", "1.5"])


if __name__ == "__main__":
    unittest.main()
