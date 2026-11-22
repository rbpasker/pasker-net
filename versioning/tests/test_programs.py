"""Tests for build kinds: d (development) / a (alpha) / b (beta) / blank (release).
alpha/beta TAGS sort below the release; a build ahead of any tag is a development
build (`d`) of the release in flight; and the `--increment alpha|beta|release`
tag computation (plus release.sh)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from versioning import (
    GitError, VersionParts, build_kind, canonical_version, canonical_version_client, latest_program_tag,
    latest_programs, latest_version_tag, latest_versions, parse_canonical_version, short_from_canonical,
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
        self.assertEqual(split_program_version("1.3.4b1"), ("1.3.4", "b", 1))
        self.assertEqual(split_program_version("0.10d"), ("0.10", "d", None))
        self.assertEqual(split_program_version("0.9a10"), ("0.9", "a", 10))
        self.assertEqual(split_program_version("0.9"), ("0.9", None, None))
        self.assertEqual(split_program_version("NOVERSION"), ("NOVERSION", None, None))

    def test_ordering(self) -> None:
        order = ["0.8", "0.9a1", "0.9a2", "0.9a10", "0.9b1", "0.9", "0.10a1", "0.10"]
        self.assertEqual(sorted(reversed(order), key=_program_key), order)

    def test_release_outranks_its_prereleases(self) -> None:
        with _TempRepo("0.9a1", "0.9b2", "0.9", "0.8") as repo:
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
        # rc and d are not tag kinds; neither are hyphenated / spelled-out forms.
        with _TempRepo("v1.0b1", "2.0-rc1", "0.9beta1", "0.9rc1", "0.9d", "nightly", "0.8") as repo:
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

    def test_ahead_of_beta_is_a_development_build_of_that_release(self) -> None:
        with _TempRepo("0.9b2") as repo:
            _commit(repo, "b")
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9d-2")
            self.assertEqual(f["client"], "0.9d-2")
            self.assertTrue(f["server"].startswith("0.9d-2-"))

    def test_ahead_of_alpha_is_development_too(self) -> None:
        with _TempRepo("0.9a1") as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo)["short"], "0.9d-2")

    def test_ahead_of_release_is_development_of_the_next_minor(self) -> None:
        with _TempRepo("0.9a1", "0.9b1", "0.9") as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo)["short"], "0.10d-2")

    def test_dirty_development(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "b")
            (Path(repo) / "a").write_text("wip\n")
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9d-2+")
            self.assertTrue(f["server"].endswith("-dirty"))
            self.assertIn("0.9d-2-dirty", f["client"])

    def test_dirty_at_release_tag_keeps_the_kind(self) -> None:
        with _TempRepo("0.8") as repo:
            (Path(repo) / "a").write_text("wip\n")
            self.assertEqual(_forms(repo)["short"], "0.8-1+")      # still blank, flagged by +

    def test_patch_prerelease_short(self) -> None:
        with _TempRepo("1.3.4b1") as repo:
            self.assertEqual(_forms(repo)["short"], "1.3b1-1")

    def test_no_tags_has_no_kind(self) -> None:
        with _TempRepo() as repo:
            _commit(repo, "b")
            self.assertEqual(_forms(repo)["short"], "NOVERSION-2")

    def test_cli_and_library_agree_through_the_program(self) -> None:
        with _TempRepo("0.8") as repo:
            for tag in ("0.9a1", "0.9a2", "0.9b1", "0.9"):
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
            self.assertEqual(_forms(repo, component="srv")["short"], "2.3d-2")
            self.assertEqual(_forms(repo, component="cli")["short"], "1.1d-2")


class ParseAndShort(unittest.TestCase):
    def test_parse_roundtrip_with_stage(self) -> None:
        p = parse_canonical_version("0.9b3-70-9151ff8-dirty (macOS-26.1)")
        self.assertEqual((p.app_version, p.rev_number, p.commit, p.dirty, p.os_version),
                         ("0.9b3", 70, "9151ff8", True, "macOS-26.1"))
        self.assertEqual((p.release_version, p.stage, p.pre_number), ("0.9", "b", 3))
        self.assertEqual(p.build_kind, "b")

    def test_release_has_no_stage(self) -> None:
        p = parse_canonical_version("0.9-70-9151ff8")
        self.assertEqual((p.release_version, p.stage, p.pre_number), ("0.9", None, None))
        self.assertEqual(p.build_kind, "")
        self.assertIsInstance(p, VersionParts)

    def test_development_parses(self) -> None:
        p = parse_canonical_version("0.10d-71-9151ff8-dirty")
        self.assertEqual((p.app_version, p.rev_number, p.commit, p.dirty), ("0.10d", 71, "9151ff8", True))
        self.assertEqual((p.build_kind, p.release_version, p.pre_number), ("d", "0.10", None))

    def test_build_kind_function(self) -> None:
        for v, want in (("0.9", ""), ("0.9a1", "a"), ("0.9b2", "b"), ("0.10d", "d"),
                        ("NOVERSION", ""), ("2.0-rc1", ""), ("", "")):
            with self.subTest(v=v):
                self.assertEqual(build_kind(v), want)

    def test_short_from_canonical_cases(self) -> None:
        cases = {
            "0.9b3-70-9151ff8-dirty": "0.9b3-70+",
            "0.9b3-70-9151ff8": "0.9b3-70",
            "0.9a1-5 (iOS-26.5)": "0.9a1-5",
            "1.3.4b1-8-abc1234": "1.3b1-8",
            "0.10-71-abc1234": "0.10-71",
            "0.10d-71-abc1234-dirty": "0.10d-71+",
            "0.10d-71-abc1234": "0.10d-71",
            "0.10d-71 (iOS-26.5)": "0.10d-71",
            "0.10d-unknown": "0.10d-unknown",
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

    def test_advance_through_stages(self) -> None:
        self.assertEqual(self._next(["0.9a1"], "alpha"), "0.9a2")
        self.assertEqual(self._next(["0.9a2"], "beta"), "0.9b1")
        self.assertEqual(self._next(["0.9b3"], "beta"), "0.9b4")

    def test_promote_to_release(self) -> None:
        self.assertEqual(self._next(["0.9b2"], "release"), "0.9")
        self.assertEqual(self._next(["0.9a1"], "release"), "0.9")
        self.assertEqual(self._next(["1.3.4b2"], "release"), "1.3.4")

    def test_bootstrap(self) -> None:
        self.assertEqual(self._next([], "alpha"), "0.1a1")

    def test_component_prefix(self) -> None:
        self.assertEqual(self._next(["srv-2.3a1", "cli-9.9"], "beta", "srv"), "srv-2.3b1")
        self.assertEqual(self._next(["srv-2.3"], "alpha", "srv"), "srv-2.4a1")

    def test_illegal_moves(self) -> None:
        for tags, kind in ((["0.9b1"], "alpha"), (["0.8"], "release"), ([], "release")):
            with self.subTest(tags=tags, kind=kind), self.assertRaises(ValueError):
                self._next(tags, kind)

    def test_unknown_kind(self) -> None:
        for kind in ("gamma", "rc", "d", "development"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self._next(["0.8"], kind)

    def test_full_cycle_previews_the_release_being_built(self) -> None:
        # Each dev preview names the same M.m the tool then says to tag.
        with _TempRepo("0.8") as repo:
            for i, kind in enumerate(("alpha", "alpha", "beta", "release")):
                _commit(repo, f"step{i}")
                preview = _forms(repo)["short"].split("-")[0]
                nxt = next_program_version(repo, kind)
                self.assertEqual(split_program_version(preview)[0],
                                 split_program_version(nxt)[0], kind)
                _git(repo, "tag", nxt)
            self.assertEqual(latest_program_tag(repo), "0.9")


class ReachableTagsOnly(unittest.TestCase):
    """A build's version comes from tags REACHABLE from HEAD, not every tag in the repo."""

    def test_checkout_older_than_the_tag(self) -> None:
        with _TempRepo("0.8") as repo:
            _commit(repo, "older")
            older = _git(repo, "rev-parse", "HEAD")
            _commit(repo, "newer")
            _git(repo, "tag", "-a", "0.9", "-m", "x")
            _git(repo, "checkout", "-q", older)
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9d-2")  # development build ahead of 0.8
            self.assertEqual(f["short"], f["cli_short"])
            self.assertEqual(latest_program_tag(repo, merged=True), "0.8")
            self.assertEqual(latest_program_tag(repo), "0.9")  # all tags still seen

    def test_older_than_every_tag_has_no_version(self) -> None:
        with _TempRepo() as repo:
            first = _git(repo, "rev-parse", "HEAD")
            _commit(repo, "later")
            _git(repo, "tag", "-a", "1.0", "-m", "x")
            _git(repo, "checkout", "-q", first)
            self.assertEqual(_forms(repo)["short"], "NOVERSION-1")

    def test_tag_on_unmerged_branch_is_ignored(self) -> None:
        with _TempRepo("0.8") as repo:
            main = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            _git(repo, "checkout", "-q", "-b", "rel")
            _commit(repo, "rel")
            _git(repo, "tag", "-a", "1.0", "-m", "x")
            _git(repo, "checkout", "-q", main)
            _commit(repo, "m")
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9d-2")  # not 1.1
            self.assertEqual(f["short"], f["cli_short"])

    def test_next_tag_uses_all_tags_so_numbers_are_never_reissued(self) -> None:
        with _TempRepo("0.8") as repo:
            main = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            _git(repo, "checkout", "-q", "-b", "rel")
            _commit(repo, "rel")
            _git(repo, "tag", "-a", "0.9a1", "-m", "x")
            _git(repo, "checkout", "-q", main)
            _commit(repo, "m")
            self.assertEqual(next_program_version(repo, "alpha"), "0.9a2")

    def test_merged_tag_counts(self) -> None:
        with _TempRepo("0.8") as repo:
            main = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            _git(repo, "checkout", "-q", "-b", "rel")
            _commit(repo, "rel")
            _git(repo, "tag", "-a", "0.9b1", "-m", "x")
            _git(repo, "checkout", "-q", main)
            _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "rel")
            self.assertTrue(_forms(repo)["short"].startswith("0.9d-"))  # ahead of merged 0.9b1


