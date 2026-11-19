"""Tests for short_version: ``M.m-seq[+]``."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from versioning import (canonical_version, canonical_version_client,
                        short_from_canonical, short_version)
from versioning.__main__ import canonical_version as cli_canonical_version
from versioning.__main__ import next_version
from versioning.__main__ import short_version as cli_short_version
from versioning.tests.test_canonical_version_format import PROJECT_ROOT, _git, _TempRepo


class ShortVersion(unittest.TestCase):
    def test_clean(self) -> None:
        with _TempRepo("2.0") as repo:
            self.assertEqual(short_version("2.0", repo_root=repo), "2.0-1")

    def test_truncates_to_major_minor(self) -> None:
        with _TempRepo() as repo:
            self.assertEqual(short_version("3.4.1", repo_root=repo), "3.4-1")

    def test_dirty_plus(self) -> None:
        with _TempRepo("2.0") as repo:
            (Path(repo) / "a").write_text("changed\n")
            self.assertEqual(short_version("2.0", repo_root=repo), "2.0-1+")

    def test_untracked_not_dirty(self) -> None:
        with _TempRepo("2.0") as repo:
            (Path(repo) / "new").write_text("x\n")
            self.assertEqual(short_version("2.0", repo_root=repo), "2.0-1")

    def test_seq_tracks_commits(self) -> None:
        with _TempRepo("2.0") as repo:
            (Path(repo) / "a").write_text("b\n")
            _git(repo, "commit", "-qam", "two")
            self.assertEqual(short_version("2.0", repo_root=repo), "2.0-2")

    def test_base_from_tag_and_component(self) -> None:
        with _TempRepo("1.5.2", "srv-2.3") as repo:
            self.assertEqual(short_version(repo_root=repo), "1.5-1")
            self.assertEqual(short_version(repo_root=repo, component="srv"), "2.3-1")

    def test_no_tag(self) -> None:
        with _TempRepo() as repo:
            self.assertEqual(short_version(repo_root=repo), "NOVERSION-1")

    def test_not_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(short_version("2.0", repo_root=d), "2.0-unknown")

    def test_cli_matches_library(self) -> None:
        with _TempRepo("2.0") as repo:
            (Path(repo) / "a").write_text("changed\n")
            out = subprocess.run(
                [sys.executable, "-m", "versioning", "--short", "--repo-root", repo],
                capture_output=True, text=True, check=True, cwd=PROJECT_ROOT,
            ).stdout.strip()
            self.assertEqual(out, "2.0-1+")
            self.assertEqual(out, cli_short_version(repo))
            self.assertEqual(out, short_version(repo_root=repo))

    def test_cli_canonical_in_sync_with_library(self) -> None:
        # __main__ inlines its own canonical_version; it must not drift.
        for dirty in (False, True):
            with _TempRepo("2.0") as repo:
                _git(repo, "checkout", "-q", "-b", "feature")
                if dirty:
                    (Path(repo) / "a").write_text("changed\n")
                self.assertEqual(
                    cli_canonical_version(repo),
                    canonical_version(repo_root=repo))


class ShortFromCanonical(unittest.TestCase):
    CASES = {
        "0.7-63-9151ff8-dirty": "0.7-63+",
        "0.7-63-9151ff8": "0.7-63",
        "2.0.1-3-abc1234 (macOS-26.1)": "2.0-3",
        "2.0-3-feature-abc1234-dirty (macOS-26.1)": "2.0-3+",
        "1.0-767 (iOS-26.5.1)": "1.0-767",
        "1.0-767-feat-dirty (2026-11-19T14:29Z) (iOS-26.5)": "1.0-767+",
        "1.0-767-dirty (2026-11-19T14:29Z)": "1.0-767+",
        "2.0": "2.0-unknown",                       # bare tag: no seq
        "10.4.1": "10.4-unknown",
        "NOVERSION-46-c7caad6": "NOVERSION-46",
        "NOVERSION-46-c7caad6-dirty": "NOVERSION-46+",
        "NOVERSION-unknown": "NOVERSION-unknown",   # git unavailable
        "2.0-unknown": "2.0-unknown",
    }

    def test_cases(self) -> None:
        for given, want in self.CASES.items():
            with self.subTest(given=given):
                self.assertEqual(short_from_canonical(given), want)

    def test_agrees_with_short_version_for_real_repos(self) -> None:
        for dirty in (False, True):
            with _TempRepo("2.0.1") as repo:
                if dirty:
                    (Path(repo) / "a").write_text("changed\n")
                want = short_version(repo_root=repo)
                self.assertEqual(
                    short_from_canonical(canonical_version(repo_root=repo)), want)
                self.assertEqual(
                    short_from_canonical(canonical_version_client(repo_root=repo)), want)


def _commit(repo: str, text: str = "x") -> None:
    (Path(repo) / "a").write_text(text + "\n")
    _git(repo, "commit", "-qam", text)


class DevelopmentPreviewsNextMinor(unittest.TestCase):
    """After a release tag, new commits build as the NEXT minor until it's tagged."""

    def test_at_tag_is_tag_version(self) -> None:
        with _TempRepo("0.8") as repo:
            self.assertEqual(short_version(repo_root=repo), "0.8-1")

    def test_commit_after_tag_previews_next_minor(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "b")
            self.assertEqual(short_version(repo_root=repo), "0.9-2")
            _commit(repo, "c")
            short_version.cache_clear()  # cached per (args): same repo path
            self.assertEqual(short_version(repo_root=repo), "0.9-3")

    def test_dirty_at_tag_keeps_tag_version(self) -> None:
        with _TempRepo("0.8") as repo:
            (Path(repo) / "a").write_text("wip\n")
            self.assertEqual(short_version(repo_root=repo), "0.8-1+")

    def test_dirty_after_tag(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "b")
            (Path(repo) / "a").write_text("wip\n")
            self.assertEqual(short_version(repo_root=repo), "0.9-2+")

    def test_all_forms_agree(self) -> None:
        with _TempRepo("0.8.3") as repo:
            _commit(repo, "b")
            self.assertTrue(canonical_version(repo_root=repo).startswith("0.9-2-"))
            self.assertEqual(canonical_version_client(repo_root=repo), "0.9-2")
            self.assertEqual(cli_canonical_version(repo), canonical_version(repo_root=repo))
            self.assertEqual(cli_short_version(repo), "0.9-2")
            self.assertEqual(
                short_from_canonical(canonical_version(repo_root=repo)),
                short_version(repo_root=repo))

    def test_component_lineage(self) -> None:
        with _TempRepo("srv-2.3", "cli-1.0") as repo:
            _commit(repo, "b")
            self.assertEqual(short_version(repo_root=repo, component="srv"), "2.4-2")
            self.assertEqual(short_version(repo_root=repo, component="cli"), "1.1-2")

    def test_explicit_app_version_untouched(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "b")
            self.assertEqual(short_version("0.8", repo_root=repo), "0.8-2")

    def test_no_tag_stays_noversion(self) -> None:
        with _TempRepo() as repo:
            _commit(repo, "b")
            self.assertEqual(short_version(repo_root=repo), "NOVERSION-2")

    def test_preview_equals_what_release_will_tag(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "b")
            self.assertEqual(short_version(repo_root=repo).split("-")[0],
                             next_version(repo, "minor"))


if __name__ == "__main__":
    unittest.main()
