#!/usr/bin/env python3
"""
Full-flow test for Playwright "only UI" QA: run two scenarios and report.

  Scenario 1 (non-UI change): Modify a store file → decision should be SKIP E2E.
  Scenario 2 (UI-only change): Modify a .svelte file → decision RUN E2E, then build + preview + smoke.

Usage (from voiceaiagentv5 project root):
  python scripts/test_e2e_only_ui_flow.py [path-to-phwb-testrepo]
  python scripts/test_e2e_only_ui_flow.py --quick [path]   # decision logic only, no build/playwright

If path not given, uses env PHWB_REPO_PATH or ../phwb-testrepo relative to this repo.
Requires: bun or npm, node, npx, playwright installed in phwb-testrepo (for full run).
"""

import os
import subprocess
import sys
from pathlib import Path

# Unbuffer stdout so progress is visible when run in background
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass

from app.activities.dev_activities import (
    _get_modified_files,
    _is_only_ui_changes,
    _run_cmd,
    _wait_for_preview_ready,
    E2E_PREVIEW_PORT,
    E2E_PREVIEW_URL,
    E2E_WAIT_READY_TIMEOUT,
    E2E_BUILD_TIMEOUT,
    E2E_PLAYWRIGHT_TIMEOUT,
)


def restore_file(repo_path: str, rel_path: str) -> None:
    subprocess.run(
        ["git", "checkout", "--", rel_path],
        cwd=repo_path,
        capture_output=True,
        timeout=5,
    )


