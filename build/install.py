#!/usr/bin/env python3
"""Protean Kit installer - resolves a selection from kit.lock.json, verifies every
pin, installs the selected ingredients, and reads every written file back.

Composition is manifest-only: this program fetches ingredients from their pinned
tags and writes them into the target. It copies no ingredient payload into the
composer and uses no git submodules.

Contribution mode is an install-time choice with a safe default:

  bash install.sh --all --target <dir> --contribution-mode off|on [--operator-ref <reference>]

  off (the default) records the mode as off; on requires --operator-ref and is
  refused without it. The composer vendors no schema: when the selection installs
  the ops ingredient, the record is rendered from that ingredient's own template
  and written beside it. An existing record is never overwritten.

  bash install.sh --all --target <dir>
  bash install.sh --ingredient <slug> --target <dir>
  bash install.sh --ingredient <slug> --no-deps --target <dir>
  bash install.sh --all --target <dir> --offline
  bash install.sh --all --target <dir> --dry-run

Exit codes:
  0 every selected ingredient fetched, integrity-verified, installed, read back
  1 usage or input: unknown flag, unknown slug, missing or unparseable lock
  2 lock invalid: schema, lockVersion, duplicate slug, non-tag ref, host or owner
    outside the allowlist, a submodule or vendored payload tree in the composer
  3 dependency: missing closure member, requires cycle, unresolvable requirement
  4 integrity: sha mismatch, moved tag, tree hash mismatch, cache unknown under
    --offline
  5 install: target not writable, or an ingredient gate failed in place
  6 verification: post-install read-back differs from the cached file

A run that installs some but not all of the selection exits non-zero. There is no
partial-success zero.

Stdlib only. No third-party import.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REF_RE = re.compile(r"^refs/tags/v\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOCK_VERSION = 1
LOCK_FILE = "kit.lock.json"
ALGORITHM = "protean-tree-v1"
CACHE_ENV = "PROTEAN_CACHE"
STAGING = ".protean-staging"
STATE_REL = "records/CONTRIB-STATE.md"
STATE_TMPL_REL = "records/CONTRIB-STATE.md.tmpl"
CONTRIBUTION_MODES = ("off", "on")


class Failure(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def info(message):
    sys.stdout.write(message + "\n")


def warn(message):
    sys.stderr.write("warning: " + message + "\n")


def run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def git(args, cwd=None):
    result = run(["git"] + args, cwd=cwd)
    if result.returncode != 0:
        raise Failure(4, "git %s failed: %s" % (args[0], (result.stderr or "").strip()[:200]))
    return result.stdout.strip()


def usage():
    info(__doc__.strip())


def parse_args(argv):
    opts = {"all": False, "ingredients": [], "no_deps": False, "offline": False,
            "dry_run": False, "target": None, "lock": LOCK_FILE, "cache": None,
            "contribution_mode": "off", "operator_ref": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            usage()
            raise SystemExit(0)
        elif arg == "--all":
            opts["all"] = True
            i += 1
        elif arg == "--ingredient":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --ingredient needs a slug")
            opts["ingredients"].append(argv[i + 1])
            i += 2
        elif arg == "--no-deps":
            opts["no_deps"] = True
            i += 1
        elif arg == "--offline":
            opts["offline"] = True
            i += 1
        elif arg == "--dry-run":
            opts["dry_run"] = True
            i += 1
        elif arg == "--target":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --target needs a directory")
            opts["target"] = argv[i + 1]
            i += 2
        elif arg.startswith("--target="):
            opts["target"] = arg.split("=", 1)[1]
            i += 1
        elif arg == "--lock":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --lock needs a path")
            opts["lock"] = argv[i + 1]
            i += 2
        elif arg == "--cache":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --cache needs a directory")
            opts["cache"] = argv[i + 1]
            i += 2
        elif arg == "--contribution-mode":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --contribution-mode needs off or on")
            opts["contribution_mode"] = argv[i + 1].strip().lower()
            i += 2
        elif arg.startswith("--contribution-mode="):
            opts["contribution_mode"] = arg.split("=", 1)[1].strip().lower()
            i += 1
        elif arg == "--operator-ref":
            if i + 1 >= len(argv):
                raise Failure(1, "usage: --operator-ref needs a reference")
            opts["operator_ref"] = argv[i + 1]
            i += 2
        elif arg.startswith("--operator-ref="):
            opts["operator_ref"] = arg.split("=", 1)[1]
            i += 1
        else:
            raise Failure(1, "usage: unknown flag: %s" % arg)
    if not opts["all"] and not opts["ingredients"]:
        raise Failure(1, "usage: select with --all or --ingredient <slug>")
    if opts["all"] and opts["ingredients"]:
        raise Failure(1, "usage: --all and --ingredient are mutually exclusive")
    if opts["no_deps"] and opts["all"]:
        raise Failure(1, "usage: --no-deps applies to a single-ingredient selection")
    if not opts["dry_run"] and not opts["target"]:
        raise Failure(1, "usage: --target <dir> is required (or use --dry-run)")
    if opts["contribution_mode"] not in CONTRIBUTION_MODES:
        raise Failure(1, "usage: --contribution-mode must be off or on, got %r"
                      % opts["contribution_mode"])
    if opts["contribution_mode"] == "on" and not opts["operator_ref"]:
        raise Failure(1, "--contribution-mode on requires --operator-ref <reference>: "
                         "turning external contribution on is a recorded operator decision, "
                         "and an unrecorded switch is refused")
    return opts


def cache_root(opts):
    if opts.get("cache"):
        return os.path.abspath(os.path.expanduser(opts["cache"]))
    env = os.environ.get(CACHE_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "protean-kit")


def target_root(opts):
    if opts.get("target"):
        return os.path.abspath(os.path.expanduser(opts["target"]))
    team = os.environ.get("HERMES_TEAM_SKILLS")
    if team:
        return os.path.abspath(os.path.expanduser(team))
    return os.path.abspath("./installed")


# ---------------------------------------------------------------------------
# Lock loading and validation (exit 2)
# ---------------------------------------------------------------------------

def load_lock(path):
    if not os.path.isfile(path):
        raise Failure(1, "lock file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            lock = json.load(fh)
    except ValueError as exc:
        raise Failure(1, "lock file is not valid JSON: %s" % exc)
    if not isinstance(lock, dict):
        raise Failure(2, "lock root must be an object")
    return lock


def validate_lock(lock):
    if lock.get("lockVersion") != LOCK_VERSION:
        raise Failure(2, "lockVersion must be %d" % LOCK_VERSION)
    kit = lock.get("kit")
    if not isinstance(kit, dict) or not kit.get("name"):
        raise Failure(2, "kit.name is required")
    hosts = lock.get("allowedHosts") or []
    owners = lock.get("allowedOwners") or []
    if not hosts or not owners:
        raise Failure(2, "allowedHosts and allowedOwners are required")
    ingredients = lock.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise Failure(2, "ingredients must be a non-empty list")
    seen = {}
    for entry in ingredients:
        slug = entry.get("slug")
        if not slug or not re.match(r"^[a-z][a-z0-9-]*$", slug):
            raise Failure(2, "invalid slug: %r" % slug)
        if slug in seen:
            raise Failure(2, "duplicate slug in the lock: %s" % slug)
        seen[slug] = entry
        ref = entry.get("ref") or ""
        if not REF_RE.match(ref):
            raise Failure(2, "%s: ref must be an annotated tag refs/tags/vX.Y.Z, got %r" % (slug, ref))
        if not SHA_RE.match(entry.get("sha") or ""):
            raise Failure(2, "%s: sha must be a 40-hex peeled commit sha" % slug)
        repo = entry.get("repo") or ""
        match = re.match(r"^https://([^/]+)/([^/]+)/[^/]+$", repo)
        if not match:
            raise Failure(2, "%s: repo must be an https repository url, got %r" % (slug, repo))
        if match.group(1) not in hosts:
            raise Failure(2, "%s: host %s is outside allowedHosts" % (slug, match.group(1)))
        if match.group(2) not in owners:
            raise Failure(2, "%s: owner %s is outside allowedOwners" % (slug, match.group(2)))
        integrity = entry.get("integrity") or {}
        if integrity.get("algorithm") != ALGORITHM:
            raise Failure(2, "%s: integrity.algorithm must be %s" % (slug, ALGORITHM))
        if not re.match(r"^[0-9a-f]{64}$", integrity.get("treeSha256") or ""):
            raise Failure(2, "%s: integrity.treeSha256 must be 64-hex" % slug)
        if not isinstance(entry.get("installTargets"), list) or not entry["installTargets"]:
            raise Failure(2, "%s: installTargets must be a non-empty list" % slug)
        for field in ("contract", "version", "kind"):
            if not entry.get(field):
                raise Failure(2, "%s: %s is required" % (slug, field))
    for entry in ingredients:
        for dep in list(entry.get("requires") or []):
            if dep not in seen:
                raise Failure(3, "%s requires %s, which is not in the lock" % (entry["slug"], dep))
    # install target collisions between ingredients
    owner_of = {}
    for entry in ingredients:
        for t in entry["installTargets"]:
            key = t.rstrip("/")
            if key in owner_of and owner_of[key] != entry["slug"]:
                raise Failure(2, "install target collision: %s is claimed by %s and %s"
                              % (key, owner_of[key], entry["slug"]))
            owner_of[key] = entry["slug"]
    return seen


def composer_tree_violations(composer_root):
    problems = []
    if os.path.isfile(os.path.join(composer_root, ".gitmodules")):
        problems.append("a .gitmodules file exists in the composer")
    result = run(["git", "ls-files", "-s"], cwd=composer_root)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("160000"):
                problems.append("a submodule entry is tracked in the composer")
                break
    if os.path.isdir(os.path.join(composer_root, "vendor")):
        problems.append("a vendor/ payload tree exists in the composer")
    return problems


# ---------------------------------------------------------------------------
# Selection, closure, order (exit 3)
# ---------------------------------------------------------------------------

def resolve_selection(lock, seen, opts):
    if opts["all"]:
        selection = [e["slug"] for e in lock["ingredients"]]
    else:
        selection = list(opts["ingredients"])
        for slug in selection:
            if slug not in seen:
                raise Failure(1, "unknown ingredient: %s" % slug)
    if opts["no_deps"]:
        # Exactly the selection is written; its requirements are reported as
        # degraded rather than resolved.
        degraded = sorted({dep for slug in selection
                           for dep in (seen[slug].get("requires") or [])})
        return selection, list(selection), degraded
    closure = list(selection)
    pending = list(selection)
    while pending:
        slug = pending.pop()
        for dep in seen[slug].get("requires") or []:
            if dep not in closure:
                closure.append(dep)
                pending.append(dep)
    degraded = []
    if not opts["all"]:
        for slug in selection:
            for dep in seen[slug].get("requires") or []:
                if dep not in closure:
                    degraded.append(dep)
    return selection, closure, sorted(set(degraded))


def find_cycle(members_left, requires, members):
    """Return the cycle path among the unresolved nodes, for the failure message."""
    state, stack = {}, []

    def visit(slug):
        colour = state.get(slug)
        if colour == "done":
            return None
        if colour == "grey":
            return stack[stack.index(slug):] + [slug]
        state[slug] = "grey"
        stack.append(slug)
        for dep in sorted(requires.get(slug, [])):
            if dep in members and dep in members_left:
                cycle = visit(dep)
                if cycle:
                    return cycle
        stack.pop()
        state[slug] = "done"
        return None

    for slug in sorted(members_left):
        cycle = visit(slug)
        if cycle:
            return cycle
    return sorted(members_left)


def topo_order(closure, requires):
    """Topological order over requires, ascending slug as the stable tiebreak.

    Repeatedly places the smallest-slug node whose hard dependencies are all
    placed. A closure that never becomes fully ready holds a cycle, which is a
    hard failure (exit 3) whose path is printed.
    """
    members = set(closure)
    order, remaining = [], set(members)
    while remaining:
        ready = sorted(slug for slug in remaining
                       if not any(dep in remaining for dep in requires.get(slug, [])
                                  if dep in members))
        if not ready:
            raise Failure(3, "requires cycle: %s"
                          % " -> ".join(find_cycle(remaining, requires, members)))
        order.append(ready[0])
        remaining.discard(ready[0])
    return order


def order_is_valid(order, closure, requires):
    """True when `order` lists the closure exactly once and respects every edge."""
    members = set(closure)
    if set(order) != members or len(order) != len(members):
        return False
    placed = set()
    for slug in order:
        for dep in requires.get(slug, []):
            if dep in members and dep not in placed:
                return False
        placed.add(slug)
    return True


def resolve_order(closure, requires, locked_order):
    """Resolve the install order without assuming either source.

    The lock declares the order; the resolver validates that declaration against
    the dependency graph. A declaration that is not a valid topological order of
    the selected closure is reported and the computed order wins.
    """
    computed = topo_order(closure, requires)
    members = set(closure)
    recorded = [slug for slug in (locked_order or []) if slug in members]
    if recorded and order_is_valid(recorded, closure, requires):
        return recorded, None
    if recorded:
        return computed, ("recorded installOrder is not a valid topological order of the "
                          "selection; the computed order wins: %s" % ", ".join(computed))
    return computed, None


# ---------------------------------------------------------------------------
# Cache, fetch, integrity
# ---------------------------------------------------------------------------

def tree_lines(repo_dir, commit):
    out = git(["ls-tree", "-r", "--full-tree", commit], cwd=repo_dir)
    lines = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, _kind, blob = parts
        lines.append((mode + " " + path).encode("utf-8") + b"\x00"
                     + blob.encode("utf-8") + b"\n")
    lines.sort(key=lambda raw: raw.split(b"\x00", 1)[0])
    return lines


def tree_sha256(repo_dir, commit):
    digest = hashlib.sha256()
    for line in tree_lines(repo_dir, commit):
        digest.update(line)
    return digest.hexdigest()


def cache_entry(cache, slug, sha):
    return os.path.join(cache, slug, sha)


def fetch_entry(entry, cache, offline):
    slug, sha = entry["slug"], entry["sha"]
    final = cache_entry(cache, slug, sha)
    if os.path.isdir(os.path.join(final, ".git")):
        head = run(["git", "rev-parse", "HEAD"], cwd=final)
        if head.returncode == 0 and head.stdout.strip() == sha:
            return final, "cache hit"
        raise Failure(4, "%s: cache entry at %s does not match the pinned sha" % (slug, final))
    if offline:
        raise Failure(4, "offline cache unavailable for %s at the pinned sha" % slug)
    os.makedirs(os.path.join(cache, slug), exist_ok=True)
    work = tempfile.mkdtemp(prefix="protean-fetch-", dir=os.path.dirname(final))
    try:
        git(["init", "--quiet", work], cwd=None)
        git(["remote", "add", "origin", entry["repo"]], cwd=work)
        git(["fetch", "--quiet", "--depth", "1", "origin", entry["ref"]], cwd=work)
        peeled = git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=work)
        if peeled != sha:
            raise Failure(4, "%s: %s resolves to %s, not the pinned %s"
                          % (slug, entry["ref"], peeled, sha))
        git(["checkout", "--quiet", "--detach", sha], cwd=work)
        actual = tree_sha256(work, sha)
        expected = entry["integrity"]["treeSha256"]
        if actual != expected:
            raise Failure(4, "%s: treeSha256 mismatch\nexpected: %s\nactual:   %s"
                          % (slug, expected, actual))
        os.rename(work, final)
        return final, "fetched"
    except Failure:
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        raise
    except OSError:
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        raise


def verify_cache_checkout(entry, entry_dir):
    """Compare the files we execute/copy with the pinned blobs, not the index.

    A matching HEAD and object tree do not authenticate a mutable checkout.
    Read the worktree directly so skip-worktree/assume-unchanged and ignored
    files cannot hide an altered or extra payload from the installer.
    """
    tracked = set()
    tree = git(["ls-tree", "-r", "-z", "--full-tree", entry["sha"]], cwd=entry_dir)
    for item in tree.split("\0"):
        if not item:
            continue
        metadata, rel = item.split("\t", 1)
        mode, kind, blob = metadata.split()
        tracked.add(rel)
        path = os.path.join(entry_dir, rel)
        if kind != "blob" or not os.path.lexists(path):
            raise Failure(4, "%s: cache file missing or unsupported: %s" % (entry["slug"], rel))
        # The installer copies regular bytes, not links. A pinned link pins
        # only its text; dereferencing it would copy bytes outside this tree.
        if mode == "120000":
            raise Failure(4, "%s: cache contains an unsupported symlink: %s" % (entry["slug"], rel))
        if os.path.islink(path) or not os.path.isfile(path):
            raise Failure(4, "%s: cache file type changed: %s" % (entry["slug"], rel))
        with open(path, "rb") as handle:
            data = handle.read()
        executable = bool(os.stat(path).st_mode & 0o111)
        if executable != (mode == "100755"):
            raise Failure(4, "%s: cache file mode changed: %s" % (entry["slug"], rel))
        actual = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        if actual != blob:
            raise Failure(4, "%s: cache file differs from pinned blob: %s" % (entry["slug"], rel))
    for root, directories, files in os.walk(entry_dir, followlinks=False):
        if root == entry_dir:
            directories[:] = [name for name in directories if name != ".git"]
        links = [name for name in directories if os.path.islink(os.path.join(root, name))]
        for name in files + links:
            rel = os.path.relpath(os.path.join(root, name), entry_dir)
            if rel not in tracked:
                raise Failure(4, "%s: cache contains an unpinned file: %s" % (entry["slug"], rel))


def verify_entry(entry, entry_dir):
    slug = entry["slug"]
    head = run(["git", "rev-parse", "HEAD"], cwd=entry_dir)
    if head.returncode != 0 or head.stdout.strip() != entry["sha"]:
        raise Failure(4, "%s: cache checkout does not sit at the pinned sha" % slug)
    actual = tree_sha256(entry_dir, entry["sha"])
    expected = entry["integrity"]["treeSha256"]
    if actual != expected:
        raise Failure(4, "%s: treeSha256 mismatch\nexpected: %s\nactual:   %s"
                      % (slug, expected, actual))
    verify_cache_checkout(entry, entry_dir)
    return True


# ---------------------------------------------------------------------------
# Install and read-back
# ---------------------------------------------------------------------------

def read_descriptor(entry_dir):
    path = os.path.join(entry_dir, "protean-ingredient.json")
    if not os.path.isfile(path):
        raise Failure(5, "ingredient descriptor missing: %s" % path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def copy_paths(entry_dir, staging, targets):
    written = []
    for rel in targets:
        src = os.path.join(entry_dir, rel)
        dest = os.path.join(staging, rel)
        if os.path.isdir(src):
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            for root, _dirs, files in os.walk(dest):
                for name in files:
                    written.append(os.path.relpath(os.path.join(root, name), staging))
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            written.append(os.path.relpath(dest, staging))
        else:
            raise Failure(5, "declared install target missing in the ingredient: %s" % rel)
    return sorted(written)


def run_gates(descriptor, entry_dir, staging):
    gates = descriptor.get("gates") or []
    ran = []
    for gate in gates:
        if gate.get("postInstall") is False:
            continue
        cmd = gate.get("cmd")
        if not cmd:
            continue
        cwd = os.path.join(staging, gate.get("cwd", "."))
        result = run(["bash", "-c", cmd], cwd=cwd)
        ran.append({"name": gate.get("name"), "cmd": cmd, "exit": result.returncode})
        if result.returncode != 0:
            raise Failure(5, "ingredient gate failed in place: %s (%s)\n%s"
                          % (gate.get("name"), cmd, (result.stdout + result.stderr).strip()[:400]))
    return ran


def promote(staging, target, written):
    for rel in written:
        src = os.path.join(staging, rel)
        dest = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(src, dest)


def read_back(entry_dir, target, written):
    mismatches = []
    for rel in written:
        src = os.path.join(entry_dir, rel)
        dest = os.path.join(target, rel)
        if not os.path.isfile(dest):
            mismatches.append((rel, "missing after install"))
            continue
        with open(src, "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()
        with open(dest, "rb") as fh:
            got = hashlib.sha256(fh.read()).hexdigest()
        if want != got:
            mismatches.append((rel, "content differs from the cached file"))
    return mismatches


def install_one(entry, cache, opts, plan, entry_dir):
    slug = entry["slug"]
    target = plan["target"]
    os.makedirs(target, exist_ok=True)
    if not os.access(target, os.W_OK):
        raise Failure(5, "target is not writable: %s" % target)
    descriptor = read_descriptor(entry_dir)
    staging_root = os.path.join(target, STAGING, slug)
    if os.path.isdir(staging_root):
        shutil.rmtree(staging_root)
    os.makedirs(staging_root, exist_ok=True)
    written = copy_paths(entry_dir, staging_root, entry["installTargets"])
    gates = run_gates(descriptor, entry_dir, staging_root)
    promote(staging_root, target, written)
    shutil.rmtree(os.path.join(target, STAGING), ignore_errors=True)
    return written, gates


# ---------------------------------------------------------------------------
# Contribution mode (install-time toggle; the schema belongs to protean-ops)
# ---------------------------------------------------------------------------

def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_cells(line):
    if not line.strip().startswith("|"):
        return []
    return [c.strip() for c in line.strip().strip("|").split("|")]


def set_header_value(text, key, value):
    out = []
    for line in text.splitlines():
        cells = row_cells(line)
        if len(cells) == 2 and cells[0].lower() == key.lower():
            line = "| %s | %s |" % (cells[0], value)
        out.append(line)
    return "\n".join(out) + "\n"


def contribution_switch_row(now, mode, operator_ref):
    stamp = now.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    return ("| cc-%s | switch | - | - | - | external | approval %s | external_contrib=%s "
            "operator reference %s | installer | %s | - |"
            % (stamp, operator_ref, mode, operator_ref, now))


def contribution_state(target, mode, operator_ref):
    """Write the contribution-state record. Returns (relative path, note).

    The schema belongs to the ops ingredient, so this renders that ingredient's
    own installed template instead of carrying a copy of it. An existing record
    is left untouched: an install never silently widens a permission.
    """
    dest = os.path.join(target, STATE_REL)
    if os.path.exists(dest):
        return None, ("%s already exists and was left unchanged; an install never overwrites "
                      "an existing contribution-state record" % STATE_REL)
    template = os.path.join(target, STATE_TMPL_REL)
    if not os.path.isfile(template):
        if mode == "on":
            raise Failure(5, "contribution mode 'on' was requested, but %s is not installed, "
                             "so the contribution-state record cannot be rendered from its "
                             "template" % STATE_TMPL_REL)
        return None, ("%s not written: %s is not in this selection"
                      % (STATE_REL, STATE_TMPL_REL))
    with open(template, encoding="utf-8") as fh:
        text = fh.read()
    now = utc_stamp()
    reason = "installer: contribution mode %s" % mode
    if operator_ref:
        reason += ", operator reference %s" % operator_ref
    text = set_header_value(text, "internal_contrib", "on")
    text = set_header_value(text, "external_contrib", mode)
    text = set_header_value(text, "read_at", now)
    text = set_header_value(text, "last_change", "%s %s" % (now, reason))
    if mode == "on":
        row = contribution_switch_row(now, mode, operator_ref)
        text = "\n".join(row if "empty state" in line.lower() else line
                         for line in text.splitlines()) + "\n"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)
    return STATE_REL, None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def dry_run(lock, order, plan, cache, offline):
    info("kit: %s %s" % (lock["kit"].get("name"), lock["kit"].get("version")))
    info("selection: %s" % plan["selection_label"])
    info("target: %s" % plan["target"])
    info("dependency mode: %s" % ("degraded (--no-deps)" if plan["no_deps"] else "full"))
    info("install order: %s" % ", ".join(order))
    info("network mode: %s" % ("offline (cache only)" if offline else "online"))
    info("contribution mode: %s%s" % (plan["contribution_mode"],
                                      "" if plan["contribution_mode"] == "off"
                                      else " (operator reference %s)" % plan["operator_ref"]))
    info("planned writes (target-relative), per ingredient:")
    for slug in order:
        entry = plan["entries"][slug]
        for rel in entry["installTargets"]:
            state = "cached" if os.path.isdir(cache_entry(cache, slug, entry["sha"])) else "to fetch"
            info("  %s: %s [%s]" % (slug, rel, state))
    info("gates: each ingredient's post-install gates run after staging and before promotion")
    if plan["degraded"]:
        info("degraded requirements reported: %s" % ", ".join(plan["degraded"]))
    info("result: dry-run, no files written")
    return 0


def main(argv):
    opts = parse_args(argv)
    composer_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lock_path = opts["lock"]
    if not os.path.isabs(lock_path):
        lock_path = os.path.join(os.getcwd(), lock_path)
    lock = load_lock(lock_path)
    seen = validate_lock(lock)

    problems = composer_tree_violations(composer_root)
    if problems:
        raise Failure(2, "composer tree is invalid: " + "; ".join(problems))

    selection, closure, degraded = resolve_selection(lock, seen, opts)
    for slug in closure:
        if slug not in seen:
            raise Failure(3, "required ingredient %s is not in the lock" % slug)
    requires = {slug: list(seen[slug].get("requires") or []) for slug in closure}
    order, order_note = resolve_order(closure, requires, lock.get("installOrder"))
    if order_note:
        warn(order_note)

    plan = {
        "target": target_root(opts),
        "selection_label": "--all" if opts["all"] else ", ".join(selection),
        "no_deps": opts["no_deps"],
        "degraded": degraded,
        "entries": seen,
        "contribution_mode": opts["contribution_mode"],
        "operator_ref": opts["operator_ref"],
    }
    cache = cache_root(opts)
    if opts["dry_run"]:
        return dry_run(lock, order, plan, cache, opts["offline"])

    info("resolve: %d selected, %d closure members" % (len(selection), len(order)))
    info("order: %s" % " -> ".join(order))
    installed = []
    warnings = []
    for slug in order:
        entry = seen[slug]
        entry_dir, how = fetch_entry(entry, cache, opts["offline"])
        info("fetch: %s [%s]" % (slug, how))
        verify_entry(entry, entry_dir)
        info("verify: %s [sha ok, tree ok]" % slug)
        written, gates = install_one(entry, cache, opts, plan, entry_dir)
        info("install: %s [staged, %d gate(s) passed, %d file(s) promoted]"
             % (slug, len(gates), len(written)))
        mismatches = read_back(entry_dir, plan["target"], written)
        if mismatches:
            detail = "; ".join("%s (%s)" % (rel, why) for rel, why in mismatches[:5])
            raise Failure(6, "post-install verification failed at %s: %s"
                          % (plan["target"], detail))
        info("readback: %s [%d file(s) verified]" % (slug, len(written)))
        installed.append(slug)
        for item in entry.get("open") or []:
            warnings.append("%s carries open item %s" % (slug, item))
        for rec in entry.get("recommends") or []:
            if rec not in order:
                warnings.append("%s recommends %s (reported, not installed)" % (slug, rec))
        if opts["no_deps"]:
            for dep in entry.get("requires") or []:
                warnings.append("%s requires %s: degraded, not installed" % (slug, dep))

    info("")
    info("summary")
    info("  target: %s" % plan["target"])
    info("  installed: %s" % ", ".join(installed))
    info("  selection: %s" % plan["selection_label"])
    info("  read-back: every written file compared to its cached blob")
    info("  contribution mode: %s" % opts["contribution_mode"])
    state_rel, state_note = contribution_state(plan["target"], opts["contribution_mode"],
                                               opts["operator_ref"])
    if state_rel:
        info("  contribution state: wrote %s" % state_rel)
    elif state_note:
        warnings.append(state_note)
    for message in warnings:
        info("  note: %s" % message)
    info("  next: read each installed ingredient's README.md under the target")
    info("result: ok, exit 0")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Failure as failure:
        sys.stderr.write("error: %s\n" % failure.message)
        sys.stderr.write("exit: %d\n" % failure.code)
        sys.exit(failure.code)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.stderr.write("error: interrupted\n")
        sys.exit(5)
