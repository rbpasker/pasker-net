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


if __name__ == "__main__":
    unittest.main()
