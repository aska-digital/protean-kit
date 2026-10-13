#!/usr/bin/env python3
"""Tests for the composition lock, the installer, and the two new gates.

Everything runs offline against fixture repositories built in a temporary
directory: a real git repository, an annotated tag, a warm cache entry, and a
lock file. No network access, no third-party import.

Run: python3 -m unittest tests/test_kit_lock.py
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "build", "install.py")
CHECK_LOCK = os.path.join(ROOT, "build", "check-lock.py")
CHECK_CONTRACT = os.path.join(ROOT, "build", "check-ingredient-contract.py")
CONTRACT = "The fixture capability: proves the composition contract end to end."


def load_install_module():
    spec = importlib.util.spec_from_file_location("kit_install", INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL_MODULE = load_install_module()


def git(args, cwd):
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, result.stderr))
    return result.stdout.strip()


IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]


def build_ingredient(base, slug, version="1.0.0", gate_exit=0, requires=None,
                     recommends=None, install_targets=None, contract=CONTRACT,
                     tree_tweak=None, extra_files=None):
    """A real fixture ingredient repository with an annotated tag."""
    path = os.path.join(base, slug)
    os.makedirs(path, exist_ok=True)
    git(["init", "--quiet", "-b", "main", path], cwd=base)
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(contract + "\n\n# " + slug + "\n\nFixture ingredient for the installer tests.\n")
    with open(os.path.join(path, "LICENSE"), "w", encoding="utf-8") as fh:
        fh.write("MIT License\n\nCopyright (c) 2026 ASK\n")
    targets = install_targets or ["payload/" + slug, "gates/" + slug]
    os.makedirs(os.path.join(path, targets[0]), exist_ok=True)
    with open(os.path.join(path, targets[0], "file.txt"), "w", encoding="utf-8") as fh:
        fh.write("payload for " + slug + "\n")
    os.makedirs(os.path.join(path, "tests"), exist_ok=True)
    with open(os.path.join(path, "tests", "test_ok.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_ok():\n    assert True\n")
    os.makedirs(os.path.join(path, "gates", slug), exist_ok=True)
    with open(os.path.join(path, "gates", slug, "check-fixture.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\n\nprint('fixture gate')\nsys.exit(%d)\n" % gate_exit)
    with open(os.path.join(path, "install.sh"), "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\nset -euo pipefail\necho fixture\n")
    descriptor = {
        "slug": slug,
        "version": version,
        "contract": contract,
        "kind": "skills",
        "entrypoint": "install.sh",
        "installTargets": targets,
        "gates": [{"name": "fixture", "cmd": "python3 gates/" + slug + "/check-fixture.py", "cwd": ".",
                   "postInstall": True}],
        "requires": requires or [],
        "recommends": recommends or [],
        "runtime": {"python": ">=3.9", "bin": ["git"]},
        "license": "MIT",
        "open": [],
    }
    with open(os.path.join(path, "protean-ingredient.json"), "w", encoding="utf-8") as fh:
        json.dump(descriptor, fh, indent=2)
    if tree_tweak:
        with open(os.path.join(path, tree_tweak), "w", encoding="utf-8") as fh:
            fh.write("extra\n")
    for rel, content in (extra_files or {}).items():
        dest = os.path.join(path, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    git(["add", "-A"], cwd=path)
    git(IDENTITY + ["commit", "--quiet", "-m", "%s %s" % (slug, version)], cwd=path)
    git(IDENTITY + ["tag", "-a", "v" + version, "-m", "%s %s" % (slug, version)], cwd=path)
    sha = git(["rev-parse", "HEAD"], cwd=path)
    return {"path": path, "sha": sha, "targets": targets, "descriptor": descriptor}


def warm_cache(cache, slug, fixture):
    entry = os.path.join(cache, slug, fixture["sha"])
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", fixture["path"], entry], check=True,
                   capture_output=True)
    git(["checkout", "--quiet", "--detach", fixture["sha"]], cwd=entry)
    return entry


def lock_entry(slug, fixture, cache=None):
    tree = INSTALL_MODULE.tree_sha256(fixture["path"], fixture["sha"])
    return {
        "slug": slug,
        "repo": "https://github.com/aska-digital/" + slug,
        "ref": "refs/tags/v1.0.0",
        "sha": fixture["sha"],
        "version": "1.0.0",
        "contract": CONTRACT,
        "kind": "skills",
        "installTargets": fixture["targets"],
        "requires": fixture["descriptor"]["requires"],
        "recommends": fixture["descriptor"]["recommends"],
        "integrity": {"fileCount": 0, "treeSha256": tree, "algorithm": "protean-tree-v1"},
        "gates": fixture["descriptor"]["gates"],
    }


def write_lock(path, entries, order=None, **overrides):
    lock = {
        "lockVersion": 1,
        "kit": {"name": "protean-kit", "version": "2.0.0", "tag": "v2.0.0"},
        "generatedAt": "2026-09-17T00:00:00Z",
        "allowedHosts": ["github.com"],
        "allowedOwners": ["aska-digital"],
        "installOrder": order or [e["slug"] for e in entries],
        "ingredients": entries,
    }
    lock.update(overrides)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(lock, fh, indent=2)
    return path


def run(script, *args, cwd=None, env=None):
    environment = dict(os.environ)
    if env:
        environment.update(env)
    return subprocess.run([sys.executable, script] + [str(a) for a in args],
                          capture_output=True, text=True, cwd=cwd or ROOT,
                          env=environment, timeout=300)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.repos = os.path.join(self.base, "repos")
        self.cache = os.path.join(self.base, "cache")
        os.makedirs(self.repos)
        os.makedirs(self.cache)
        self.addCleanup(self._tmp.cleanup)

    def ingredient(self, slug, **kwargs):
        return build_ingredient(self.repos, slug, **kwargs)


class TestTreeHash(FixtureCase):
    def test_tree_hash_is_deterministic(self):
        fixture = self.ingredient("protean-one")
        first = INSTALL_MODULE.tree_sha256(fixture["path"], fixture["sha"])
        second = INSTALL_MODULE.tree_sha256(fixture["path"], fixture["sha"])
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_tree_hash_changes_with_content(self):
        fixture = self.ingredient("protean-one")
        before = INSTALL_MODULE.tree_sha256(fixture["path"], fixture["sha"])
        with open(os.path.join(fixture["path"], "README.md"), "a", encoding="utf-8") as fh:
            fh.write("more\n")
        git(["add", "-A"], cwd=fixture["path"])
        git(IDENTITY + ["commit", "--quiet", "-m", "change"], cwd=fixture["path"])
        after = INSTALL_MODULE.tree_sha256(fixture["path"], git(["rev-parse", "HEAD"],
                                                               cwd=fixture["path"]))
        self.assertNotEqual(before, after)


class TestOrderAndClosure(FixtureCase):
    def test_topological_order_breaks_ties_by_slug(self):
        order = INSTALL_MODULE.topo_order(
            {"protean-b", "protean-a", "protean-c"},
            {"protean-a": [], "protean-b": ["protean-a"], "protean-c": ["protean-a", "protean-b"]})
        self.assertEqual(order.index("protean-a"), 0)
        self.assertEqual(order.index("protean-c"), 2)

    def test_cycle_is_a_dependency_failure(self):
        try:
            INSTALL_MODULE.topo_order({"protean-a", "protean-b"},
                                      {"protean-a": ["protean-b"], "protean-b": ["protean-a"]})
        except INSTALL_MODULE.Failure as failure:
            self.assertEqual(failure.code, 3)
            self.assertIn("cycle", failure.message)
        else:
            self.fail("cycle was not detected")

    def test_closure_follows_requires_only(self):
        seen = {"protean-a": {"slug": "protean-a", "requires": [], "recommends": ["protean-z"]},
                "protean-b": {"slug": "protean-b", "requires": ["protean-a"], "recommends": []},
                "protean-z": {"slug": "protean-z", "requires": [], "recommends": []}}
        lock = {"ingredients": [seen["protean-a"], seen["protean-b"], seen["protean-z"]]}
        opts = {"all": False, "ingredients": ["protean-b"], "no_deps": False}
        selection, closure, _degraded = INSTALL_MODULE.resolve_selection(lock, seen, opts)
        self.assertEqual(selection, ["protean-b"])
        self.assertEqual(sorted(closure), ["protean-a", "protean-b"])


class TestEndToEndInstall(FixtureCase):
    def fixture_lock(self, **kwargs):
        fixture = self.ingredient("protean-one", **kwargs)
        warm_cache(self.cache, "protean-one", fixture)
        entry = lock_entry("protean-one", fixture)
        return fixture, write_lock(os.path.join(self.base, "kit.lock.json"), [entry])

    def test_offline_warm_cache_installs_and_reads_back(self):
        fixture, lock = self.fixture_lock()
        target = os.path.join(self.base, "installed")
        result = run(INSTALL, "--all", "--target", target, "--offline", "--cache", self.cache,
                     "--lock", lock)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("readback", result.stdout)
        self.assertTrue(os.path.isfile(os.path.join(target, "payload", "protean-one", "file.txt")))
        self.assertFalse(os.path.isdir(os.path.join(target, ".protean-staging")))

    def test_tampered_cache_is_refused_before_any_target_write(self):
        for mutation in ("tracked", "untracked", "ignored", "hidden-tracked"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as base:
                os.makedirs(os.path.join(base, "repos"))
                cache = os.path.join(base, "cache")
                fixture = build_ingredient(os.path.join(base, "repos"), "protean-one")
                cached = warm_cache(cache, "protean-one", fixture)
                lock = write_lock(os.path.join(base, "kit.lock.json"),
                                  [lock_entry("protean-one", fixture)])
                payload = os.path.join(cached, "payload", "protean-one", "file.txt")
                if mutation == "hidden-tracked":
                    git(["update-index", "--skip-worktree", "payload/protean-one/file.txt"], cwd=cached)
                if mutation in ("tracked", "hidden-tracked"):
                    with open(payload, "w", encoding="utf-8") as handle:
                        handle.write("TAMPERED CACHE PAYLOAD\n")
                else:
                    name = "injected.txt" if mutation == "untracked" else "injected.pyc"
                    if mutation == "ignored":
                        with open(os.path.join(cached, ".git", "info", "exclude"), "a") as handle:
                            handle.write("*.pyc\n")
                    with open(os.path.join(os.path.dirname(payload), name), "w") as handle:
                        handle.write("UNPINNED CACHE PAYLOAD\n")
                target = os.path.join(base, "installed")
                result = run(INSTALL, "--all", "--target", target, "--offline", "--cache", cache,
                             "--lock", lock)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("cache", result.stderr)
                self.assertFalse(os.path.exists(target), "integrity failure must precede writes")

    def test_pinned_symlink_cannot_copy_unpinned_external_bytes(self):
        fixture = self.ingredient("protean-one")
        outside = os.path.join(self.base, "outside-marker.txt")
        with open(outside, "w") as handle:
            handle.write("UNPINNED EXTERNAL MARKER\n")
        os.symlink(outside, os.path.join(fixture["path"], "payload", "protean-one", "linked.txt"))
        git(["add", "payload/protean-one/linked.txt"], cwd=fixture["path"])
        git(IDENTITY + ["commit", "--quiet", "-m", "synthetic symlink"], cwd=fixture["path"])
        fixture["sha"] = git(["rev-parse", "HEAD"], cwd=fixture["path"])
        git(IDENTITY + ["tag", "-a", "v1.0.1", "-m", "synthetic symlink fixture"], cwd=fixture["path"])
        warm_cache(self.cache, "protean-one", fixture)
        entry = lock_entry("protean-one", fixture)
        entry.update(ref="refs/tags/v1.0.1", version="1.0.1")
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), [entry])
        target = os.path.join(self.base, "installed")
        result = run(INSTALL, "--all", "--target", target, "--offline", "--cache", self.cache,
                     "--lock", lock)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("symlink", result.stderr)
        self.assertFalse(os.path.exists(target))
        with open(outside) as handle:
            self.assertEqual(handle.read(), "UNPINNED EXTERNAL MARKER\n")

    def test_single_ingredient_dry_run_writes_nothing(self):
        fixture, lock = self.fixture_lock()
        target = os.path.join(self.base, "dry-target")
        result = run(INSTALL, "--ingredient", "protean-one", "--target", target, "--dry-run",
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no files written", result.stdout)
        self.assertFalse(os.path.exists(target))

    def test_cold_cache_offline_is_exit_4(self):
        fixture = self.ingredient("protean-one")
        entry = lock_entry("protean-one", fixture)
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), [entry])
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"), "--offline",
                     "--cache", os.path.join(self.base, "empty-cache"), "--lock", lock)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("offline cache unavailable", result.stderr)

    def test_tampered_tree_hash_is_exit_4(self):
        fixture = self.ingredient("protean-one")
        warm_cache(self.cache, "protean-one", fixture)
        entry = lock_entry("protean-one", fixture)
        entry["integrity"]["treeSha256"] = "0" * 64
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), [entry])
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"), "--offline",
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("treeSha256 mismatch", result.stderr)

    def test_failing_post_install_gate_is_exit_5(self):
        fixture = self.ingredient("protean-one", gate_exit=1)
        warm_cache(self.cache, "protean-one", fixture)
        entry = lock_entry("protean-one", fixture)
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), [entry])
        target = os.path.join(self.base, "t")
        result = run(INSTALL, "--all", "--target", target, "--offline", "--cache", self.cache,
                     "--lock", lock)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("gate failed in place", result.stderr)
        self.assertFalse(os.path.isfile(os.path.join(target, "payload", "protean-one", "file.txt")),
                         "a failed install promoted files")

    def test_branch_ref_is_exit_2(self):
        fixture, lock = self.fixture_lock()
        lock_data = json.load(open(lock, encoding="utf-8"))
        lock_data["ingredients"][0]["ref"] = "refs/heads/main"
        with open(lock, "w", encoding="utf-8") as fh:
            json.dump(lock_data, fh)
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"),
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_off_allowlist_owner_is_exit_2(self):
        fixture, lock = self.fixture_lock()
        lock_data = json.load(open(lock, encoding="utf-8"))
        lock_data["ingredients"][0]["repo"] = "https://github.com/someone-else/protean-one"
        with open(lock, "w", encoding="utf-8") as fh:
            json.dump(lock_data, fh)
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"),
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_duplicate_slug_is_exit_2(self):
        fixture, lock = self.fixture_lock()
        lock_data = json.load(open(lock, encoding="utf-8"))
        lock_data["ingredients"].append(dict(lock_data["ingredients"][0]))
        with open(lock, "w", encoding="utf-8") as fh:
            json.dump(lock_data, fh)
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"),
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_unknown_flag_is_exit_1(self):
        result = run(INSTALL, "--nonsense")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown flag", result.stderr)

    def test_unknown_slug_is_exit_1(self):
        fixture, lock = self.fixture_lock()
        result = run(INSTALL, "--ingredient", "protean-absent", "--target",
                     os.path.join(self.base, "t"), "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 1)

    def test_missing_closure_member_is_exit_3(self):
        fixture = self.ingredient("protean-one", requires=["protean-absent"])
        warm_cache(self.cache, "protean-one", fixture)
        entry = lock_entry("protean-one", fixture)
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), [entry])
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"), "--offline",
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_requires_cycle_is_exit_3(self):
        one = self.ingredient("protean-one")
        two = self.ingredient("protean-two")
        entries = [lock_entry("protean-one", one), lock_entry("protean-two", two)]
        entries[0]["requires"] = ["protean-two"]
        entries[1]["requires"] = ["protean-one"]
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), entries)
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"), "--offline",
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("cycle", result.stderr)

    def test_no_deps_reports_degraded_without_failing(self):
        one = self.ingredient("protean-one")
        two = self.ingredient("protean-two", requires=["protean-one"])
        warm_cache(self.cache, "protean-one", one)
        warm_cache(self.cache, "protean-two", two)
        entries = [lock_entry("protean-one", one), lock_entry("protean-two", two)]
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), entries,
                          order=["protean-one", "protean-two"])
        target = os.path.join(self.base, "t")
        result = run(INSTALL, "--ingredient", "protean-two", "--no-deps", "--target", target,
                     "--offline", "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("degraded", result.stdout)
        self.assertFalse(os.path.isdir(os.path.join(target, "payload", "protean-one")),
                         "degraded mode installed a requirement")

    def test_install_target_collision_is_exit_2(self):
        one = self.ingredient("protean-one")
        two = self.ingredient("protean-two", install_targets=["payload/shared"])
        entries = [lock_entry("protean-one", one), lock_entry("protean-two", two)]
        entries[0]["installTargets"] = ["payload/shared"]
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), entries)
        result = run(INSTALL, "--all", "--target", os.path.join(self.base, "t"), "--offline",
                     "--cache", self.cache, "--lock", lock)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("collision", result.stderr)

    def test_read_back_detects_a_difference(self):
        fixture, lock = self.fixture_lock()
        target = os.path.join(self.base, "installed")
        run(INSTALL, "--all", "--target", target, "--offline", "--cache", self.cache, "--lock", lock)
        entry_dir = os.path.join(self.cache, "protean-one", fixture["sha"])
        rel = os.path.join("payload", "protean-one", "file.txt")
        with open(os.path.join(target, rel), "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
        mismatches = INSTALL_MODULE.read_back(entry_dir, target, [rel])
        self.assertEqual(len(mismatches), 1)

    def test_composer_tree_violations_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitmodules"), "w", encoding="utf-8") as fh:
                fh.write("[submodule]\n")
            os.makedirs(os.path.join(tmp, "vendor"))
            problems = INSTALL_MODULE.composer_tree_violations(tmp)
            self.assertEqual(len(problems), 2)


class TestGates(FixtureCase):
    def fixture_lock(self, slugs=("protean-one",)):
        entries = []
        for slug in slugs:
            fixture = self.ingredient(slug)
            warm_cache(self.cache, slug, fixture)
            entries.append(lock_entry(slug, fixture))
        return entries, write_lock(os.path.join(self.base, "kit.lock.json"), entries)

    def test_check_lock_passes_and_verifies_pins(self):
        entries, lock = self.fixture_lock()
        result = run(CHECK_LOCK, lock, "--cache", self.cache, "--verify", "--offline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-lock: PASS", result.stdout)

    def test_check_lock_reports_drifted_tree_hash(self):
        entries, lock = self.fixture_lock()
        data = json.load(open(lock, encoding="utf-8"))
        data["ingredients"][0]["integrity"]["treeSha256"] = "1" * 64
        with open(lock, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        result = run(CHECK_LOCK, lock, "--cache", self.cache, "--verify", "--offline")
        self.assertEqual(result.returncode, 1)
        self.assertIn("treeSha256 mismatch", result.stdout)

    def test_check_lock_reports_cycle(self):
        one = self.ingredient("protean-one")
        two = self.ingredient("protean-two")
        entries = [lock_entry("protean-one", one), lock_entry("protean-two", two)]
        entries[0]["requires"] = ["protean-two"]
        entries[1]["requires"] = ["protean-one"]
        lock = write_lock(os.path.join(self.base, "kit.lock.json"), entries)
        result = run(CHECK_LOCK, lock, "--cache", self.cache)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cycle", result.stdout)

    def test_check_ingredient_contract_passes(self):
        entries, lock = self.fixture_lock()
        result = run(CHECK_CONTRACT, "--lock", lock, "--cache", self.cache, "--verify", "--offline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-ingredient-contract: PASS", result.stdout)

    def test_check_ingredient_contract_reports_a_bad_readme(self):
        entries, lock = self.fixture_lock()
        entry_dir = os.path.join(self.cache, "protean-one", entries[0]["sha"])
        with open(os.path.join(entry_dir, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("A different first line\n")
        result = run(CHECK_CONTRACT, "--lock", lock, "--cache", self.cache, "--verify", "--offline")
        self.assertEqual(result.returncode, 1)
        self.assertIn("README first line is not the contract line", result.stdout)

    def test_check_ingredient_contract_reports_a_missing_license(self):
        entries, lock = self.fixture_lock()
        entry_dir = os.path.join(self.cache, "protean-one", entries[0]["sha"])
        os.remove(os.path.join(entry_dir, "LICENSE"))
        result = run(CHECK_CONTRACT, "--lock", lock, "--cache", self.cache, "--verify", "--offline")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LICENSE is missing", result.stdout)

    def test_check_ingredient_contract_reports_unverified_without_trees(self):
        entries, lock = self.fixture_lock()
        result = run(CHECK_CONTRACT, "--lock", lock,
                     "--cache", os.path.join(self.base, "empty-cache"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unverified pins", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
