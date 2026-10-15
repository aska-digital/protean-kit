#!/usr/bin/env python3
"""
verify-all.py — Deterministic release gate runner.

Runs the repository's public gates in documented order. Uses subprocess with
fail-closed behavior: nonzero exit on any failure. No dependencies beyond stdlib.

ORDER (this list is the gate list; the composer holds no separate release-gates
document, and this docstring is authoritative):
  1. composition lock validity (check-lock.py, static)
  2. ingredient contract (check-ingredient-contract.py, static)
  3. composition lock tests (tests/test_kit_lock.py)
  4. contribution mode tests (tests/test_contribution_mode.py)

Exit: 0 = all gates pass; 1 = any gate failed.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run_gate(name, cmd, cwd=ROOT):
    """Run a gate, print its output, return True on pass."""
    print(f"\n{'=' * 64}")
    print(f"STEP: {name}")
    print('=' * 64)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=300  # 5 minute timeout per gate
        )

        # Print stdout (the gate's own output)
        if result.stdout:
            print(result.stdout)

        # Print stderr if present (warnings, notes)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        if result.returncode == 0:
            print(f"\n✓ {name}: PASSED")
            return True
        else:
            print(f"\n✗ {name}: FAILED (exit {result.returncode})")
            return False

    except subprocess.TimeoutExpired:
        print(f"\n✗ {name}: TIMEOUT")
        return False
    except Exception as e:
        print(f"\n✗ {name}: ERROR — {e}")
        return False


def main():
    print("=" * 64)
    print("TEAM6-KIT RELEASE GATE RUNNER")
    print("=" * 64)
    print("\nThis runner executes all public release gates in documented order.")
    print("Any failure stops execution. All gates use fail-closed semantics.")

    gates = []

    # 1. composition lock validity
    gates.append(("check-lock.py",
                  [sys.executable, os.path.join(HERE, "check-lock.py"),
                   os.path.join(ROOT, "kit.lock.json")]))

    # 2. ingredient contract (reports pinned trees it cannot read, never invents one)
    gates.append(("check-ingredient-contract.py",
                  [sys.executable, os.path.join(HERE, "check-ingredient-contract.py"),
                   "--lock", os.path.join(ROOT, "kit.lock.json")]))

    # 3. composition lock tests
    gates.append(("composition lock tests",
                  [sys.executable, "-m", "unittest", "tests.test_kit_lock"]))

    # 4. contribution mode tests
    gates.append(("contribution mode tests",
                  [sys.executable, "-m", "unittest", "tests.test_contribution_mode"]))

    gates.append(("target confinement tests",
                  [sys.executable, "-m", "unittest", "tests.test_target_confinement"]))

    # Run all gates
    results = []
    for name, cmd in gates:
        passed = run_gate(name, cmd)
        results.append((name, passed))

    # Summary
    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)

    passed = sum(1 for _, p in results if p)
    failed = sum(1 for _, p in results if not p)
    total = len(results)

    print(f"\n  Gates run: {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")

    print("\n  Results:")
    for name, p in results:
        status = "✓ PASS" if p else "✗ FAIL"
        print(f"    {status}: {name}")

    print("\n" + "=" * 64)

    if failed > 0:
        print("RELEASE BLOCKED — one or more gates failed.")
        print("Fix failures before proceeding.")
        return 1
    else:
        print("RELEASE GATES PASSED — safe to proceed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