class ListAndDescribe(unittest.TestCase):
    def _run(self, repo, *flags):
        return subprocess.run([sys.executable, "-m", "versioning", "--repo-root", repo, *flags],
                              capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    def test_list_shows_prerelease_like_builds_do(self) -> None:
        with _TempRepo("queuing-1.0", "queuing-1.1b1", "versioning-2.0", "0.5a1") as repo:
            rows = {ln.split()[0]: ln.split()[1]
                    for ln in self._run(repo, "--list").stdout.splitlines()}
            self.assertEqual(rows, {"queuing": "1.1b1", "versioning": "2.0", "(bare)": "0.5a1"})
            self.assertEqual(latest_programs(repo),
                             {"queuing": "1.1b1", "versioning": "2.0", None: "0.5a1"})

    def test_list_matches_what_a_build_reports(self) -> None:
        with _TempRepo("queuing-1.1b1") as repo:
            listed = self._run(repo, "--list").stdout.split()[1]
            self.assertEqual(_forms(repo, component="queuing")["short"].split("-")[0], listed)

    def test_release_only_lookups_unchanged(self) -> None:
        with _TempRepo("queuing-1.0", "queuing-1.1b1") as repo:
            self.assertEqual(latest_versions(repo), {"queuing": "1.0"})

    def test_describe_reports_the_reachable_program_tag(self) -> None:
        with _TempRepo("0.9b2") as repo:
            self.assertIn("0.9b2", self._run(repo, "-v", "--short").stderr)


class LibraryAndCliCopyAgree(unittest.TestCase):
    """The CLI inlines its own copy of the library; compare them over many repo states."""

    SCENARIOS = [
        (), ("0.8",), ("0.9a1",), ("0.9b2",), ("1.3.4b1",), ("0.8", "0.9a3", "0.9b1"),
        ("0.9a1", "0.9b1", "0.9"), ("srv-2.3b1", "srv-2.2", "cli-1.0", "0.5a1"),
        ("v1.0", "2.0-rc1", "nightly"),
    ]

    def test_constants_match(self) -> None:
        import versioning.__main__ as cli
        import versioning.versioning as lib
        for name in ("_DEFAULT_MAIN_BRANCHES", "_GIT_TIMEOUT_S", "_GIT_ATTEMPTS", "NO_VERSION",
                     "PRE_STAGES", "_GENERATED_EXCLUDES", "_KINDS"):
            with self.subTest(name=name):
                self.assertEqual(getattr(cli, name), getattr(lib, name))
        for name in ("CANONICAL_VERSION_RE", "PROGRAM_VERSION_RE", "_ANY_VERSION_TAG_RE",
                     "_ANY_PROGRAM_TAG_RE"):
            with self.subTest(name=name):
                self.assertEqual(getattr(cli, name).pattern, getattr(lib, name).pattern)

    @staticmethod
    def _either(fn, *args):
        try:
            return fn(*args)
        except ValueError as e:
            return f"ValueError: {e}"

    def test_lookups_agree_over_scenarios(self) -> None:
        import versioning.__main__ as cli
        import versioning.versioning as lib
        for tags in self.SCENARIOS:
            with _TempRepo(*tags) as repo:
                _commit(repo, "ahead")
                for comp in (None, "srv", "cli"):
                    with self.subTest(tags=tags, comp=comp):
                        self.assertEqual(cli.latest_version_tag(repo, comp),
                                         lib.latest_version_tag(repo, comp))
                        for merged in (False, True):
                            self.assertEqual(cli.latest_program_tag(repo, comp, merged),
                                             lib.latest_program_tag(repo, comp, merged=merged))
                        self.assertEqual(cli._resolve_app_version(repo, comp),
                                         lib._resolve_app_version(repo, comp))
                        for kind in ("alpha", "beta", "rc", "release"):
                            self.assertEqual(
                                self._either(cli.next_program_version, repo, kind, comp),
                                self._either(lib.next_program_version, repo, kind, comp))
                        for part in ("major", "minor"):
                            self.assertEqual(self._either(cli.next_version, repo, part, comp),
                                             self._either(lib.next_version, repo, part, comp))
                with self.subTest(tags=tags, what="global"):
                    self.assertEqual(cli.latest_programs(repo), lib.latest_programs(repo))
                    self.assertEqual(cli.latest_versions(repo), lib.latest_versions(repo))
                    self.assertEqual(cli.build_number(repo), lib.build_number(repo))

    def test_pure_helpers_agree(self) -> None:
        import versioning.__main__ as cli
        import versioning.versioning as lib
        for v in ("0.9", "0.9b2", "1.3.4b1", "10.4.1", "0.10d", "NOVERSION", "2.0-rc1", ""):
            with self.subTest(v=v):
                self.assertEqual(cli.split_program_version(v), lib.split_program_version(v))
                self.assertEqual(cli._short_app(v), lib._short_app(v))
                self.assertEqual(cli.build_kind(v), lib.build_kind(v))
        order = ["0.9", "0.9a1", "0.9b1", "0.10a1", "1.0"]
        self.assertEqual(sorted(order, key=cli._program_key), sorted(order, key=lib._program_key))


class PartsAsDict(unittest.TestCase):
    def test_as_dict_includes_derived_fields(self) -> None:
        d = parse_canonical_version("0.9b3-70-9151ff8-dirty").as_dict()
        self.assertEqual((d["app_version"], d["release_version"], d["stage"], d["pre_number"],
                          d["rev_number"], d["dirty"], d["build_kind"]),
                         ("0.9b3", "0.9", "b", 3, 70, True, "b"))

    def test_release_as_dict(self) -> None:
        d = parse_canonical_version("0.9-70-9151ff8").as_dict()
        self.assertEqual((d["stage"], d["pre_number"], d["release_version"], d["build_kind"]),
                         (None, None, "0.9", ""))


class SwiftEmitter(unittest.TestCase):
    def _emit(self, repo: str, component=None) -> str:
        from versioning.emit_swift import emit
        return emit(repo, "CanonicalVersion", ("main", "master"), component)

    def test_prerelease_bakes_stage_and_tag_version(self) -> None:
        with _TempRepo("0.9b2") as repo:
            out = self._emit(repo)
            self.assertIn('static let buildKind = "b"', out)
            self.assertIn('static let buildSuffix = "b2"', out)
            self.assertIn('static let tagAppVersion = "0.9b2"', out)
            self.assertIn(r'"\(appVersion)\(buildSuffix)-\(revNumber)"', out)

    def test_ahead_of_beta_is_development(self) -> None:
        with _TempRepo("0.9b2") as repo:
            _commit(repo, "x")
            out = self._emit(repo)
            self.assertIn('buildKind = "d"', out)
            self.assertIn('buildSuffix = "d"', out)
            self.assertIn('tagAppVersion = "0.9d"', out)

    def test_release_is_blank_and_development_is_d(self) -> None:
        with _TempRepo("0.8") as repo:
            out = self._emit(repo)
            self.assertIn('buildKind = ""', out)
            self.assertIn('buildSuffix = ""', out)
            _commit(repo, "x")
            out = self._emit(repo)
            self.assertIn('buildKind = "d"', out)
            self.assertIn('tagAppVersion = "0.9d"', out)

    def test_component_lineage(self) -> None:
        with _TempRepo("srv-2.3a1", "cli-1.0") as repo:
            out = self._emit(repo, "srv")
            self.assertIn('buildKind = "a"', out)
            self.assertIn('buildSuffix = "a1"', out)
            self.assertIn('buildSuffix = ""', self._emit(repo, "cli"))

    def test_idempotent(self) -> None:
        with _TempRepo("0.9b2") as repo:
            self.assertEqual(self._emit(repo), self._emit(repo))

    def test_tag_lookup_stall_fails_the_emit(self) -> None:
        from unittest import mock
        real = subprocess.run

        def run(cmd, *a, **kw):
            if "tag" in cmd:
                raise subprocess.TimeoutExpired(cmd, 5.0)
            return real(cmd, *a, **kw)
        with _TempRepo("0.9b2") as repo, mock.patch("time.sleep"), \
                mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(GitError):
                self._emit(repo)

    @unittest.skipUnless(shutil.which("swiftc"), "swiftc not installed")
    def test_generated_swift_typechecks(self) -> None:
        with _TempRepo("0.9b2") as repo, tempfile.TemporaryDirectory() as d:
            f = Path(d) / "CanonicalVersion.swift"
            f.write_text(self._emit(repo))
            r = subprocess.run(["swiftc", "-typecheck", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class LifecycleSimulation(unittest.TestCase):
    """Walk a throwaway repo through a whole program with empty commits and pin
    the version at every step (the sequence we exercised by hand)."""

    def _commit(self, repo: str, n: int = 1) -> None:
        for _ in range(n):
            _git(repo, "commit", "-q", "--allow-empty", "-m", "c")

    def _tag(self, repo: str, kind: str) -> str:
        t = next_program_version(repo, kind)
        _git(repo, "tag", "-a", t, "-m", t)
        return t

    def test_alpha_to_release_and_beyond(self) -> None:
        with _TempRepo("0.8") as repo:                    # init commit, tagged 0.8
            steps = [
                # (action, expected short, tag created or None, expected build kind)
                (None,                 "0.8-1",     None,    ""),
                (("commit", 1),        "0.9d-2",    None,    "d"),
                (("commit", 1),        "0.9d-3",    None,    "d"),
                (("tag", "alpha"),     "0.9a1-3",   "0.9a1", "a"),   # tag on this commit
                (("commit", 1),        "0.9d-4",    None,    "d"),
                (("tag", "alpha"),     "0.9a2-4",   "0.9a2", "a"),
                (("commit", 1),        "0.9d-5",    None,    "d"),
                (("tag", "beta"),      "0.9b1-5",   "0.9b1", "b"),
                (("commit", 2),        "0.9d-7",    None,    "d"),
                (("tag", "beta"),      "0.9b2-7",   "0.9b2", "b"),
                (("commit", 1),        "0.9d-8",    None,    "d"),
                (("tag", "release"),   "0.9-8",     "0.9",   ""),
                (("commit", 1),        "0.10d-9",   None,    "d"),
            ]
            for action, want_short, want_tag, want_kind in steps:
                with self.subTest(action=action):
                    if action:
                        verb, arg = action
                        if verb == "commit":
                            self._commit(repo, arg)
                        else:
                            self.assertEqual(self._tag(repo, arg), want_tag)
                    f = _forms(repo)
                    self.assertEqual(f["short"], want_short)
                    # Every form and the CLI copy agree, and share the same M.m + seq.
                    self.assertEqual(f["short"], f["cli_short"])
                    self.assertEqual(f["server"], f["cli_server"])
                    self.assertEqual(short_from_canonical(f["server"]), f["short"])
                    self.assertEqual(short_from_canonical(f["client"]), f["short"])
                    self.assertEqual(f["client"], f["short"])  # clean client form == short
                    self.assertEqual(build_kind(f["short"].split("-")[0]), want_kind)
                    cli_kind = subprocess.run(
                        [sys.executable, "-m", "versioning", "--repo-root", repo, "--build-kind"],
                        capture_output=True, text=True, cwd=str(PROJECT_ROOT)).stdout
                    self.assertEqual(cli_kind, want_kind + "\n")

    def test_dirty_at_a_prerelease_tag(self) -> None:
        with _TempRepo("0.8") as repo:
            _git(repo, "tag", "-a", "0.9b1", "-m", "b")
            (Path(repo) / "a").write_text("wip\n")
            f = _forms(repo)
            self.assertEqual(f["short"], "0.9b1-1+")
            self.assertTrue(f["server"].endswith("-dirty"))
            self.assertEqual(short_from_canonical(f["server"]), "0.9b1-1+")

    def test_illegal_moves_leave_tags_untouched(self) -> None:
        with _TempRepo("0.9b1") as repo:
            before = _git(repo, "tag")
            for kind in ("alpha",):
                with self.assertRaises(ValueError):
                    next_program_version(repo, kind)
            self.assertEqual(_git(repo, "tag"), before)

    def test_two_tags_on_one_commit_report_the_highest(self) -> None:
        with _TempRepo("0.9b1", "0.9b2") as repo:
            self.assertEqual(_forms(repo)["short"], "0.9b2-1")


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
            self.assertEqual((r.returncode, r.stdout), (3, ""))
            self.assertIn("cannot go back", r.stderr)

    def test_bad_program_name_is_a_usage_error(self) -> None:
        with _TempRepo("0.9a1") as repo:
            self.assertEqual(self._run(repo, "--increment", "gamma").returncode, 2)

    def test_mutually_exclusive_with_other_modes(self) -> None:
        with _TempRepo("0.9a1") as repo:
            self.assertEqual(
                self._run(repo, "--increment", "beta", "--increment-minor").returncode, 2)

    def test_minor_over_a_prerelease_is_refused(self) -> None:
        # A minor bump would silently skip the program in flight: promote instead.
        with _TempRepo("0.8", "0.9b2") as repo:
            r = self._run(repo, "--increment-minor")
            self.assertEqual((r.returncode, r.stdout), (3, ""))
            self.assertIn("use `release`", r.stderr)
            self.assertEqual(self._run(repo, "--increment", "release").stdout.strip(), "0.9")

    def test_major_over_a_prerelease_is_allowed(self) -> None:
        with _TempRepo("0.8", "0.9b2") as repo:
            self.assertEqual(self._run(repo, "--increment-major").stdout.strip(), "1.0")

    def test_minor_major_unchanged_without_prereleases(self) -> None:
        with _TempRepo("2.3") as repo:
            self.assertEqual(self._run(repo, "--increment-minor").stdout.strip(), "2.4")
            self.assertEqual(self._run(repo, "--increment-major").stdout.strip(), "3.0")

    def test_exit_codes_are_distinct(self) -> None:
        with _TempRepo("0.9b1") as repo:
            illegal = self._run(repo, "--increment", "alpha").returncode
            usage = self._run(repo, "--increment", "gamma").returncode
        with tempfile.TemporaryDirectory() as d:
            notrepo = self._run(d, "--build-number").returncode
        self.assertEqual((notrepo, usage, illegal), (1, 2, 3))

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
                          ("beta", "comp-0.9b1"), ("release", "comp-0.9")):
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
        for bad in ("gamma", "rc", "d"):
            with self.subTest(bad=bad):
                self.assertEqual(self._release(work, "comp", bad).returncode, 2)


if __name__ == "__main__":
    unittest.main()
