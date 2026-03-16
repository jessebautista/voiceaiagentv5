from temporalio import activity
import os
import re
import tempfile
import subprocess
import shutil
import logging
import time
from pydantic import BaseModel
from typing import Tuple, Dict, Any, Optional, List

class DevActionInput(BaseModel):
    bug_id: int
    title: str
    description: str
    category: str
    status: str


# ─────────────────────────────────────────────────────────────────────────────
# Logging helper — writes a row to phwb_dev_logs for realtime display in PHWB
# ─────────────────────────────────────────────────────────────────────────────

async def log_dev_event(
    bug_id: int,
    step: str,
    message: str,
    level: str = "info",
    workflow_id: str | None = None
) -> None:
    """Insert a log entry into phwb_dev_logs so the PHWB UI can stream it live."""
    try:
        from app.supabase_client import get_supabase_client
        supabase = get_supabase_client("DEV_AGENT")
        if not supabase:
            return
        supabase.table("phwb_dev_logs").insert({
            "bug_id": bug_id,
            "step": step,
            "message": message,
            "level": level,
            "workflow_id": workflow_id,
        }).execute()
    except Exception as e:
        # Never let logging failures crash the workflow
        logging.warning(f"log_dev_event failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Setup Repository
# ─────────────────────────────────────────────────────────────────────────────

@activity.defn
async def setup_repository(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Clones the repository and checks out a new branch. Payload: input_data (dict), workflow_id (optional)."""
    input_data = DevActionInput(**payload["input_data"])
    workflow_id = payload.get("workflow_id")

    github_token = os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN")
    if not github_token:
        raise ValueError("DEV_AGENT_GITHUB_ACCESS_TOKEN not set in environment.")

    repo_url = os.getenv("DEV_AGENT_GITHUB_REPO_URL", "https://github.com/singforhope/phwb.git")
    
    await log_dev_event(input_data.bug_id, "setup_repository", "🔧 Step 1/5: Setting up workspace...", "info", workflow_id)

    # Embed the token in the URL for cloning
    auth_repo_url = repo_url.replace("https://", f"https://oauth2:{github_token}@")

    workspace_dir = tempfile.mkdtemp(prefix=f"phwb_bug_{input_data.bug_id}_")
    logging.info(f"Cloning repo into {workspace_dir}")
    
    await log_dev_event(input_data.bug_id, "setup_repository", "📥 Cloning repository from GitHub...", "info", workflow_id)

    git_env = {
        **os.environ, 
        "GIT_SSL_NO_VERIFY": "1",
        "GIT_HTTP_MAX_REQUESTS": "100",
        "GIT_CURL_VERBOSE": "1"
    }
    
    # Retry clone up to 5 times due to network instability
    max_retries = 5
    for attempt in range(max_retries):
        try:
            logging.info(f"Git clone attempt {attempt + 1}/{max_retries}...")
            subprocess.run(
                ["git", "clone", "-c", "http.postBuffer=524288000", auth_repo_url, "."],
                cwd=workspace_dir,
                check=True,
                capture_output=True,
                text=True,
                env=git_env,
                encoding="utf-8",
                errors="replace"
            )
            logging.info("[setup_repository] Clone succeeded.")
            break
        except subprocess.CalledProcessError as e:
            if attempt == max_retries - 1:
                await log_dev_event(input_data.bug_id, "setup_repository", "❌ Failed to clone repository after {} attempts.".format(max_retries), "error", workflow_id)
                raise
            logging.warning("[setup_repository] Clone failed (retry %s): %s", attempt + 1, (e.stderr or str(e))[:200])
            await log_dev_event(input_data.bug_id, "setup_repository", "⚠️ Clone attempt {} failed, retrying...".format(attempt + 1), "warning", workflow_id)
            time.sleep(5)
    
    branch_name = f"dev-agent/bug-fix/{input_data.bug_id}-{int(time.time())}"
    
    try:
        subprocess.run(
            f"git checkout -b {branch_name}",
            cwd=workspace_dir,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=git_env,
        )
        await log_dev_event(
            input_data.bug_id, "setup_repository",
            "✅ Step 1/5 done. Branch `{}` created. (Pushed to GitHub in Step 4.)".format(branch_name),
            "success", workflow_id
        )
        logging.info("[setup_repository] Branch created: %s", branch_name)
        return workspace_dir, branch_name
    except subprocess.CalledProcessError as e:
        logging.error("[setup_repository] Branch creation failed: %s", e.stderr or str(e))
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Analyze & Code
# ─────────────────────────────────────────────────────────────────────────────

def _build_dev_fix_instruction(repo_path: str, bug_data: DevActionInput, last_error: Optional[str]) -> str:
    """
    Build the dev-fix instruction: repo context first, then a clear task with acceptance criteria.
    """
    # Optional: repo-owned prefix (.dev-agent/instruction-prefix.md or AGENTS.md / CLAUDE.md)
    repo_prefix = ""
    for candidate in (
        os.path.join(repo_path, ".dev-agent", "instruction-prefix.md"),
        os.path.join(repo_path, "AGENTS.md"),
        os.path.join(repo_path, "CLAUDE.md"),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(6000).strip()
                    if content:
                        repo_prefix = f"Repository context (read this first):\n{content}\n\n"
            except Exception:
                pass
            break

    harness_guidance = (
        "Use the repository as source of truth. Read AGENTS.md or CLAUDE.md in the repo root for structure and conventions. "
        "Use docs/ for architecture as needed. Then implement the bug fix below.\n\n"
    )

    task = (
        f"## Bug to fix\n"
        f"- **ID:** #{bug_data.bug_id}\n"
        f"- **Title:** {bug_data.title}\n"
        f"- **Description:** {bug_data.description or '(No description provided)'}\n"
        f"- **Category:** {bug_data.category}\n\n"
        f"## What to do\n"
        f"1. Locate the code that causes or relates to this bug.\n"
        f"2. Make at least one concrete code change (use the write_file tool) that fixes or addresses the issue. Prefer a minimal, single-file change when possible.\n"
        f"3. Do not break the project: the change must pass the project's check (e.g. `bun run check` or `npm run check`).\n"
    )
    if last_error:
        task += (
            f"\n## Previous attempt failed verification\n"
            f"The last run failed with the following output. Fix the issues and try again.\n\n"
            f"```\n{last_error[:8000]}\n```\n"
        )

    return f"{repo_prefix}{harness_guidance}---\n\n{task}"


@activity.defn
async def analyze_and_code(config: Dict[str, Any]) -> None:
    """Invokes the LangChain LLM to analyze the bug and write code changes (harness: repo as source of truth, progressive disclosure)."""
    import asyncio
    repo_path = config["repo_path"]
    bug_data = DevActionInput(**config["bug_data"])
    last_error = config.get("last_error")
    workflow_id = config.get("workflow_id")

    from app.agents.dev_agent import run_dev_agent

    await log_dev_event(bug_data.bug_id, "analyze_and_code", "🤖 Step 2/5: Dev Agent analyzing bug and codebase...", "info", workflow_id)
    logging.info("[analyze_and_code] Building instruction and running agent")

    prompt = _build_dev_fix_instruction(repo_path, bug_data, last_error)
    if last_error:
        await log_dev_event(bug_data.bug_id, "analyze_and_code", "🔁 Retry: using previous verification error as context.", "warning", workflow_id)
        logging.info("[analyze_and_code] Retry with last_error context")

    async def heartbeat_loop():
        """Send Temporal heartbeats every 60s so the activity is not timed out."""
        while True:
            await asyncio.sleep(60)
            try:
                activity.heartbeat("Agent is working...")
            except Exception:
                break

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        await run_dev_agent(repo_path, prompt)
        await log_dev_event(bug_data.bug_id, "analyze_and_code", "✅ Step 2/5 done. Agent finished writing code changes.", "success", workflow_id)
        logging.info("[analyze_and_code] Agent run complete")
    finally:
        heartbeat_task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Verify Fix
# ─────────────────────────────────────────────────────────────────────────────

def _run_cmd(
    cmd: list[str],
    cwd: str,
    timeout_sec: int = 180,
    capture: bool = True,
) -> Tuple[int, str, str]:
    """Run a command; returns (returncode, stdout, stderr). Cross-platform."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return (r.returncode, r.stdout or "", r.stderr or "")
    except subprocess.TimeoutExpired:
        return (-1, "", f"Command timed out after {timeout_sec}s")
    except FileNotFoundError:
        return (-1, "", "Command not found (e.g. bun or npm not in PATH)")


def _get_modified_files(repo_path: str) -> List[str]:
    """Return list of file paths (relative to repo root) changed since HEAD."""
    code, out, _ = _run_cmd(["git", "diff", "--name-only", "HEAD"], repo_path, timeout_sec=10)
    if code != 0:
        return []
    return [line.strip().replace("\\", "/").lstrip("./") for line in (out or "").splitlines() if line.strip()]


def _parse_check_error_paths(full_output: str, repo_path: str) -> List[str]:
    """Parse svelte-check/tsc-style output; return list of relative paths that have errors."""
    repo_norm = os.path.normpath(repo_path).rstrip(os.sep)
    if not repo_norm:
        return []
    seen: set[str] = set()
    # Match "path:line:col" at start of line (svelte-check format)
    for m in re.finditer(r"^(.+?):(\d+):(\d+)", full_output, re.MULTILINE):
        path_raw = m.group(1).strip()
        if not path_raw or path_raw.startswith("("):
            continue
        try:
            if os.path.isabs(path_raw) and path_raw.startswith(repo_norm):
                rel = path_raw[len(repo_norm) :].lstrip(os.sep).replace("\\", "/").lstrip("./")
            else:
                rel = path_raw.replace("\\", "/").lstrip("./")
            if rel and rel not in seen:
                seen.add(rel)
        except Exception:
            continue
    return list(seen)


@activity.defn
async def verify_fix(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Runs install (if needed) and project check in the cloned repo. Payload: repo_path, bug_id, workflow_id (optional)."""
    repo_path = payload["repo_path"]
    bug_id = payload["bug_id"]
    workflow_id = payload.get("workflow_id")
    await log_dev_event(bug_id, "verify_fix", "🔍 Step 3/5: Verifying fix (dependencies + project check)...", "info", workflow_id)
    logging.info("[verify_fix] Starting verification in %s", repo_path)

    try:
        target_node_modules = os.path.join(repo_path, "node_modules")
        # Optional: copy pre-existing node_modules from host (env path, no hardcoded OS path)
        local_node_modules = os.getenv("DEV_AGENT_NODE_MODULES_PATH")
        if local_node_modules:
            local_node_modules = os.path.expanduser(local_node_modules.strip())
            if os.path.isdir(local_node_modules):
                logging.info(f"Copying node_modules from {local_node_modules} to {target_node_modules}...")
                if os.path.exists(target_node_modules):
                    shutil.rmtree(target_node_modules, ignore_errors=True)
                shutil.copytree(local_node_modules, target_node_modules, symlinks=True, ignore_dangling_symlinks=True)
                logging.info("Copy done.")
            else:
                logging.warning(f"DEV_AGENT_NODE_MODULES_PATH set but not a directory: {local_node_modules}")

        # Ensure dependencies: prefer bun, fallback to npm
        if not os.path.isdir(target_node_modules):
            await log_dev_event(bug_id, "verify_fix", "📦 Installing dependencies (bun or npm)...", "info", workflow_id)
            install_ok = False
            for install_cmd, name in (
                (["bun", "install"], "bun install"),
                (["npm", "install", "--no-audit", "--no-fund"], "npm install"),
            ):
                code, out, err = _run_cmd(install_cmd, repo_path, timeout_sec=300)
                if code == 0:
                    install_ok = True
                    logging.info("[verify_fix] %s succeeded", name)
                    break
                logging.warning("[verify_fix] %s failed (code=%s): %s", name, code, (err or out)[:300])
            if not install_ok:
                msg = "Dependency install failed (bun install or npm install). Ensure bun or npm is available and the repo has a valid package.json."
                await log_dev_event(bug_id, "verify_fix", "❌ " + msg, "error", workflow_id)
                return False, msg

        # Run project check (bun run check or npm run check)
        await log_dev_event(bug_id, "verify_fix", "▶️ Running project check (bun run check / npm run check)...", "info", workflow_id)
        check_ok = False
        last_code = -1
        last_out, last_err = "", ""
        for check_cmd, name in (
            (["bun", "run", "check"], "bun run check"),
            (["npm", "run", "check"], "npm run check"),
        ):
            code, out, err = _run_cmd(check_cmd, repo_path, timeout_sec=120)
            last_code, last_out, last_err = code, out, err
            if code == 0:
                check_ok = True
                await log_dev_event(bug_id, "verify_fix", "✅ Step 3/5 done. Project check passed.", "success", workflow_id)
                logging.info("[verify_fix] %s passed", name)
                break
            full = (out + "\n" + err).strip()
            logging.warning("[verify_fix] %s failed (exit %s). Output: %s", name, code, full[-500:] if full else "(none)")

        if check_ok:
            return True, None

        # Bypass pre-existing errors: fail only if check reported errors in files modified in this run.
        # Set DEV_AGENT_VERIFY_STRICT=1 to always fail on any check failure (no bypass).
        if os.getenv("DEV_AGENT_VERIFY_STRICT", "").strip() in ("1", "true", "yes"):
            error_msg = (last_out + "\n" + last_err).strip() or "Project check failed (syntax/type errors)."
            await log_dev_event(bug_id, "verify_fix", "❌ Verification failed:\n" + error_msg[:2000], "error", workflow_id)
            return False, error_msg

        modified = _get_modified_files(repo_path)
        full_output = (last_out + "\n" + last_err).strip()
        error_paths = _parse_check_error_paths(full_output, repo_path)
        errors_in_modified = [p for p in error_paths if p in modified] if modified else []

        if modified and error_paths and not errors_in_modified:
            await log_dev_event(
                bug_id, "verify_fix",
                "✅ Step 3/5 done. Bypassing: errors only in pre-existing files (none in your changes).",
                "info",
                workflow_id,
            )
            logging.info("[verify_fix] Bypass: %s error path(s), 0 in modified files. Passing.", len(error_paths))
            return True, None

        # Fail: either we have errors in modified files, or we couldn't determine modified files
        error_msg = full_output or "Project check failed (syntax/type errors)."
        if errors_in_modified:
            error_msg = "Errors in modified files:\n" + "\n".join(errors_in_modified[:20]) + "\n\n" + error_msg[:1500]
        await log_dev_event(
            bug_id, "verify_fix",
            "❌ Verification failed:\n" + error_msg[:2000],
            "error",
            workflow_id,
        )
        return False, error_msg

    except Exception as e:
        logging.exception("verify_fix failed")
        err_str = str(e)
        bid = payload.get("bug_id")
        wid = payload.get("workflow_id")
        if bid is not None:
            await log_dev_event(bid, "verify_fix", "❌ Unexpected error: " + err_str[:1000], "error", wid)
        return False, f"Unexpected error during verification: {err_str}"


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Apply trivial test change (test_mode: skip LLM, still create branch/PR)
# ─────────────────────────────────────────────────────────────────────────────

@activity.defn
async def apply_trivial_test_change(payload: Dict[str, Any]) -> None:
    """Applies a minimal file change so the workflow can run verify → push → PR without the full agent."""
    repo_path = payload["repo_path"]
    bug_id = payload["bug_id"]
    workflow_id = payload.get("workflow_id")
    await log_dev_event(
        bug_id, "apply_trivial_test_change",
        "🧪 Test mode: Applying trivial change (no LLM).",
        "info", workflow_id
    )
    logging.info("[apply_trivial_test_change] Writing trivial change to repo")
    readme = os.path.join(repo_path, "README.md")
    marker = "\n\n<!-- Dev fix repo test – bug #{} -->\n".format(bug_id)
    if os.path.isfile(readme):
        with open(readme, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if marker.strip() not in content:
            with open(readme, "a", encoding="utf-8") as f:
                f.write(marker)
    else:
        test_file = os.path.join(repo_path, ".dev-agent-repo-test.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Dev fix repo test – bug #{}\n".format(bug_id))
    await log_dev_event(bug_id, "apply_trivial_test_change", "✅ Trivial change applied. Proceeding to verify.", "success", workflow_id)
    logging.info("[apply_trivial_test_change] Done")


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Create Pull Request
# ─────────────────────────────────────────────────────────────────────────────

@activity.defn
async def create_pull_request(config: Dict[str, Any]) -> str:
    """Commits code, pushes branch, and uses PyGithub to raise PR."""
    repo_path = config["repo_path"]
    branch_name = config["branch_name"]
    bug_data = DevActionInput(**config["bug_data"])
    workflow_id = config.get("workflow_id")
    
    github_token = os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN")
    
    repo_url = os.getenv("DEV_AGENT_GITHUB_REPO_URL", "https://github.com/singforhope/phwb.git")
    repo_name = repo_url.split("github.com/")[-1].replace(".git", "")
    await log_dev_event(
        bug_data.bug_id, "create_pull_request",
        "📦 Step 4/5: Committing, pushing branch, and creating PR...",
        "info", workflow_id
    )
    logging.info("[create_pull_request] Staging and committing changes")

    try:
        subprocess.run("git add .", cwd=repo_path, shell=True, check=True)
        status = subprocess.run("git status --porcelain", cwd=repo_path, shell=True, capture_output=True, text=True)
        if not status.stdout.strip():
            await log_dev_event(bug_data.bug_id, "create_pull_request", "⚠️ No code changes to commit.", "warning", workflow_id)
            raise ValueError("The Development Agent completed its analysis but made no code changes. Nothing to commit.")

        await log_dev_event(bug_data.bug_id, "create_pull_request", "📤 Committing and pushing branch to GitHub...", "info", workflow_id)
        subprocess.run(
            ["git", "commit", "-m", f"Fixes #{bug_data.bug_id}: {bug_data.title}"],
            cwd=repo_path, check=True
        )
        subprocess.run(
            f"git push origin {branch_name}",
            cwd=repo_path, shell=True, check=True
        )
        logging.info("[create_pull_request] Pushed branch %s to origin", branch_name)
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to commit/push: {e}")
        await log_dev_event(bug_data.bug_id, "create_pull_request", "❌ Failed to push branch: " + str(e), "error", workflow_id)
        raise

    # Raise PR using PyGithub (lazy import: only in activity, not in workflow sandbox)
    try:
        from github import Github
    except ImportError:
        raise RuntimeError("PyGithub not installed. Run: pip install PyGithub")
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    
    await log_dev_event(bug_data.bug_id, "create_pull_request", "🔗 Creating pull request on GitHub...", "info", workflow_id)
    pr = repo.create_pull(
        title=f"Fix: {bug_data.title} (Bug #{bug_data.bug_id})",
        body=f"Automated PR generated by DevAgent to fix bug #{bug_data.bug_id}.\n\n{bug_data.description}",
        head=branch_name,
        base="main"
    )
    await log_dev_event(bug_data.bug_id, "create_pull_request", "✅ Step 4/5 done. PR: " + pr.html_url, "success", workflow_id)
    logging.info("[create_pull_request] PR created: %s", pr.html_url)
    return pr.html_url


# ─────────────────────────────────────────────────────────────────────────────
# Activity: Update Bug Ticket
# ─────────────────────────────────────────────────────────────────────────────

@activity.defn
async def update_bug_ticket(config: Dict[str, Any]) -> None:
    """Updates Supabase bug ticket with the PR link or error."""
    from app.supabase_client import get_supabase_client
    supabase = get_supabase_client("DEV_AGENT")
    
    bug_id = config["bug_id"]
    pr_url = config.get("pr_url")
    error_msg = config.get("error_msg")
    workflow_id = config.get("workflow_id")
    
    if not supabase:
        logging.warning("[update_bug_ticket] Supabase not configured. Skipping.")
        return

    if pr_url:
        await log_dev_event(bug_id, "update_bug_ticket", "📝 Step 5/5: Posting PR link to Comments and setting status to Review...", "info", workflow_id)
        supabase.table("phwb_bug_comments").insert({
            "bug_id": bug_id,
            "user_id": None,
            "content": f"✅ **Dev Agent** completed a fix and raised a Pull Request.\n\nPlease review it here: {pr_url}",
            "is_internal": False,
        }).execute()
        supabase.table("phwb_bugs").update({"status": "review"}).eq("id", bug_id).execute()
        await log_dev_event(bug_id, "update_bug_ticket", "🎉 Step 5/5 done. PR link posted; status → Review. All done!", "success", workflow_id)
        logging.info("[update_bug_ticket] Comment + status updated for bug #%s", bug_id)

    elif error_msg:
        await log_dev_event(bug_id, "update_bug_ticket", "📝 Posting error to Comments (internal)...", "info", workflow_id)
        supabase.table("phwb_bug_comments").insert({
            "bug_id": bug_id,
            "user_id": None,
            "content": f"❌ **Dev Agent** encountered an error:\n\n```\n{error_msg}\n```",
            "is_internal": True,
        }).execute()
        await log_dev_event(bug_id, "update_bug_ticket", "❌ Error posted to Comments: " + error_msg[:200], "error", workflow_id)
        logging.info("[update_bug_ticket] Error comment posted for bug #%s", bug_id)
