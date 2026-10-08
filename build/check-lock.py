#!/usr/bin/env python3
"""check-lock.py - lock validity gate for the Protean Kit composition manifest.

Validates kit.lock.json and every pin it records:

  schema          required fields, types, lockVersion
  slugs           unique, well formed
  refs            annotated tag refs only (refs/tags/vX.Y.Z), never a branch,
                  and the ref must name the declared version
  allowlist       repo host and owner inside allowedHosts / allowedOwners
  closure         every requires edge resolves inside the same lock
  cycles          a requires cycle is a violation and the cycle path is printed
  collisions      two ingredients may not claim the same install target
  contract parity the lock's contract line equals the ingredient descriptor's
  install plan    the lock's installTargets equal the descriptor's
  tree hashes     recomputed for every pin whose tree is available

Modes:

  --static (default)   no network. Verifies everything derivable from the lock,
                       plus parity against ingredient trees that are already
                       present in the cache or an ingredients directory.
  --verify             requires every pin's tree to be present (fetching it from
                       the pinned tag when needed), re-resolves every tag against
                       the remote, and recomputes the sha pairing and treeSha256
                       for each one. Each pin's verification provenance (remote
                       tag pairing or local tree only) is reported; a moved tag
                       or a hash mismatch is a violation.

Usage:
  check-lock.py [LOCK] [--cache DIR] [--ingredients-dir DIR] [--verify] [--offline]
Exit: 0 pass; 1 violation; 2 missing or unparseable input.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

REF_RE = re.compile(r"^refs/tags/v\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
ALGORITHM = "protean-tree-v1"


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def tree_sha256(repo_dir, commit):
    result = run(["git", "ls-tree", "-r", "--full-tree", commit], cwd=repo_dir)
    if result.returncode != 0:
        return None
    lines = []
    for line in result.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, _kind, blob = parts
        lines.append((mode + " " + path).encode("utf-8") + b"\x00"
                     + blob.encode("utf-8") + b"\n")
    lines.sort(key=lambda raw: raw.split(b"\x00", 1)[0])
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line)
    return digest.hexdigest()


def topo_order(slugs, requires):
    """Topological order over requires, ascending slug as the stable tiebreak."""
    members = set(slugs)
    order, remaining = [], set(members)
    while remaining:
        ready = sorted(slug for slug in remaining
                       if not any(dep in remaining for dep in requires.get(slug, [])
                                  if dep in members))
        if not ready:
            return None, " -> ".join(sorted(remaining))
        order.append(ready[0])
        remaining.discard(ready[0])
    return order, None


def order_is_valid(order, slugs, requires):
    members = set(slugs)
    if set(order) != members or len(order) != len(members):
        return False
    placed = set()
    for slug in order:
        for dep in requires.get(slug, []):
            if dep in members and dep not in placed:
                return False
        placed.add(slug)
    return True


def descriptor_of(pin, cache, ingredients_dir):
    candidates = []
    if ingredients_dir:
        candidates.append(os.path.join(ingredients_dir, pin["slug"]))
    candidates.append(os.path.join(cache, pin["slug"], pin["sha"]))
    for cand in candidates:
        path = os.path.join(cand, "protean-ingredient.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), cand
    return None, None


def remote_tag_sha(repo, ref):
    """Resolve a tag ref against the remote. Returns the peeled commit sha,
    or None when the ref cannot be resolved."""
    result = run(["git", "ls-remote", repo, ref])
    if result.returncode != 0:
        return None
    tag_sha = None
    for line in result.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref + "^{":
            return sha
        if name == ref:
            tag_sha = sha
    return tag_sha


def main():
    parser = argparse.ArgumentParser(
        prog="check-lock.py",
        description="Validate the Protean Kit composition lock and its pins.",
        epilog="exit codes: 0 pass, 1 violation, 2 missing or unparseable input.")
    parser.add_argument("lock", nargs="?", default="kit.lock.json")
    parser.add_argument("--cache", default=None,
                        help="ingredient cache root (default $PROTEAN_CACHE or the user cache)")
    parser.add_argument("--ingredients-dir", default=None,
                        help="directory of ingredient checkouts named by slug")
    parser.add_argument("--verify", action="store_true",
                        help="require every pinned tree and recompute its sha and treeSha256")
    parser.add_argument("--offline", action="store_true",
                        help="with --verify, never fetch: require the cache to be warm")
    args = parser.parse_args()

    if not os.path.isfile(args.lock):
        print("FAIL: lock file not found: %s" % args.lock)
        return 2
    try:
        with open(args.lock, encoding="utf-8") as fh:
            lock = json.load(fh)
    except ValueError as exc:
        print("FAIL: lock file is not valid JSON: %s" % exc)
        return 2

    violations, notes, unchecked = [], [], []
    if lock.get("lockVersion") != 1:
        violations.append("lockVersion must be 1, got %r" % lock.get("lockVersion"))
    hosts = lock.get("allowedHosts") or []
    owners = lock.get("allowedOwners") or []
    if not hosts or not owners:
        violations.append("allowedHosts and allowedOwners are required")
    pins = lock.get("ingredients")
    if not isinstance(pins, list) or not pins:
        print("FAIL: ingredients must be a non-empty list")
        return 2

    seen, requires, targets = {}, {}, {}
    for pin in pins:
        slug = pin.get("slug")
        if not slug or not SLUG_RE.match(slug):
            violations.append("invalid slug: %r" % slug)
            continue
        if slug in seen:
            violations.append("duplicate slug: %s" % slug)
        seen[slug] = pin
        ref = pin.get("ref") or ""
        if not REF_RE.match(ref):
            violations.append("%s: ref must be refs/tags/vX.Y.Z (a tag, never a branch): %r"
                              % (slug, ref))
        if not SHA_RE.match(pin.get("sha") or ""):
            violations.append("%s: sha must be a 40-hex peeled commit sha" % slug)
        repo = pin.get("repo") or ""
        match = re.match(r"^https://([^/]+)/([^/]+)/[^/]+$", repo)
        if not match:
            violations.append("%s: repo must be an https repository url: %r" % (slug, repo))
        else:
            if match.group(1) not in hosts:
                violations.append("%s: host %s is outside allowedHosts" % (slug, match.group(1)))
            if match.group(2) not in owners:
                violations.append("%s: owner %s is outside allowedOwners" % (slug, match.group(2)))
        if pin.get("version") and ref and not ref.endswith("v" + str(pin["version"])):
            violations.append("%s: ref %s does not name the declared version %s"
                              % (slug, ref, pin["version"]))
        integrity = pin.get("integrity") or {}
        if integrity.get("algorithm") != ALGORITHM:
            violations.append("%s: integrity.algorithm must be %s" % (slug, ALGORITHM))
        if not re.match(r"^[0-9a-f]{64}$", integrity.get("treeSha256") or ""):
            violations.append("%s: integrity.treeSha256 must be 64-hex" % slug)
        if not isinstance(pin.get("installTargets"), list) or not pin["installTargets"]:
            violations.append("%s: installTargets must be a non-empty list" % slug)
        else:
            for t in pin["installTargets"]:
                key = t.rstrip("/")
                if key in targets and targets[key] != slug:
                    violations.append("install target collision: %s claimed by %s and %s"
                                      % (key, targets[key], slug))
                targets[key] = slug
        requires[slug] = list(pin.get("requires") or [])
        for field in ("version", "contract", "kind"):
            if not pin.get(field):
                violations.append("%s: %s is required" % (slug, field))

    for slug, deps in requires.items():
        for dep in deps:
            if dep not in seen:
                violations.append("%s: requires %s, which is not in the lock" % (slug, dep))

    order, cycle = topo_order(set(seen), requires)
    if cycle:
        violations.append("requires cycle: %s" % cycle)
    locked_order = [s for s in (lock.get("installOrder") or []) if s in seen]
    if locked_order and order and not order_is_valid(locked_order, set(seen), requires):
        notes.append("recorded installOrder is not a valid topological order of the lock; the "
                     "computed order %s is authoritative" % ", ".join(order))

    composer_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(os.path.join(composer_root, ".gitmodules")):
        violations.append("a .gitmodules file exists in the composer tree")
    if os.path.isdir(os.path.join(composer_root, "vendor")):
        violations.append("a vendor/ payload tree exists in the composer tree")

    cache = args.cache or os.environ.get("PROTEAN_CACHE")
    if not cache:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".cache")
        cache = os.path.join(base, "protean-kit")

    provenance = {}
    for slug in sorted(seen):
        pin = seen[slug]
        tree = None
        if args.ingredients_dir:
            candidate = os.path.join(args.ingredients_dir, slug)
            if os.path.isdir(os.path.join(candidate, ".git")):
                tree = candidate
                provenance[slug] = "ingredients-dir"
        if tree is None and os.path.isdir(os.path.join(cache, slug, pin["sha"], ".git")):
            tree = os.path.join(cache, slug, pin["sha"])
            provenance[slug] = "cache"
        if tree is None:
            if args.verify:
                if args.offline:
                    violations.append("%s: no tree available under --offline" % slug)
                    continue
                os.makedirs(os.path.join(cache, slug), exist_ok=True)
                import tempfile
                work = tempfile.mkdtemp(prefix="check-lock-", dir=os.path.join(cache, slug))
                run(["git", "init", "--quiet", work])
                run(["git", "remote", "add", "origin", pin["repo"]], cwd=work)
                run(["git", "fetch", "--quiet", "--depth", "1", "origin", pin["ref"]], cwd=work)
                peeled = run(["git", "rev-parse", "FETCH_HEAD^{commit}"], cwd=work).stdout.strip()
                if peeled != pin["sha"]:
                    violations.append("%s: %s resolves to %s, not the pinned %s"
                                      % (slug, pin["ref"], peeled, pin["sha"]))
                    continue
                run(["git", "checkout", "--quiet", "--detach", pin["sha"]], cwd=work)
                tree = work
                provenance[slug] = "remote"
            else:
                unchecked.append(slug)
        if tree is None:
            continue
        if args.verify and not args.offline and provenance.get(slug) != "remote":
            # A warm cache or an ingredients-dir checkout can sit at the pinned
            # sha while the published tag has moved on. --verify must never
            # present a local cache entry as a published-release verification,
            # so the tag pairing is re-resolved against the remote.
            remote = remote_tag_sha(pin["repo"], pin["ref"])
            if remote is None:
                violations.append("%s: cannot resolve %s against the remote" % (slug, pin["ref"]))
            elif remote != pin["sha"]:
                violations.append("%s: %s resolves on the remote to %s, not the pinned %s"
                                  % (slug, pin["ref"], remote, pin["sha"]))
            else:
                provenance[slug] = "remote"
        head = run(["git", "rev-parse", "HEAD"], cwd=tree)
        if head.returncode == 0 and head.stdout.strip() not in (pin["sha"], ""):
            violations.append("%s: tree is at %s, not the pinned %s"
                              % (slug, head.stdout.strip(), pin["sha"]))
        actual = tree_sha256(tree, pin["sha"])
        if actual is None:
            violations.append("%s: cannot read the pinned commit %s in its tree" % (slug, pin["sha"]))
        elif actual != pin["integrity"]["treeSha256"]:
            violations.append("%s: treeSha256 mismatch\nexpected: %s\nactual:   %s"
                              % (slug, pin["integrity"]["treeSha256"], actual))
        descriptor, _where = descriptor_of(pin, cache, args.ingredients_dir)
        if descriptor is None:
            unchecked.append(slug + " descriptor")
            continue
        if descriptor.get("contract") != pin.get("contract"):
            violations.append("%s: lock contract differs from the ingredient descriptor's contract"
                              % slug)
        if sorted(descriptor.get("installTargets") or []) != sorted(pin.get("installTargets") or []):
            violations.append("%s: lock installTargets differ from the descriptor's" % slug)

    for note in notes:
        print("note: " + note)
    if unchecked:
        print("unverified (no tree available; run --verify): %s" % ", ".join(sorted(set(unchecked))))
    if args.verify:
        for slug in sorted(seen):
            print("verify: %s [provenance: %s]" % (slug, provenance.get(slug, "no tree")))
        if args.offline:
            print("note: --verify --offline checked pins against local trees only; "
                  "the remote tag pairing was not re-resolved")
    if violations:
        print("LOCK VIOLATION: %d" % len(violations))
        for v in violations:
            print("  " + v)
        return 1
    authoritative = locked_order if (locked_order and order
                                     and order_is_valid(locked_order, set(seen), requires)) else order
    print("check-lock: PASS  ingredients=%d  order=%s" % (len(seen), " -> ".join(authoritative or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
