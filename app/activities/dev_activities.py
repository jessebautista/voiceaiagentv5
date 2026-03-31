from temporalio import activity
import os
import re
import socket
import tempfile
import subprocess
import shutil
import logging
import time
from pydantic import BaseModel
from typing import Tuple, Dict, Any, Optional, List

# Default preview URL when running E2E (must match vite preview port, e.g. 4173)
E2E_PREVIEW_URL = "http://localhost:4173"
E2E_PREVIEW_PORT = 4173
E2E_WAIT_READY_TIMEOUT = 60
E2E_BUILD_TIMEOUT = 300
E2E_PLAYWRIGHT_TIMEOUT = 300

class DevActionInput(BaseModel):
    bug_id: int
    title: str
    description: str
    category: str
    status: str
    module_hint: Optional[str] = None
    path_hint: Optional[str] = None
    labels: Optional[List[str]] = None


_AREA_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "artists": ("artist", "artists"),
    "events": ("event", "events", "calendar", "schedule"),
    "venues": ("venue", "venues", "facility", "facilities"),
    "partners": ("partner", "partners", "sponsor", "sponsors"),
    "programs": ("program", "programs"),
    "payroll": ("payroll", "payment", "payments", "timesheet", "timesheets"),
    "reports": ("report", "reports", "analytics"),
    "bugs": ("bug", "bugs", "dev fix", "dev-fix"),
    "settings": ("setting", "settings", "notification", "notifications"),
    "production-managers": ("production manager", "production managers"),
}

_AREA_ROUTE_PREFIXES: Dict[str, str] = {
    "artists": "src/routes/artists/",
    "events": "src/routes/events/",
    "venues": "src/routes/venues/",
    "partners": "src/routes/partners/",
    "programs": "src/routes/programs/",
    "payroll": "src/routes/payroll/",
    "reports": "src/routes/reports/",
    "bugs": "src/routes/bugs/",
    "settings": "src/routes/settings/",
    "production-managers": "src/routes/settings/production-managers/",
}


def _is_ui_category(category: Optional[str]) -> bool:
    c = (category or "").strip().lower()
    return c in ("ui", "ui/ux", "frontend", "ux")


