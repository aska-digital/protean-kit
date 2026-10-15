"""Offline target-boundary regressions using real pinned ingredient fixtures."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_kit_lock import FixtureCase, INSTALL, lock_entry, run, warm_cache, write_lock

INSTALL = os.environ.get("INSTALL_SOURCE", INSTALL)


class TestTargetConfinement(FixtureCase):
    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.base, "target")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(self.target)
        os.makedirs(self.outside)
        self.canary = os.path.join(self.outside, "canary")
        with open(self.canary, "w") as fh:
            fh.write("outside user bytes")

    def lock(self, slugs=("protean-one",), **kwargs):
        entries = []
        for slug in slugs:
            fixture = self.ingredient(slug, **kwargs)
            warm_cache(self.cache, slug, fixture)
            entries.append(lock_entry(slug, fixture))
        return write_lock(os.path.join(self.base, "kit.lock.json"), entries)

    def install(self, lock, target=None):
        return run(INSTALL, "--all", "--target", target or self.target, "--offline",
                   "--cache", self.cache, "--lock", lock)

    def refused(self, result):
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        with open(self.canary) as fh:
            self.assertEqual(fh.read(), "outside user bytes")
        self.assertEqual(os.listdir(self.outside), ["canary"])
        self.assertFalse(os.path.exists(os.path.join(self.target, "gates")))

    def test_payload_parent_escape(self):
        os.symlink(self.outside, os.path.join(self.target, "payload"))
        self.refused(self.install(self.lock()))

    def test_payload_leaf_escape(self):
        parent = os.path.join(self.target, "payload", "protean-one")
        os.makedirs(parent)
        os.symlink(self.canary, os.path.join(parent, "file.txt"))
        self.refused(self.install(self.lock()))

    def test_staging_root_escape(self):
        os.symlink(self.outside, os.path.join(self.target, ".protean-staging"))
        self.refused(self.install(self.lock()))

    def test_unused_staging_cleanup_child_escape(self):
        staging = os.path.join(self.target, ".protean-staging")
        os.makedirs(staging)
        os.symlink(self.outside, os.path.join(staging, "old-run"))
        self.refused(self.install(self.lock()))

    def test_dangling_contribution_state_escape(self):
        os.makedirs(os.path.join(self.target, "records"))
        dest = os.path.join(self.outside, "new-record")
        os.symlink(dest, os.path.join(self.target, "records", "CONTRIB-STATE.md"))
        self.refused(self.install(self.lock(extra_files={"records/CONTRIB-STATE.md.tmpl": "template"},
                                             install_targets=["records", "gates/protean-one"])))
        self.assertFalse(os.path.exists(dest))

    def test_existing_contribution_state_escape(self):
        os.makedirs(os.path.join(self.target, "records"))
        os.symlink(self.canary, os.path.join(self.target, "records", "CONTRIB-STATE.md"))
        self.refused(self.install(self.lock()))

    def test_later_unsafe_ingredient_does_not_partially_install(self):
        os.makedirs(os.path.join(self.target, "payload"))
        os.symlink(self.outside, os.path.join(self.target, "payload", "protean-two"))
        self.refused(self.install(self.lock(("protean-one", "protean-two"))))
        self.assertFalse(os.path.exists(os.path.join(self.target, "payload", "protean-one")))

    def test_explicit_root_link_is_allowed(self):
        link = os.path.join(self.base, "chosen-root")
        os.symlink(self.target, link)
        result = self.install(self.lock(), link)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "payload", "protean-one", "file.txt")))

    def test_confined_payload_parent_link_is_allowed(self):
        internal = os.path.join(self.target, "internal")
        os.makedirs(internal)
        os.symlink(internal, os.path.join(self.target, "payload"))
        result = self.install(self.lock())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(internal, "protean-one", "file.txt")))

    def test_normal_install(self):
        result = self.install(self.lock())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
