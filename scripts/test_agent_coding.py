#!/usr/bin/env python3
"""
Test the dev agent's coding capability: clone repo, run Planner → Coder → Reviewer
with a simple instruction, then show whether any files were changed.

Usage (from voiceaiagentv5 project root):
  python scripts/test_agent_coding.py

Requires .env: ANTHROPIC_API_KEY, DEV_AGENT_GITHUB_ACCESS_TOKEN, DEV_AGENT_GITHUB_REPO_URL (optional).
Requires: node/npx (for MCP filesystem server), git.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass


def clone_repo(workspace_dir: str) -> None:
    """Clone DEV_AGENT_GITHUB_REPO_URL into workspace_dir."""
    token = os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN")
    if not token:
        raise SystemExit("DEV_AGENT_GITHUB_ACCESS_TOKEN not set in .env")
    repo_url = os.getenv("DEV_AGENT_GITHUB_REPO_URL", "https://github.com/singforhope/phwb.git")
    auth_url = repo_url.replace("https://", f"https://oauth2:{token}@")
    git_env = {**os.environ, "GIT_SSL_NO_VERIFY": "1"}
    subprocess.run(
        ["git", "clone", "-c", "http.postBuffer=524288000", auth_url, "."],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=git_env,
    )


async def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    workspace_dir = tempfile.mkdtemp(prefix="dev_agent_test_")
    print(f"Cloning repo into {workspace_dir} ...")
    try:
        clone_repo(workspace_dir)
    except subprocess.CalledProcessError as e:
        print(f"Clone failed: {e.stderr}")
        sys.exit(1)

    # Simple instruction: one small, safe edit so we can verify the agent uses write_file
    instruction = (
        "Add a single line to the README.md file: append the exact line\n"
        "\n"
        "<!-- Test from dev agent -->\n"
        "\n"
        "at the end of the file. Use the write_file tool to make this change. Do nothing else."
    )

    print("Running dev agent (Planner → Coder → Reviewer) ...")
    print("Instruction:", instruction[:80], "...")
    print()

    from app.agents.dev_agent import run_dev_agent

    try:
        result = await run_dev_agent(workspace_dir, instruction)
        print("Agent finished. Final response snippet:", (result or "")[:400])
    except Exception as e:
        print("Agent failed:", e)
        print(f"Workspace left at: {workspace_dir}")
        sys.exit(1)

    # Show whether any files were changed
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )

    print()
    print("--- Git status ---")
    print(status.stdout or "(no changes)")
    print("--- Git diff ---")
    print(diff.stdout[:2000] if diff.stdout else "(no diff)")

    if status.stdout.strip():
        print()
        print("SUCCESS: The agent made file changes (coding capability OK).")
    else:
        print()
        print("NO CHANGES: The agent did not modify any files. Check the instruction or agent logs.")

    print(f"Workspace kept at: {workspace_dir}")


if __name__ == "__main__":
    asyncio.run(main())