def _infer_primary_area(
    title: Optional[str],
    description: Optional[str],
    module_hint: Optional[str] = None,
    path_hint: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Optional[str]:
    path = (path_hint or "").replace("\\", "/").lower()
    for area, prefix in _AREA_ROUTE_PREFIXES.items():
        if prefix.lower() in path:
            return area

    labels_text = " ".join(labels or [])
    haystack = f"{title or ''} {description or ''} {module_hint or ''} {path_hint or ''} {labels_text}".lower()
    for area, tokens in _AREA_KEYWORDS.items():
        if any(token in haystack for token in tokens):
            return area
    return None


def _expected_ui_prefix(bug_data: DevActionInput) -> Optional[str]:
    path_hint = (bug_data.path_hint or "").replace("\\", "/").strip().lstrip("./")
    if path_hint.startswith("src/routes/"):
        parts = path_hint.split("/")
        if len(parts) >= 3:
            return "/".join(parts[:3]) + "/"
        return "src/routes/"

    area = _infer_primary_area(
        bug_data.title,
        bug_data.description,
        module_hint=bug_data.module_hint,
        path_hint=bug_data.path_hint,
        labels=bug_data.labels,
    )
    if area:
        return _AREA_ROUTE_PREFIXES.get(area)
    return None


def _build_scope_hint(bug_data: DevActionInput) -> str:
    """Create stable, explicit scope guidance for the coding agent."""
    primary_area = _infer_primary_area(
        bug_data.title,
        bug_data.description,
        module_hint=bug_data.module_hint,
        path_hint=bug_data.path_hint,
        labels=bug_data.labels,
    )
    expected_prefix = _expected_ui_prefix(bug_data)
    ui_scoped = _is_ui_category(bug_data.category)
    lines: List[str] = []

    if ui_scoped:
        lines.append("- Treat this as a UI-scoped issue.")
        lines.append("- Restrict edits to UI paths only (`src/routes/**`, `src/lib/components/**`, `src/app.css`, `static/**`, `*.svelte`).")
        lines.append("- Do not modify backend/service/store files unless verification explicitly proves it is required.")
    if bug_data.labels:
        lines.append(f"- Ticket labels: {', '.join(bug_data.labels[:8])}.")
    if bug_data.module_hint:
        lines.append(f"- Module hint: `{bug_data.module_hint}`.")
    if bug_data.path_hint:
        lines.append(f"- Path hint: `{bug_data.path_hint}`.")
    if primary_area:
        lines.append(f"- Primary feature area: `{primary_area}`.")
    if expected_prefix:
        lines.append(f"- Prefer files under `{expected_prefix}` for this issue.")
    if not lines:
        lines.append("- Keep edits narrowly scoped to files directly related to the reported bug.")

    return "## Scope constraints\n" + "\n".join(lines) + "\n"


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
    task += "\n" + _build_scope_hint(bug_data)
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
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a command; returns (returncode, stdout, stderr). Cross-platform."""
    run_env = {**os.environ, **(env or {})}
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=run_env,
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


def _is_ui_path(path: str) -> bool:
    """True if path is considered a UI file (Svelte, routes, components, app.css, optional static)."""
    p = path.replace("\\", "/").strip().lstrip("./")
    if not p:
        return False
    if p.endswith(".svelte"):
        return True
    if p.startswith("src/routes/"):
        return True
    if p.startswith("src/lib/components/"):
        return True
    if p == "src/app.css":
        return True
    if p.startswith("static/"):
        return True
    return False


def _is_only_ui_changes(modified_files: List[str]) -> bool:
    """True iff there is at least one modified file and every one is a UI path."""
    return len(modified_files) > 0 and all(_is_ui_path(p) for p in modified_files)


def _ui_scope_violations(modified_files: List[str], bug_data: Optional[DevActionInput]) -> List[str]:
    """Return violations for UI-scoped bugs. Empty list means scope looks valid."""
    if not bug_data or not _is_ui_category(bug_data.category):
        return []

    violations: List[str] = []
    if not modified_files:
        return ["No files were modified."]

    non_ui = [p for p in modified_files if not _is_ui_path(p)]
    if non_ui:
        violations.append("UI-scoped bug modified non-UI paths: " + ", ".join(non_ui[:20]))

    expected_prefix = _expected_ui_prefix(bug_data)
    if expected_prefix:
        touches_expected_area = any(p.replace("\\", "/").startswith(expected_prefix) for p in modified_files)
        if not touches_expected_area:
            violations.append(
                "UI-scoped bug did not touch expected area "
                f"`{expected_prefix}` (changed: {', '.join(modified_files[:20])})."
            )

    return violations


def _wait_for_preview_ready(port: int = E2E_PREVIEW_PORT, timeout_sec: int = E2E_WAIT_READY_TIMEOUT) -> bool:
    """Return True when port is accepting connections."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except (OSError, socket.error):
            time.sleep(1)
    return False


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
            # Normalize common prefixes from Vite/Svelte output, e.g. "File: /abs/path/to/file.svelte"
            path_raw = re.sub(r"^(?:File|file)\s*:\s*", "", path_raw).strip()
            path_raw = re.sub(r"^at\s+", "", path_raw).strip()
            if repo_norm in path_raw:
                path_raw = path_raw[path_raw.index(repo_norm) :]
            if os.path.isabs(path_raw) and path_raw.startswith(repo_norm):
                rel = path_raw[len(repo_norm) :].lstrip(os.sep).replace("\\", "/").lstrip("./")
            else:
                rel = path_raw.replace("\\", "/").lstrip("./")
            if rel and rel not in seen:
                seen.add(rel)
        except Exception:
            continue
    return list(seen)


def _path_matches_modified(err_path: str, modified_path: str) -> bool:
    """Flexible path match for relative/absolute and parser variants."""
    e = err_path.replace("\\", "/").lstrip("./")
    m = modified_path.replace("\\", "/").lstrip("./")
    return e == m or e.endswith("/" + m) or m.endswith("/" + e)


def _output_mentions_modified_file(full_output: str, modified_files: List[str]) -> bool:
    """Best-effort guard: if failed output mentions a modified file, don't bypass."""
    out = full_output.replace("\\", "/")
    for p in modified_files:
        rel = p.replace("\\", "/").lstrip("./")
        base = os.path.basename(rel)
        if rel and rel in out:
            return True
        if base and base in out:
            return True
    return False


@activity.defn
async def verify_fix(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Runs install (if needed) and project check in the cloned repo. Payload: repo_path, bug_id, workflow_id (optional)."""
    repo_path = payload["repo_path"]
    bug_id = payload["bug_id"]
    bug_data_raw = payload.get("bug_data")
    bug_data: Optional[DevActionInput] = None
    if isinstance(bug_data_raw, dict):
        try:
            bug_data = DevActionInput(**bug_data_raw)
        except Exception:
            bug_data = None
    workflow_id = payload.get("workflow_id")
    await log_dev_event(bug_id, "verify_fix", "🔍 Step 3/5: Verifying fix (dependencies + project check)...", "info", workflow_id)
    logging.info("[verify_fix] Starting verification in %s", repo_path)

    try:
        modified = _get_modified_files(repo_path)
        scope_violations = _ui_scope_violations(modified, bug_data)
        if scope_violations:
            scope_msg = (
                "Scope violation detected before verification.\n"
                + "\n".join(f"- {v}" for v in scope_violations[:20])
            )
            await log_dev_event(
                bug_id,
                "verify_fix",
                "❌ " + scope_msg[:2000],
                "error",
                workflow_id,
            )
            return False, scope_msg

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
            # Optional E2E: only when DEV_AGENT_RUN_E2E is set and (only UI changes or DEV_AGENT_E2E_ALWAYS).
            run_e2e_env = (os.getenv("DEV_AGENT_RUN_E2E") or "").strip().lower() in ("1", "true", "yes")
            e2e_always = (os.getenv("DEV_AGENT_E2E_ALWAYS") or "").strip().lower() in ("1", "true", "yes")
            only_ui = _is_only_ui_changes(modified)

            if not run_e2e_env:
                return True, None
            if not only_ui and not e2e_always:
                await log_dev_event(
                    bug_id, "verify_fix",
                    "Skipping E2E (changes are not only UI).",
                    "info", workflow_id,
                )
                logging.info("[verify_fix] Skipping E2E (changes are not only UI)")
                return True, None

            await log_dev_event(
                bug_id, "verify_fix",
                "Running E2E (changes are only UI).",
                "info", workflow_id,
            )
            logging.info("[verify_fix] Running E2E (changes are only UI)")

            # Build
            build_ok = False
            for build_cmd, name in (
                (["bun", "run", "build"], "bun run build"),
                (["npm", "run", "build"], "npm run build"),
            ):
                code, out, err = _run_cmd(build_cmd, repo_path, timeout_sec=E2E_BUILD_TIMEOUT)
                if code == 0:
                    build_ok = True
                    break
                logging.warning("[verify_fix] %s failed: %s", name, (err or out)[-500:])
            if not build_ok:
                msg = "E2E build failed (bun run build / npm run build)."
                await log_dev_event(bug_id, "verify_fix", "❌ " + msg, "error", workflow_id)
                return False, msg

            # Start preview in background
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
                await log_dev_event(bug_id, "verify_fix", "❌ Could not start preview (bun/npm not found).", "error", workflow_id)
                return False, "E2E: could not start preview server."

            try:
                if not _wait_for_preview_ready(E2E_PREVIEW_PORT, E2E_WAIT_READY_TIMEOUT):
                    stderr = (preview_proc.stderr and preview_proc.stderr.read()) or b""
                    await log_dev_event(
                        bug_id, "verify_fix",
                        "❌ Preview did not become ready in time.",
                        "error", workflow_id,
                    )
                    return False, "E2E: preview server did not become ready. " + (stderr.decode("utf-8", errors="replace")[-500:] or "")

                code, out, err = _run_cmd(
                    ["npx", "playwright", "test", "--project=smoke"],
                    repo_path,
                    timeout_sec=E2E_PLAYWRIGHT_TIMEOUT,
                    capture=True,
                    env={"BASE_URL": E2E_PREVIEW_URL},
                )
                combined = (out + "\n" + err).strip()
                if code != 0:
                    last_chars = combined[-500:] if combined else "No output"
                    msg = "E2E failed: " + last_chars
                    await log_dev_event(bug_id, "verify_fix", "❌ " + msg[:2000], "error", workflow_id)
                    return False, msg
            finally:
                preview_proc.terminate()
                try:
                    preview_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    preview_proc.kill()

            await log_dev_event(bug_id, "verify_fix", "✅ E2E smoke passed.", "success", workflow_id)
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
        errors_in_modified = (
            [p for p in error_paths if any(_path_matches_modified(p, m) for m in modified)]
            if modified
            else []
        )
        logging.info(
            "[verify_fix] check failed. modified=%s error_paths=%s errors_in_modified=%s",
            modified[:20],
            error_paths[:20],
            errors_in_modified[:20],
        )

        if modified and error_paths and not errors_in_modified:
            # Safety guard: don't bypass when output mentions any modified file.
            if _output_mentions_modified_file(full_output, modified):
                error_msg = "Project check output references modified files; not bypassing.\n\n" + full_output[:1500]
                await log_dev_event(
                    bug_id, "verify_fix",
                    "❌ Verification failed:\n" + error_msg[:2000],
                    "error",
                    workflow_id,
                )
                return False, error_msg
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

def _suggest_fix_for_error(error_msg: str) -> str:
    """
    Generate a practical suggestion block for common dev-fix failures.
    This is used when the workflow exhausts retries and posts a final comment.
    """
    msg = (error_msg or "").lower()

    if "failed to verify fix after" in msg or "verification failed" in msg:
        return (
            "- Review the **last verification output** and fix the first concrete compile/type error.\n"
            "- Re-run `bun run check` (or `npm run check`) locally and ensure it passes.\n"
            "- If the error is baseline/unrelated, isolate changed files and confirm no modified file appears in the failing paths."
        )
    if "pgrst200" in msg or "could not find a relationship" in msg:
        return (
            "- Add/fix the missing foreign-key relationship in Supabase via a migration.\n"
            "- If the relationship is optional, update the query to avoid implicit relationship expansion.\n"
            "- Re-run the affected page/API call to confirm schema cache now resolves the relation."
        )
    if "406" in msg or "no rows updated" in msg or "not found or update affected no rows" in msg:
        return (
            "- Check row existence and RLS/policy visibility for the target record.\n"
            "- Prefer server-side update route for privileged updates when appropriate.\n"
            "- Confirm update returns a row in Supabase before treating as success."
        )
    if "command not found" in msg or "bun or npm not in path" in msg:
        return (
            "- Ensure Node tooling is installed and available in PATH (`bun` or `npm`).\n"
            "- Verify `package.json` exists in the repository root.\n"
            "- Retry workflow after dependency tooling is confirmed."
        )
    if "dependency install failed" in msg:
        return (
            "- Run `bun install` and `npm install` manually to capture exact installer error.\n"
            "- Resolve lockfile or registry/auth issues, then retry the workflow."
        )
    if "e2e build failed" in msg or "e2e failed" in msg or "could not start preview" in msg:
        return (
            "- Validate `bun run build`/`npm run build` succeeds locally first.\n"
            "- Confirm preview starts on expected port and `BASE_URL` is reachable.\n"
            "- Re-run smoke tests after preview readiness is stable."
        )
    if "failed to clone repository" in msg or "git clone" in msg:
        return (
            "- Validate `DEV_AGENT_GITHUB_ACCESS_TOKEN` and `DEV_AGENT_GITHUB_REPO_URL`.\n"
            "- Confirm token permissions include repository read/write.\n"
            "- Retry after network/auth stability is verified."
        )
    if "failed to commit/push" in msg or "create pull request" in msg:
        return (
            "- Check git push permissions and branch protection constraints.\n"
            "- Validate GitHub token scopes for pushing and PR creation.\n"
            "- Retry once remote access and branch state are confirmed."
        )

    return (
        "- Inspect the stack trace/log snippet and identify the first deterministic failure.\n"
        "- Reproduce locally with the same command/environment to isolate root cause.\n"
        "- Apply a minimal fix, then rerun verification before retriggering workflow."
    )


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
        suggested_fix = _suggest_fix_for_error(error_msg)
        await log_dev_event(bug_id, "update_bug_ticket", "📝 Posting error to Comments (internal)...", "info", workflow_id)
        supabase.table("phwb_bug_comments").insert({
            "bug_id": bug_id,
            "user_id": None,
            "content": (
                "❌ **Dev Agent** could not complete this fix after retry attempts.\n\n"
                "**Error:**\n"
                f"```\n{error_msg}\n```\n\n"
                "**Suggested fix:**\n"
                f"{suggested_fix}"
            ),
            "is_internal": True,
        }).execute()
        await log_dev_event(
            bug_id,
            "update_bug_ticket",
            "❌ Error + suggested fix posted to Comments: " + error_msg[:200],
            "error",
            workflow_id,
        )
        logging.info("[update_bug_ticket] Error comment posted for bug #%s", bug_id)