def stash_push(repo_path: str) -> bool:
    """Stash all changes; return True if stash was created (had something to stash)."""
    r = subprocess.run(
        ["git", "stash", "push", "-u", "-m", "e2e-flow-test"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and "Saved" in out


def stash_pop(repo_path: str) -> None:
    subprocess.run(
        ["git", "stash", "pop"],
        cwd=repo_path,
        capture_output=True,
        timeout=10,
    )


def append_comment(repo_path: str, rel_path: str, comment: str) -> None:
    path = Path(repo_path) / rel_path
    with open(path, "a", encoding="utf-8") as f:
        f.write(comment)


def run_scenario_non_ui(repo_path: str) -> dict:
    """Apply non-UI change, assert decision is SKIP E2E, restore."""
    rel = "src/lib/stores/artists.ts"
    marker = "\n// E2E flow test: non-UI change\n"
    append_comment(repo_path, rel, marker)
    try:
        modified = _get_modified_files(repo_path)
        only_ui = _is_only_ui_changes(modified)
        return {
            "modified": modified,
            "only_ui": only_ui,
            "expected_skip": True,
            "passed": not only_ui,
        }
    finally:
        restore_file(repo_path, rel)


def run_scenario_ui_only(repo_path: str, quick: bool = False) -> dict:
    """Apply UI-only change, assert decision RUN E2E; if not quick, run build + preview + playwright; restore."""
    rel = "src/routes/+page.svelte"
    marker = "\n<!-- E2E flow test: UI only -->\n"
    append_comment(repo_path, rel, marker)
    try:
        modified = _get_modified_files(repo_path)
        only_ui = _is_only_ui_changes(modified)
        if not only_ui:
            return {
                "modified": modified,
                "only_ui": only_ui,
                "expected_run": True,
                "passed": False,
                "error": "Expected only_ui=True for UI-only change",
            }
        if quick:
            return {
                "modified": modified,
                "only_ui": True,
                "passed": True,
                "skipped_e2e": True,
            }

        # Build
        build_ok = False
        for cmd, name in (
            (["bun", "run", "build"], "bun run build"),
            (["npm", "run", "build"], "npm run build"),
        ):
            code, out, err = _run_cmd(cmd, repo_path, timeout_sec=E2E_BUILD_TIMEOUT)
            if code == 0:
                build_ok = True
                break
        if not build_ok:
            return {
                "modified": modified,
                "only_ui": True,
                "build_passed": False,
                "passed": False,
                "error": "Build failed (bun/npm run build)",
            }

        # Start preview
        preview_proc = None
        for preview_cmd in (["bun", "run", "preview"], ["npm", "run", "preview"]):
            try:
                preview_proc = subprocess.Popen(
                    preview_cmd,
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env={**os.environ},
                )
                break
            except FileNotFoundError:
                continue
        if not preview_proc:
            return {
                "modified": modified,
                "only_ui": True,
                "build_passed": True,
                "passed": False,
                "error": "Could not start preview (bun/npm not found)",
            }

        try:
            if not _wait_for_preview_ready(E2E_PREVIEW_PORT, E2E_WAIT_READY_TIMEOUT):
                stderr = (preview_proc.stderr and preview_proc.stderr.read()) or b""
                return {
                    "modified": modified,
                    "only_ui": True,
                    "build_passed": True,
                    "passed": False,
                    "error": "Preview did not become ready. " + (stderr.decode("utf-8", errors="replace")[-300:] or ""),
                }

            code, out, err = _run_cmd(
                ["npx", "playwright", "test", "--project=smoke"],
                repo_path,
                timeout_sec=E2E_PLAYWRIGHT_TIMEOUT,
                env={"BASE_URL": E2E_PREVIEW_URL},
            )
            combined = (out + "\n" + err).strip()
            if code != 0:
                return {
                    "modified": modified,
                    "only_ui": True,
                    "build_passed": True,
                    "e2e_passed": False,
                    "passed": False,
                    "error": "E2E failed: " + (combined[-500:] if combined else "No output"),
                }
            return {
                "modified": modified,
                "only_ui": True,
                "build_passed": True,
                "e2e_passed": True,
                "passed": True,
            }
        finally:
            preview_proc.terminate()
            try:
                preview_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                preview_proc.kill()
    finally:
        restore_file(repo_path, rel)


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--quick"]
    quick = "--quick" in sys.argv[1:]
    if args:
        repo_path = os.path.abspath(args[0])
    else:
        repo_path = os.getenv("PHWB_REPO_PATH")
        if not repo_path:
            repo_path = str(ROOT.parent / "phwb-testrepo")
        repo_path = os.path.abspath(repo_path)

    if not os.path.isdir(repo_path):
        print(f"ERROR: Repo path is not a directory: {repo_path}")
        sys.exit(2)

    print("=" * 60)
    print("Playwright 'only UI' QA — full flow test" + (" (quick: decision only)" if quick else ""))
    print("=" * 60)
    print(f"Repo: {repo_path}\n")

    # Stash any existing changes so "modified" is only our test file(s)
    had_stash = stash_push(repo_path)
    if had_stash:
        print("(Stashed existing changes for isolated test)\n")
    try:
        results = _run_scenarios(repo_path, quick=quick)
    finally:
        if had_stash:
            stash_pop(repo_path)
            print("(Restored stashed changes)\n")

    _print_report(results)
    overall = results["non_ui"]["passed"] and results["ui_only"].get("passed", False)
    sys.exit(0 if overall else 1)


def _run_scenarios(repo_path: str, quick: bool = False) -> dict:

    results = {}
    # Scenario 1: non-UI change → SKIP E2E
    print("Scenario 1: Non-UI change (store file)")
    print("- Applying change to src/lib/stores/artists.ts ...")
    r1 = run_scenario_non_ui(repo_path)
    results["non_ui"] = r1
    if r1["passed"]:
        print(f"  Decision: only_ui={r1['only_ui']} → SKIP E2E (expected) ✓")
    else:
        print(f"  Decision: only_ui={r1['only_ui']} → RUN E2E (unexpected) ✗")
    print()

    # Scenario 2: UI-only change → RUN E2E
    print("Scenario 2: UI-only change (.svelte)" + (" → build, preview, smoke" if not quick else " → decision only"))
    print("- Applying change to src/routes/+page.svelte ...")
    r2 = run_scenario_ui_only(repo_path, quick=quick)
    results["ui_only"] = r2
    if r2.get("error"):
        print(f"  Error: {r2['error'][:400]}")
        print(f"  Result: FAIL ✗")
    elif r2.get("passed"):
        if r2.get("skipped_e2e"):
            print(f"  Decision: only_ui=True → RUN E2E (expected) ✓ (skipped build/playwright in --quick)")
        else:
            print(f"  Build: OK, E2E smoke: OK ✓")
    else:
        print(f"  Result: {r2.get('error', 'FAIL')} ✗")
    print()
    return results


def _print_report(results: dict) -> None:
    print("=" * 60)
    print("FINAL RESULT")
    print("=" * 60)
    s1 = "PASS" if results["non_ui"]["passed"] else "FAIL"
    s2 = "PASS" if results["ui_only"].get("passed") else "FAIL"
    print(f"  Scenario 1 (non-UI → skip E2E): {s1}")
    print(f"  Scenario 2 (UI only → run E2E):  {s2}")
    overall = results["non_ui"]["passed"] and results["ui_only"].get("passed", False)
    print(f"  Overall: {'PASS' if overall else 'FAIL'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
