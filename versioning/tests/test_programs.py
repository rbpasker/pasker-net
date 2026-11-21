"""Tests for pre-release programs: alpha (a) / beta (b) / rc tags that sort
below the release, previews of the next step in the same program, and the
`--increment alpha|beta|rc|release` tag computation (plus release.sh)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from versioning import (
    VersionParts, canonical_version, canonical_version_client, latest_program_tag,
    latest_version_tag, parse_canonical_version, short_from_canonical,
    short_version, split_program_version,
)
from versioning.__main__ import canonical_version as cli_canonical_version
from versioning.__main__ import latest_program_tag as cli_latest_program_tag
from versioning.__main__ import main as cli_main
from versioning.__main__ import next_program_version
from versioning.__main__ import short_version as cli_short_version
from versioning.tests.test_canonical_version_format import PROJECT_ROOT, _git, _TempRepo
from versioning.versioning import _program_key


def _commit(repo: str, text: str = "x") -> None:
    (Path(repo) / "a").write_text(text + "\n")
    _git(repo, "commit", "-qam", text)


def _forms(repo: str, **kw) -> dict:
    """Every version form for `repo`, caches cleared (same path is reused)."""
    for fn in (short_version, canonical_version, canonical_version_client):
        fn.cache_clear()
    return {"short": short_version(repo_root=repo, **kw),
            "server": canonical_version(repo_root=repo, **kw),
            "client": canonical_version_client(repo_root=repo, **kw),
            "cli_short": cli_short_version(repo, kw.get("component")),
            "cli_server": cli_canonical_version(repo, kw.get("component"))}


class Grammar(unittest.TestCase):
    def test_split(self) -> None:
        self.assertEqual(split_program_version("0.9b2"), ("0.9", "b", 2))
        self.assertEqual(split_program_version("1.3.4rc1"), ("1.3.4", "rc", 1))
        self.assertEqual(split_program_version("0.9a10"), ("0.9", "a", 10))
        self.assertEqual(split_program_version("0.9"), ("0.9", None, None))
        self.assertEqual(split_program_version("NOVERSION"), ("NOVERSION", None, None))

    def test_ordering(self) -> None:
        order = ["0.8", "0.9a1", "0.9a2", "0.9a10", "0.9b1", "0.9rc1", "0.9", "0.10a1", "0.10"]
        self.assertEqual(sorted(reversed(order), key=_program_key), order)

    def test_release_outranks_its_prereleases(self) -> None:
        with _TempRepo("0.9a1", "0.9b2", "0.9rc1", "0.9", "0.8") as repo:
            self.assertEqual(latest_program_tag(repo), "0.9")

    def test_highest_stage_wins_below_release(self) -> None:
        with _TempRepo("0.8", "0.9a3", "0.9b1") as repo:
            self.assertEqual(latest_program_tag(repo), "0.9b1")

    def test_numeric_not_lexical(self) -> None:
        with _TempRepo("0.9a2", "0.9a10") as repo:
            self.assertEqual(latest_program_tag(repo), "0.9a10")

    def test_release_only_lookup_ignores_prereleases(self) -> None:
        # Existing callers (latest_version_tag) must not start seeing 0.9b2.
        with _TempRepo("0.8", "0.9b2") as repo:
            self.assertEqual(latest_version_tag(repo), "0.8")
            self.assertEqual(latest_program_tag(repo), "0.9b2")

    def test_stray_tags_ignored(self) -> None:
        with _TempRepo("v1.0b1", "2.0-rc1", "0.9beta1", "nightly", "0.8") as repo:
            self.assertEqual(latest_program_tag(repo), "0.8")

    def test_component_lineages(self) -> None:
        with _TempRepo("srv-2.3b1", "srv-2.2", "cli-1.0", "0.5a1") as repo:
            self.assertEqual(latest_program_tag(repo, "srv"), "2.3b1")
            self.assertEqual(latest_program_tag(repo, "cli"), "1.0")
            self.assertEqual(latest_program_tag(repo), "0.5a1")

    def test_no_tags(self) -> None:
        with _TempRepo() as repo:
            self.assertIsNone(latest_program_tag(repo))


class Builds(unittest.TestCase):
    def test_at_prerelease_tag(self) -> None:
        with _TempRepo("0.9b2") as repo:
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9b2-1")
            self.assertEqual(f["client"], "0.9b2-1")
            self.assertTrue(f["server"].startswith("0.9b2-1-"))

    def test_dirty_at_prerelease_tag_keeps_it(self) -> None:
        with _TempRepo("0.9b2") as repo:
            (Path(repo) / "a").write_text("wip\n")
            self.assertEqual(_forms(repo)["short"], "0.9b2-1+")

    def test_ahead_of_prerelease_previews_next_number(self) -> None:
        with _TempRepo("0.9b2") as repo:
            _commit(repo, "b")
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9b3-2")
            self.assertEqual(f["client"], "0.9b3-2")
            self.assertTrue(f["server"].startswith("0.9b3-2-"))

    def test_ahead_of_alpha_stays_alpha(self) -> None:
        with _TempRepo("0.9a1") as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo)["short"], "0.9a2-2")

    def test_ahead_of_release_after_prereleases_previews_next_minor(self) -> None:
        with _TempRepo("0.9a1", "0.9b1", "0.9") as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo)["short"], "0.10-2")

    def test_patch_prerelease_short(self) -> None:
        with _TempRepo("1.3.4rc1") as repo:
            self.assertEqual(_forms(repo)["short"], "1.3rc1-1")

    def test_cli_and_library_agree_through_the_program(self) -> None:
        with _TempRepo("0.8") as repo:
            for tag in ("0.9a1", "0.9a2", "0.9b1", "0.9rc1", "0.9"):
                _commit(repo, tag)
                f = _forms(repo)
                self.assertEqual(f["short"], f["cli_short"], tag)
                self.assertEqual(f["server"], f["cli_server"], tag)
                self.assertEqual(cli_latest_program_tag(repo), latest_program_tag(repo))
                _git(repo, "tag", tag)
                f = _forms(repo)
                self.assertEqual(f["short"], f["cli_short"], tag)
                self.assertEqual(f["short"].split("-")[0], tag)  # at the tag: the tag

    def test_explicit_app_version_untouched(self) -> None:
        with _TempRepo("0.9b2") as repo:
            _commit(repo, "b")
            self.assertEqual(short_version("0.9b2", repo_root=repo), "0.9b2-2")

    def test_component_prerelease_preview(self) -> None:
        with _TempRepo("srv-2.3b1", "cli-1.0") as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo, component="srv")["short"], "2.3b2-2")
            self.assertEqual(_forms(repo, component="cli")["short"], "1.1-2")


class ParseAndShort(unittest.TestCase):
    def test_parse_roundtrip_with_stage(self) -> None:
        p = parse_canonical_version("0.9b3-70-9151ff8-dirty (macOS-26.1)")
        self.assertEqual((p.app_version, p.rev_number, p.commit, p.dirty, p.os_version),
                         ("0.9b3", 70, "9151ff8", True, "macOS-26.1"))
        self.assertEqual((p.release_version, p.stage, p.pre_number), ("0.9", "b", 3))

    def test_release_has_no_stage(self) -> None:
        p = parse_canonical_version("0.9-70-9151ff8")
        self.assertEqual((p.release_version, p.stage, p.pre_number), ("0.9", None, None))
        self.assertIsInstance(p, VersionParts)

    def test_short_from_canonical_cases(self) -> None:
        cases = {
            "0.9b3-70-9151ff8-dirty": "0.9b3-70+",
            "0.9b3-70-9151ff8": "0.9b3-70",
            "0.9a1-5 (iOS-26.5)": "0.9a1-5",
            "1.3.4rc1-8-abc1234": "1.3rc1-8",
            "0.10-71-abc1234": "0.10-71",
            "0.9b3-unknown": "0.9b3-unknown",
            "NOVERSION-46-c7caad6": "NOVERSION-46",
        }
        for given, want in cases.items():
            with self.subTest(given=given):
                self.assertEqual(short_from_canonical(given), want)


class NextProgramVersion(unittest.TestCase):
    def _next(self, tags, kind, component=None):
        with _TempRepo(*tags) as repo:
            return next_program_version(repo, kind, component)

    def test_start_from_release(self) -> None:
        self.assertEqual(self._next(["0.8"], "alpha"), "0.9a1")
        self.assertEqual(self._next(["0.8"], "beta"), "0.9b1")   # skipping a stage is fine
        self.assertEqual(self._next(["0.8"], "rc"), "0.9rc1")

    def test_advance_through_stages(self) -> None:
        self.assertEqual(self._next(["0.9a1"], "alpha"), "0.9a2")
        self.assertEqual(self._next(["0.9a2"], "beta"), "0.9b1")
        self.assertEqual(self._next(["0.9b3"], "beta"), "0.9b4")
        self.assertEqual(self._next(["0.9b3"], "rc"), "0.9rc1")
        self.assertEqual(self._next(["0.9a2"], "rc"), "0.9rc1")

    def test_promote_to_release(self) -> None:
        self.assertEqual(self._next(["0.9rc2"], "release"), "0.9")
        self.assertEqual(self._next(["0.9a1"], "release"), "0.9")
        self.assertEqual(self._next(["1.3.4b2"], "release"), "1.3.4")

    def test_bootstrap(self) -> None:
        self.assertEqual(self._next([], "alpha"), "0.1a1")

    def test_component_prefix(self) -> None:
        self.assertEqual(self._next(["srv-2.3a1", "cli-9.9"], "beta", "srv"), "srv-2.3b1")
        self.assertEqual(self._next(["srv-2.3"], "alpha", "srv"), "srv-2.4a1")

    def test_illegal_moves(self) -> None:
        for tags, kind in ((["0.9b1"], "alpha"), (["0.9rc1"], "beta"),
                           (["0.9rc1"], "alpha"), (["0.8"], "release"), ([], "release")):
            with self.subTest(tags=tags, kind=kind), self.assertRaises(ValueError):
                self._next(tags, kind)

    def test_unknown_program(self) -> None:
        with self.assertRaises(ValueError):
            self._next(["0.8"], "gamma")

    def test_full_cycle_previews_the_release_being_built(self) -> None:
        # Each dev preview names the same M.m the tool then says to tag.
        with _TempRepo("0.8") as repo:
            for i, kind in enumerate(("alpha", "alpha", "beta", "rc", "release")):
                _commit(repo, f"step{i}")
                preview = _forms(repo)["short"].split("-")[0]
                nxt = next_program_version(repo, kind)
                self.assertEqual(split_program_version(preview)[0],
                                 split_program_version(nxt)[0], kind)
                _git(repo, "tag", nxt)
            self.assertEqual(latest_program_tag(repo), "0.9")


class IncrementCLI(unittest.TestCase):
    def _run(self, repo: str, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "versioning", "--repo-root", repo, *flags],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

    def test_emits_bare_tag(self) -> None:
        with _TempRepo("0.9a2") as repo:
            r = self._run(repo, "--increment", "beta")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "0.9b1"))

    def test_illegal_move_exits_1_with_no_stdout(self) -> None:
        with _TempRepo("0.9b1") as repo:
            r = self._run(repo, "--increment", "alpha")
            self.assertEqual((r.returncode, r.stdout), (1, ""))
            self.assertIn("cannot go back", r.stderr)

    def test_bad_program_name_is_a_usage_error(self) -> None:
        with _TempRepo("0.9a1") as repo:
            self.assertEqual(self._run(repo, "--increment", "gamma").returncode, 2)

    def test_mutually_exclusive_with_other_modes(self) -> None:
        with _TempRepo("0.9a1") as repo:
            self.assertEqual(
                self._run(repo, "--increment", "beta", "--increment-minor").returncode, 2)

    def test_existing_increments_unchanged_with_prereleases_present(self) -> None:
        # --increment-minor/major stay release-based.
        with _TempRepo("0.8", "0.9b2") as repo:
            self.assertEqual(self._run(repo, "--increment-minor").stdout.strip(), "0.9")

    def test_verbose_describes_program_tag(self) -> None:
        with _TempRepo("0.9b2") as repo:
            r = self._run(repo, "-v", "--short")
            self.assertIn("0.9b2", r.stderr)
            self.assertEqual(r.stdout.strip(), "0.9b2-1")


class ReleaseScript(unittest.TestCase):
    """release.sh <component> alpha|beta|rc|release tags the computed value."""

    def _setup(self, *tags: str):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        origin = Path(d.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        work = Path(d.name) / "work"
        work.mkdir()
        _git(str(work), "init", "-q")
        _git(str(work), "config", "user.email", "t@t")
        _git(str(work), "config", "user.name", "t")
        shutil.copy(PROJECT_ROOT / "release.sh", work / "release.sh")
        shutil.copytree(PROJECT_ROOT / "versioning", work / "versioning",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        _git(str(work), "add", "-A")
        _git(str(work), "commit", "-qm", "init")
        _git(str(work), "remote", "add", "origin", str(origin))
        for t in tags:
            _git(str(work), "tag", "-a", t, "-m", t)
        return work, origin

    def _release(self, work, *args):
        return subprocess.run(["bash", "release.sh", *args], cwd=str(work),
                              capture_output=True, text=True)

    def test_alpha_then_beta_then_release(self) -> None:
        work, origin = self._setup("comp-0.8")
        for arg, want in (("alpha", "comp-0.9a1"), ("alpha", "comp-0.9a2"),
                          ("beta", "comp-0.9b1"), ("rc", "comp-0.9rc1"),
                          ("release", "comp-0.9")):
            r = self._release(work, "comp", arg)
            self.assertEqual(r.returncode, 0, r.stderr)
            tags = _git(str(origin), "tag").split()
            self.assertIn(want, tags)
            # same commit gets every tag; stage is by tag, so later calls just compute.
            _git(str(work), "commit", "-q", "--allow-empty", "-m", arg)

    def test_illegal_move_tags_nothing(self) -> None:
        work, origin = self._setup("comp-0.9b1")
        r = self._release(work, "comp", "alpha")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(_git(str(origin), "tag").split(), [])

    def test_bad_arg_usage(self) -> None:
        work, _ = self._setup()
        self.assertEqual(self._release(work, "comp", "gamma").returncode, 2)


if __name__ == "__main__":
    unittest.main()
