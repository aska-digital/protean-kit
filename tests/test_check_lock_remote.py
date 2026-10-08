#!/usr/bin/env python3
"""Regression tests for check-lock.py remote_tag_sha (warm-cache --verify).

An annotated tag has two shas: the tag object and the peeled commit. The
warm-cache / ingredients-dir re-resolution branch of --verify must compare
the pinned sha against the *peeled commit* returned by `git ls-remote`.
A ref-pattern-only ls-remote omits the peeled ^{} line, which used to make
--verify report a LOCK VIOLATION for every current annotated-tag pin.

Everything runs offline: `git ls-remote` is faked at the check-lock run()
boundary while all other git operations run for real against fixture
repositories. No network access, no third-party import.

Run from the repo root: python3 -m unittest discover
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_LOCK = os.path.join(ROOT, "build", "check-lock.py")
CONTRACT = "The fixture capability: proves the composition contract end to end."


def load_check_lock():
    spec = importlib.util.spec_from_file_location("kit_check_lock", CHECK_LOCK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_check_lock()

IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]


def git(args, cwd):
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, result.stderr))
    return result.stdout.strip()


def ok(result):
    return types.SimpleNamespace(returncode=0, stdout=result, stderr="")


class RemoteTagShaCase(unittest.TestCase):
    """Unit tests for remote_tag_sha with a faked ls-remote transport."""

    def resolve(self, stdout, ref="refs/tags/v1.0.0", record=None):
        real_run = CHECK.run

        def fake(cmd, cwd=None):
            if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "ls-remote":
                if record is not None:
                    record.append(cmd)
                return ok(stdout)
            return real_run(cmd, cwd=cwd)

        old = CHECK.run
        CHECK.run = fake
        try:
            return CHECK.remote_tag_sha("https://github.com/aska-digital/x", ref)
        finally:
            CHECK.run = old

    def test_annotated_tag_returns_peeled_commit_not_tag_object(self):
        tag_object = "a" * 40
        commit = "b" * 40
        out = "%s\trefs/tags/v1.0.0\n%s\trefs/tags/v1.0.0^{}\n" % (tag_object, commit)
        self.assertEqual(self.resolve(out), commit)

    def test_annotated_tag_prefers_peeled_line_whatever_the_order(self):
        tag_object = "a" * 40
        commit = "b" * 40
        out = "%s\trefs/tags/v1.0.0^{}\n%s\trefs/tags/v1.0.0\n" % (commit, tag_object)
        self.assertEqual(self.resolve(out), commit)

    def test_ls_remote_is_asked_for_the_peeled_ref(self):
        record = []
        self.resolve("c" * 40 + "\trefs/tags/v1.0.0\n", record=record)
        self.assertEqual(len(record), 1)
        self.assertIn("refs/tags/v1.0.0^{}", record[0],
                      "ls-remote must request the peeled ^{} ref or annotated "
                      "tags resolve to the tag object")

    def test_lightweight_tag_without_peeled_line_still_resolves(self):
        sha = "c" * 40
        self.assertEqual(self.resolve("%s\trefs/tags/v1.0.0\n" % sha), sha)

    def test_unresolvable_ref_returns_none(self):
        real_run = CHECK.run
        CHECK.run = lambda cmd, cwd=None: types.SimpleNamespace(
            returncode=128, stdout="", stderr="no such ref")
        try:
            self.assertIsNone(CHECK.remote_tag_sha("https://example.invalid/r", "refs/tags/v9.9.9"))
        finally:
            CHECK.run = real_run
        self.assertIsNone(self.resolve(""))


class WarmCacheVerifyCase(unittest.TestCase):
    """End-to-end warm-cache --verify against a fixture annotated tag.

    ls-remote is faked to serve the fixture repository's real tag lines;
    every other git call (rev-parse, ls-tree, clone) runs for real.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.repos = os.path.join(self.base, "repos")
        self.cache = os.path.join(self.base, "cache")
        os.makedirs(self.repos)
        os.makedirs(self.cache)
        self.addCleanup(self._tmp.cleanup)
        self.ls_calls = []

    def make_fixture(self, slug="protean-one"):
        path = os.path.join(self.repos, slug)
        os.makedirs(path)
        git(["init", "--quiet", "-b", "main", path], cwd=self.repos)
        with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(CONTRACT + "\n\n# %s\n" % slug)
        with open(os.path.join(path, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write("MIT License\n")
        os.makedirs(os.path.join(path, "payload", slug))
        with open(os.path.join(path, "payload", slug, "file.txt"), "w", encoding="utf-8") as fh:
            fh.write("payload for %s\n" % slug)
        descriptor = {
            "slug": slug, "version": "1.0.0", "contract": CONTRACT, "kind": "skills",
            "entrypoint": "install.sh", "installTargets": ["payload/" + slug],
            "gates": [], "requires": [], "recommends": [],
            "runtime": {"python": ">=3.9", "bin": ["git"]},
            "license": "MIT", "open": [],
        }
        with open(os.path.join(path, "protean-ingredient.json"), "w", encoding="utf-8") as fh:
            json.dump(descriptor, fh, indent=2)
        git(["add", "-A"], cwd=path)
        git(IDENTITY + ["commit", "--quiet", "-m", "v1"], cwd=path)
        git(IDENTITY + ["tag", "-a", "v1.0.0", "-m", "v1.0.0"], cwd=path)
        sha = git(["rev-parse", "HEAD"], cwd=path)
        tree = CHECK.tree_sha256(path, sha)
        return {"path": path, "sha": sha, "tree": tree, "descriptor": descriptor}

    def warm_cache(self, slug, fixture):
        entry = os.path.join(self.cache, slug, fixture["sha"])
        os.makedirs(os.path.dirname(entry), exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", fixture["path"], entry], check=True,
                       capture_output=True)
        git(["checkout", "--quiet", "--detach", fixture["sha"]], cwd=entry)
        return entry

    def write_lock(self, slug, fixture):
        lock = {
            "lockVersion": 1,
            "kit": {"name": "protean-kit", "version": "2.0.0", "tag": "v2.0.0"},
            "generatedAt": "2026-09-17T00:00:00Z",
            "allowedHosts": ["github.com"],
            "allowedOwners": ["aska-digital"],
            "installOrder": [slug],
            "ingredients": [{
                "slug": slug,
                "repo": "https://github.com/aska-digital/" + slug,
                "ref": "refs/tags/v1.0.0",
                "sha": fixture["sha"],
                "version": "1.0.0",
                "contract": CONTRACT,
                "kind": "skills",
                "installTargets": ["payload/" + slug],
                "requires": [], "recommends": [],
                "integrity": {"fileCount": 0, "treeSha256": fixture["tree"],
                              "algorithm": "protean-tree-v1"},
                "gates": [],
            }],
        }
        path = os.path.join(self.base, "kit.lock.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(lock, fh, indent=2)
        return path

    def ls_remote_lines(self, fixture_path):
        """Real ls-remote-shaped output for the fixture's annotated tag."""
        tag_object = git(["rev-parse", "v1.0.0"], cwd=fixture_path)
        commit = git(["rev-parse", "v1.0.0^{commit}"], cwd=fixture_path)
        return "%s\trefs/tags/v1.0.0\n%s\trefs/tags/v1.0.0^{}\n" % (tag_object, commit)

    def run_verify(self, lock, ls_stdout):
        real_run = CHECK.run

        def fake(cmd, cwd=None):
            if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "ls-remote":
                self.ls_calls.append(cmd)
                return ok(ls_stdout)
            return real_run(cmd, cwd=cwd)

        old_run, old_argv = CHECK.run, sys.argv
        CHECK.run = fake
        sys.argv = ["check-lock.py", lock, "--cache", self.cache, "--verify"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = CHECK.main()
        finally:
            CHECK.run = old_run
            sys.argv = old_argv
        return code, buf.getvalue()

    def test_current_annotated_tag_passes_warm_cache_verify(self):
        fixture = self.make_fixture()
        self.warm_cache("protean-one", fixture)
        lock = self.write_lock("protean-one", fixture)
        code, out = self.run_verify(lock, self.ls_remote_lines(fixture["path"]))
        self.assertEqual(code, 0, out)
        self.assertIn("check-lock: PASS", out)
        self.assertIn("[provenance: remote]", out)

    def test_moved_tag_fails_closed_on_warm_cache_verify(self):
        fixture = self.make_fixture()
        self.warm_cache("protean-one", fixture)
        lock = self.write_lock("protean-one", fixture)
        # Move the published tag to a new commit; the lock still pins the old one.
        with open(os.path.join(fixture["path"], "payload", "protean-one", "file.txt"),
                  "a", encoding="utf-8") as fh:
            fh.write("upstream change\n")
        git(["add", "-A"], cwd=fixture["path"])
        git(IDENTITY + ["commit", "--quiet", "-m", "v2"], cwd=fixture["path"])
        git(IDENTITY + ["tag", "-f", "-a", "v1.0.0", "-m", "v1.0.0 moved"], cwd=fixture["path"])
        code, out = self.run_verify(lock, self.ls_remote_lines(fixture["path"]))
        self.assertEqual(code, 1, out)
        self.assertIn("resolves on the remote to", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
