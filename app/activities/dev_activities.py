from temporalio import activity
import os
import re
import json
import socket
import tempfile
import subprocess
import shutil
import logging
import time
from urllib import parse, request
from pydantic import BaseModel
from typing import Tuple, Dict, Any, Optional, List

# Default preview URL when running E2E (must match vite preview port, e.g. 4173)
E2E_PREVIEW_URL = "http://localhost:4173"
E2E_PREVIEW_PORT = 4173
E2E_WAIT_READY_TIMEOUT = 60
E2E_BUILD_TIMEOUT = 300
E2E_PLAYWRIGHT_TIMEOUT = 300
PREFLIGHT_CHECK_TIMEOUT = 180
PREFLIGHT_INSTALL_TIMEOUT = 300

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

_AREA_DB_TABLES: Dict[str, str] = {
    "artists": "phwb_artists",
    "events": "phwb_events",
    "venues": "phwb_venues",
    "partners": "phwb_partners",
    "programs": "phwb_programs",
    "payroll": "phwb_payroll",
    "reports": "phwb_reports",
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
    if ui_scoped and expected_prefix:
        lines.append(
            f"- Allowed edit paths for this ticket: `{expected_prefix}**`, `src/lib/components/**`, `src/app.css`, `static/**`."
        )
        lines.append("- Avoid editing other route folders unless explicitly required by the issue.")
    if not lines:
        lines.append("- Keep edits narrowly scoped to files directly related to the reported bug.")

    return "## Scope constraints\n" + "\n".join(lines) + "\n"


def _extract_candidate_columns(bug_data: DevActionInput) -> List[str]:
    text = f"{bug_data.title} {bug_data.description or ''}"
    candidates: List[str] = []
    for m in re.finditer(r"\b([a-z][a-z0-9_]{2,})\b", text):
        token = m.group(1).strip().lower()
        if "_" not in token:
            continue
        if token.startswith("phwb_"):
            continue
        candidates.append(token)
    seen: set[str] = set()
    deduped: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped[:12]


def _infer_candidate_tables(bug_data: DevActionInput) -> List[str]:
    text = f"{bug_data.title} {bug_data.description or ''} {bug_data.path_hint or ''} {bug_data.module_hint or ''}"
    tables: List[str] = []
    for m in re.finditer(r"\b(phwb_[a-z0-9_]+)\b", text, re.IGNORECASE):
        tables.append(m.group(1).lower())

    area = _infer_primary_area(
        bug_data.title,
        bug_data.description,
        module_hint=bug_data.module_hint,
        path_hint=bug_data.path_hint,
        labels=bug_data.labels,
    )
    if area and area in _AREA_DB_TABLES:
        tables.append(_AREA_DB_TABLES[area])

    seen: set[str] = set()
    deduped: List[str] = []
    for t in tables:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:6]


def _looks_missing_table_error(msg: str) -> bool:
    m = (msg or "").lower()
    return (
        "could not find the table" in m
        or "relation" in m and "does not exist" in m
        or "pgrst205" in m
    )


def _looks_missing_column_error(msg: str) -> bool:
    m = (msg or "").lower()
    return ("column" in m and "does not exist" in m) or "pgrst204" in m


def _run_schema_audit(bug_data: DevActionInput) -> Dict[str, Any]:
    """
    Best-effort schema audit before coding:
    - checks candidate tables inferred from bug metadata
    - checks candidate snake_case columns (if any) against those tables
    Never raises; returns structured findings.
    """
    from app.supabase_client import get_supabase_client

    supabase = get_supabase_client("DEV_AGENT")
    result: Dict[str, Any] = {
        "tables_checked": [],
        "tables_missing": [],
        "columns_checked": [],
        "columns_missing": [],
        "errors": [],
    }
    if not supabase:
        result["errors"].append("supabase_not_configured")
        return result

    tables = _infer_candidate_tables(bug_data)
    columns = _extract_candidate_columns(bug_data)

    existing_tables: List[str] = []
    for table in tables:
        result["tables_checked"].append(table)
        try:
            supabase.from_(table).select("*").limit(1).execute()
            existing_tables.append(table)
        except Exception as e:
            msg = str(e)
            if _looks_missing_table_error(msg):
                result["tables_missing"].append(table)
            else:
                result["errors"].append(f"table_check:{table}:{msg[:140]}")

    for table in existing_tables:
        for col in columns:
            result["columns_checked"].append(f"{table}.{col}")
            try:
                supabase.from_(table).select(col).limit(1).execute()
            except Exception as e:
                msg = str(e)
                if _looks_missing_column_error(msg):
                    result["columns_missing"].append(f"{table}.{col}")
                elif _looks_missing_table_error(msg):
                    result["tables_missing"].append(table)
                else:
                    result["errors"].append(f"column_check:{table}.{col}:{msg[:140]}")

    # Dedupe lists
    for key in ("tables_checked", "tables_missing", "columns_checked", "columns_missing", "errors"):
        seen: set[str] = set()
        deduped: List[str] = []
        for item in result[key]:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        result[key] = deduped
    return result


def _infer_ticket_type(bug_data: DevActionInput) -> str:
    text = f"{bug_data.title} {bug_data.description or ''}".lower()
    if any(k in text for k in ("error", "fail", "broken", "bug", "not working", "exception")):
        return "bug_fix"
    if any(k in text for k in ("improve", "enhance", "optimize", "refine", "update")):
        return "enhancement"
    if any(k in text for k in ("add", "create", "new", "support", "enable", "implement")):
        return "feature"
    return "bug_fix"


def _build_implementation_contract(
    bug_data: DevActionInput,
    last_error: Optional[str] = None,
) -> Dict[str, Any]:
    text = f"{bug_data.title} {bug_data.description or ''} {bug_data.category}".lower()
    err = (last_error or "").lower()
    layers: List[str] = []

    ui_signals = ("ui", "ux", "page", "screen", "button", "form", "modal", "tab", "toggle", "table")
    backend_signals = ("api", "endpoint", "server", "backend", "store", "service", "hook")
    db_signals = ("supabase", "database", "db", "table", "column", "relation", "migration", "schema", "foreign key")

    if any(s in text for s in ui_signals) or _is_ui_category(bug_data.category):
        layers.append("UI")
    if any(s in text for s in backend_signals):
        layers.append("API/Backend")
    if any(s in text for s in db_signals):
        layers.append("DB")

    # Retry context can indicate additional layers that must be addressed.
    if "src/lib/stores/" in err or "/+server.ts" in err or "api/" in err:
        if "API/Backend" not in layers:
            layers.append("API/Backend")
    if "pgrst" in err or "supabase" in err or "schema cache" in err or "relation" in err:
        if "DB" not in layers:
            layers.append("DB")

    if not layers:
        # Non-dev reports are often ambiguous; default to UI first but allow expansion.
        layers.append("UI")

    requires_migration = "DB" in layers
    checks: List[str] = [
        "User-visible behavior from the ticket works end-to-end in the affected flow.",
        "No syntax/type errors are introduced in modified files.",
        "Changes remain scoped to files necessary for this ticket.",
    ]
    if "API/Backend" in layers:
        checks.append("API/store/service behavior matches the UI behavior and persists correctly.")
    if "DB" in layers:
        checks.append("Required migration is added under `phwb-testrepo/migrations` and schema usage is consistent.")

    return {
        "ticket_type": _infer_ticket_type(bug_data),
        "impacted_layers": layers,
        "requires_migration": requires_migration,
        "acceptance_checks": checks,
    }


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


def _looks_like_migration_file(path: str) -> bool:
    """Return True if a path looks like a SQL migration file."""
    p = path.replace("\\", "/").lstrip("./").lower()
    if not p.endswith(".sql"):
        return False
    if p.startswith("migrations/"):
        return True
    if p.startswith("supabase/migrations/"):
        return True
    if p.startswith("db/migrations/"):
        return True
    if p.startswith("database/migrations/"):
        return True
    if p.startswith("prisma/migrations/"):
        return True
    return "/migrations/" in p


def _detect_migration_files(modified_files: List[str]) -> List[str]:
    """Return changed SQL migration files."""
    return [
        p.replace("\\", "/").lstrip("./")
        for p in modified_files
        if _looks_like_migration_file(p)
    ]


def _db_change_metadata(modified_files: List[str]) -> Dict[str, Any]:
    migration_files = _detect_migration_files(modified_files)
    return {
        "db_changes_detected": len(migration_files) > 0,
        "migration_files": migration_files[:50],
        "requires_db_confirmation": len(migration_files) > 0,
    }


def _is_backend_path(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./")
    if p.startswith("src/routes/api/"):
        return True
    if p.endswith("+server.ts") or p.endswith("+server.js"):
        return True
    if p.startswith("src/lib/stores/") or p.startswith("src/lib/services/"):
        return True
    if p in ("src/hooks.server.ts", "src/hooks.server.js"):
        return True
    return False


def _functional_completeness_warnings(
    modified_files: List[str],
    bug_data: Optional[DevActionInput],
) -> List[str]:
    """
    Non-blocking completeness checks (phase B2 first pass).
    These warnings help catch likely partial implementations from non-dev tickets.
    """
    if not modified_files or not bug_data:
        return []

    contract = _build_implementation_contract(bug_data)
    impacted = set(contract.get("impacted_layers", []))
    migrations = _detect_migration_files(modified_files)

    ui_touched = any(_is_ui_path(p) for p in modified_files)
    backend_touched = any(_is_backend_path(p) for p in modified_files)
    warnings: List[str] = []

    # Reduce noisy warnings: DB+UI tickets may legitimately not touch backend files
    # when schema change is captured via migration and UI reads existing data paths.
    skip_backend_warning = bool(contract.get("requires_migration") and migrations)
    if "API/Backend" in impacted and not backend_touched and not skip_backend_warning:
        warnings.append(
            "Implementation contract expects API/Backend work, but modified files appear UI-only."
        )

    if "UI" in impacted and not ui_touched:
        warnings.append(
            "Implementation contract expects UI work, but no UI paths were modified."
        )

    if contract.get("requires_migration") and not migrations:
        warnings.append(
            "Implementation contract indicates DB/schema changes, but no migration file was modified."
        )

    # If DB paths were touched indirectly via backend/store but no migration was included, call it out.
    if "DB" in impacted and backend_touched and not migrations:
        warnings.append(
            "DB-related ticket context detected without migration updates; confirm schema already exists."
        )

    return warnings[:10]


def _functional_completeness_failures(
    modified_files: List[str],
    bug_data: Optional[DevActionInput],
) -> List[str]:
    """
    Critical completeness checks that can be promoted to blocking behavior.
    """
    if not modified_files or not bug_data:
        return []

    contract = _build_implementation_contract(bug_data)
    impacted = set(contract.get("impacted_layers", []))
    migrations = _detect_migration_files(modified_files)
    ui_touched = any(_is_ui_path(p) for p in modified_files)
    backend_touched = any(_is_backend_path(p) for p in modified_files)

    failures: List[str] = []
    skip_backend_failure = bool(contract.get("requires_migration") and migrations)
    if "API/Backend" in impacted and not backend_touched and not skip_backend_failure:
        failures.append("Expected API/Backend layer changes were not implemented.")
    if "UI" in impacted and not ui_touched:
        failures.append("Expected UI layer changes were not implemented.")
    if contract.get("requires_migration") and not migrations:
        failures.append("DB/schema changes appear required, but no migration file was added.")
    return failures[:10]


def _detect_missing_schema_references(output: str) -> List[str]:
    """
    Parse verify/check output for likely DB schema object issues.
    Non-blocking signal used to suggest migration/schema follow-up.
    """
    text = output or ""
    findings: List[str] = []

    # PostgREST missing table in schema cache
    for m in re.finditer(r"Could not find the table '([^']+)' in the schema cache", text, re.IGNORECASE):
        findings.append(f"missing_table:{m.group(1)}")

    # PostgREST missing relationship in schema cache
    for m in re.finditer(
        r"Could not find a relationship between '([^']+)' and '([^']+)' in the schema cache",
        text,
        re.IGNORECASE,
    ):
        findings.append(f"missing_relationship:{m.group(1)}->{m.group(2)}")

    # PostgreSQL relation does not exist
    for m in re.finditer(r"relation \"([^\"]+)\" does not exist", text, re.IGNORECASE):
        findings.append(f"missing_relation:{m.group(1)}")

    # Dedupe while preserving order
    seen: set[str] = set()
    deduped: List[str] = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[:20]


async def log_workflow_state(
    bug_id: int,
    state: str,
    detail: str,
    workflow_id: Optional[str] = None,
    post_comment: bool = True,
) -> None:
    """
    Log standardized workflow state transitions and optionally surface them in bug comments.
    This avoids changing the phwb_bugs status enum while making progress visible.
    """
    state_msg = f"🔖 Workflow state → `{state}`. {detail}".strip()
    await log_dev_event(bug_id, "workflow_state", state_msg, "info", workflow_id)

    if not post_comment:
        return
    try:
        from app.supabase_client import get_supabase_client

        supabase = get_supabase_client("DEV_AGENT")
        if not supabase:
            return
        supabase.table("phwb_bug_comments").insert(
            {
                "bug_id": bug_id,
                "user_id": None,
                "content": state_msg,
                "is_internal": False,
            }
        ).execute()
    except Exception as e:
        logging.warning("log_workflow_state comment insert failed: %s", e)


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

def _build_dev_fix_instruction(
    repo_path: str,
    bug_data: DevActionInput,
    last_error: Optional[str],
    schema_audit: Optional[Dict[str, Any]] = None,
) -> str:
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

    contract = _build_implementation_contract(bug_data, last_error=last_error)

    task = (
        f"## Bug to fix\n"
        f"- **ID:** #{bug_data.bug_id}\n"
        f"- **Title:** {bug_data.title}\n"
        f"- **Description:** {bug_data.description or '(No description provided)'}\n"
        f"- **Category:** {bug_data.category}\n\n"
        f"## Implementation contract (create before editing)\n"
        f"- **Ticket type:** {contract['ticket_type']}\n"
        f"- **Impacted layers:** {', '.join(contract['impacted_layers'])}\n"
        f"- **Migration likely required:** {'yes' if contract['requires_migration'] else 'no'}\n"
        f"- **Acceptance checks:**\n"
        + "".join(f"  - {c}\n" for c in contract["acceptance_checks"])
        + "\n"
        f"## What to do\n"
        f"1. Locate the code that causes or relates to this bug.\n"
        f"2. Make at least one concrete code change (use the write_file tool) that fixes or addresses the issue. Prefer a minimal, single-file change when possible.\n"
        f"3. Do not break the project: the change must pass the project's check (e.g. `bun run check` or `npm run check`).\n"
    )
    if schema_audit:
        task += (
            "\n## DB schema audit (before coding)\n"
            f"- Tables checked: {', '.join(schema_audit.get('tables_checked', [])) or '(none)'}\n"
            f"- Missing tables: {', '.join(schema_audit.get('tables_missing', [])) or '(none)'}\n"
            f"- Columns checked: {', '.join(schema_audit.get('columns_checked', [])) or '(none)'}\n"
            f"- Missing columns: {', '.join(schema_audit.get('columns_missing', [])) or '(none)'}\n"
            f"- Audit errors: {', '.join(schema_audit.get('errors', [])) or '(none)'}\n"
            "- If required table/column is missing, add a migration before claiming completion.\n"
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

    contract = _build_implementation_contract(bug_data, last_error=last_error)
    await log_dev_event(
        bug_data.bug_id,
        "analyze_and_code",
        (
            "🧾 Implementation contract: "
            f"type={contract['ticket_type']} | "
            f"layers={','.join(contract['impacted_layers'])} | "
            f"migration_required={'yes' if contract['requires_migration'] else 'no'}"
        ),
        "info",
        workflow_id,
    )

    schema_audit = _run_schema_audit(bug_data)
    await log_dev_event(
        bug_data.bug_id,
        "analyze_and_code",
        (
            "🧪 Schema audit: "
            f"tables_checked={len(schema_audit.get('tables_checked', []))} | "
            f"tables_missing={','.join(schema_audit.get('tables_missing', []) or ['(none)'])} | "
            f"columns_missing={','.join(schema_audit.get('columns_missing', []) or ['(none)'])}"
        ),
        "info",
        workflow_id,
    )

    prompt = _build_dev_fix_instruction(repo_path, bug_data, last_error, schema_audit=schema_audit)
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


def _git_default_base_ref(repo_path: str) -> str:
    """
    Resolve a reliable base ref for branch-aware diffs.
    Prefers origin/HEAD, then origin/main, then origin/master.
    """
    code, out, _ = _run_cmd(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        repo_path,
        timeout_sec=10,
    )
    if code == 0 and (out or "").strip():
        return (out or "").strip()

    for candidate in ("origin/main", "origin/master"):
        verify_code, _, _ = _run_cmd(
            ["git", "show-ref", "--verify", f"refs/remotes/{candidate}"],
            repo_path,
            timeout_sec=10,
        )
        if verify_code == 0:
            return candidate
    return "origin/main"


def _parse_git_path_list(raw: str) -> List[str]:
    return [
        line.strip().replace("\\", "/").lstrip("./")
        for line in (raw or "").splitlines()
        if line.strip()
    ]


def _get_modified_files(repo_path: str) -> List[str]:
    """
    Return changed paths relative to repo root with branch-aware coverage.

    Sources (union):
    1) Working tree/index changes vs HEAD.
    2) Untracked files (important for newly-created migrations).
    3) Branch diff vs base (origin/HEAD -> main/master fallback).
    """
    files: List[str] = []

    code_head, out_head, _ = _run_cmd(["git", "diff", "--name-only", "HEAD"], repo_path, timeout_sec=10)
    if code_head == 0:
        files.extend(_parse_git_path_list(out_head))

    code_untracked, out_untracked, _ = _run_cmd(
        ["git", "ls-files", "--others", "--exclude-standard"],
        repo_path,
        timeout_sec=10,
    )
    if code_untracked == 0:
        files.extend(_parse_git_path_list(out_untracked))

    base_ref = _git_default_base_ref(repo_path)
    code_base, out_base, _ = _run_cmd(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        repo_path,
        timeout_sec=10,
    )
    if code_base == 0:
        files.extend(_parse_git_path_list(out_base))

    deduped: List[str] = []
    seen: set[str] = set()
    for p in files:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


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
        allowed_prefixes = (expected_prefix, "src/lib/components/", "static/")
        ui_outside_allowlist = [
            p
            for p in modified_files
            if _is_ui_path(p)
            and p.replace("\\", "/") != "src/app.css"
            and not p.replace("\\", "/").startswith(allowed_prefixes)
        ]
        if ui_outside_allowlist:
            violations.append(
                "UI-scoped bug modified UI files outside allowlist: "
                + ", ".join(ui_outside_allowlist[:20])
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


def _run_project_check(repo_path: str, timeout_sec: int = PREFLIGHT_CHECK_TIMEOUT) -> Tuple[bool, str]:
    """
    Run project check with bun/npm fallback.
    Returns (passed, combined_output_from_last_attempt_or_successful_run).
    """
    last_out, last_err = "", ""
    for check_cmd in (["bun", "run", "check"], ["npm", "run", "check"]):
        code, out, err = _run_cmd(check_cmd, repo_path, timeout_sec=timeout_sec)
        last_out, last_err = out, err
        if code == 0:
            return True, (out + "\n" + err).strip()
    return False, (last_out + "\n" + last_err).strip()


def _is_tooling_failure_output(text: str) -> bool:
    t = (text or "").lower()
    return any(
        token in t
        for token in (
            "command not found",
            "svelte-kit: command not found",
            "bun or npm not in path",
            "could not determine executable to run",
            "npm err! enoent",
            "enoent: no such file or directory",
        )
    )


def _ensure_dependencies_for_check(repo_path: str) -> Tuple[bool, str]:
    """
    Ensure dependencies are available before running baseline check.
    Returns (ready, reason), where reason explains skip/failure context.
    """
    node_modules_path = os.path.join(repo_path, "node_modules")
    if os.path.isdir(node_modules_path):
        return True, "already_installed"

    last_output = ""
    for install_cmd in (["bun", "install"], ["npm", "install", "--no-audit", "--no-fund"]):
        code, out, err = _run_cmd(install_cmd, repo_path, timeout_sec=PREFLIGHT_INSTALL_TIMEOUT)
        combined = (out + "\n" + err).strip()
        if code == 0:
            return True, "installed_for_preflight"
        last_output = combined

    if _is_tooling_failure_output(last_output):
        return False, "tooling_unavailable"
    return False, "dependency_install_failed"


def _normalize_check_path(path_raw: str, repo_norm: str) -> Optional[str]:
    """Normalize a check output path to repo-relative unix-style form."""
    raw = (path_raw or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^(?:File|file)\s*:\s*", "", raw).strip()
    raw = re.sub(r"^at\s+", "", raw).strip()
    if not raw:
        return None

    if repo_norm in raw:
        raw = raw[raw.index(repo_norm) :]

    if os.path.isabs(raw) and raw.startswith(repo_norm):
        rel = raw[len(repo_norm) :].lstrip(os.sep).replace("\\", "/").lstrip("./")
    else:
        rel = raw.replace("\\", "/").lstrip("./")
    return rel or None


def _parse_check_diagnostics(full_output: str, repo_path: str) -> List[Tuple[str, str]]:
    """
    Parse diagnostics as (relative_path, severity) where severity is:
    - "error"
    - "warning"
    - "unknown"
    """
    repo_norm = os.path.normpath(repo_path).rstrip(os.sep)
    if not repo_norm:
        return []

    lines = (full_output or "").splitlines()
    diagnostics: List[Tuple[str, str]] = []
    i = 0
    path_re = re.compile(r"^(.+?):(\d+):(\d+)\s*$")
    inline_re = re.compile(r"^(.+?):(\d+):(\d+)\s+(Error|Warn|Warning)\s*:", re.IGNORECASE)

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        rel_path: Optional[str] = None
        severity = "unknown"

        inline = inline_re.match(line)
        if inline:
            rel_path = _normalize_check_path(inline.group(1), repo_norm)
            level = inline.group(4).lower()
            severity = "error" if level == "error" else "warning"
            i += 1
        else:
            m = path_re.match(line)
            if not m:
                i += 1
                continue
            rel_path = _normalize_check_path(m.group(1), repo_norm)
            j = i + 1
            while j < len(lines):
                probe = lines[j].strip()
                if not probe:
                    j += 1
                    continue
                if path_re.match(probe) or inline_re.match(probe):
                    break
                if "Error:" in probe:
                    severity = "error"
                    break
                if "Warn:" in probe or "Warning:" in probe:
                    severity = "warning"
                    break
                j += 1
            i += 1

        if rel_path:
            diagnostics.append((rel_path, severity))

    return diagnostics


@activity.defn
async def preflight_repository_check(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run a baseline check before any agent edits.
    Returns:
      - baseline_ok: bool
      - baseline_error_paths: List[str]
      - baseline_summary: str
    """
    repo_path = payload["repo_path"]
    bug_id = payload["bug_id"]
    workflow_id = payload.get("workflow_id")
    await log_dev_event(
        bug_id,
        "preflight_repository_check",
        "🧪 Preflight: running baseline project check before edits...",
        "info",
        workflow_id,
    )
    deps_ready, deps_reason = _ensure_dependencies_for_check(repo_path)
    if not deps_ready:
        summary = (
            "Preflight skipped: dependencies/tooling not ready before baseline check "
            f"({deps_reason})."
        )
        await log_dev_event(
            bug_id,
            "preflight_repository_check",
            "⏭️ " + summary,
            "warning",
            workflow_id,
        )
        return {
            "preflight_status": "skipped",
            "preflight_reason": deps_reason,
            "baseline_ok": True,
            "baseline_error_paths": [],
            "baseline_summary": summary,
        }

    passed, output = _run_project_check(repo_path, timeout_sec=PREFLIGHT_CHECK_TIMEOUT)
    if passed:
        await log_dev_event(
            bug_id,
            "preflight_repository_check",
            "✅ Preflight baseline check passed.",
            "success",
            workflow_id,
        )
        return {
            "preflight_status": "passed",
            "preflight_reason": deps_reason,
            "baseline_ok": True,
            "baseline_error_paths": [],
            "baseline_summary": "",
        }

    if _is_tooling_failure_output(output):
        summary = "Preflight skipped: tooling failure while running baseline check."
        await log_dev_event(
            bug_id,
            "preflight_repository_check",
            "⏭️ " + summary,
            "warning",
            workflow_id,
        )
        return {
            "preflight_status": "skipped",
            "preflight_reason": "tooling_failure_during_check",
            "baseline_ok": True,
            "baseline_error_paths": [],
            "baseline_summary": (output or summary)[:4000],
        }

    error_paths = _parse_check_error_paths(output, repo_path)
    summary = (
        "Baseline project check failed before agent edits. "
        f"Detected {len(error_paths)} error path(s)."
    )
    await log_dev_event(
        bug_id,
        "preflight_repository_check",
        "⚠️ " + summary,
        "warning",
        workflow_id,
    )
    return {
        "preflight_status": "failed",
        "preflight_reason": "baseline_check_failed",
        "baseline_ok": False,
        "baseline_error_paths": error_paths[:500],
        "baseline_summary": (output or summary)[:4000],
    }


def _parse_check_error_paths(full_output: str, repo_path: str) -> List[str]:
    """Return unique relative paths that have Error diagnostics."""
    seen: set[str] = set()
    for rel_path, severity in _parse_check_diagnostics(full_output, repo_path):
        if severity == "error" and rel_path not in seen:
            seen.add(rel_path)
    return list(seen)


def _path_matches_modified(err_path: str, modified_path: str) -> bool:
    """Flexible path match for relative/absolute and parser variants."""
    e = err_path.replace("\\", "/").lstrip("./")
    m = modified_path.replace("\\", "/").lstrip("./")
    return e == m or e.endswith("/" + m) or m.endswith("/" + e)


def _output_mentions_modified_file(full_output: str, modified_files: List[str]) -> bool:
    """Guard based on exact normalized path mentions only (no basename matching)."""
    out = full_output.replace("\\", "/")
    for p in modified_files:
        rel = p.replace("\\", "/").lstrip("./")
        if not rel:
            continue
        if rel in out or ("/" + rel) in out:
            return True
    return False


@activity.defn
async def verify_fix(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Runs install (if needed) and project check in the cloned repo. Payload: repo_path, bug_id, workflow_id (optional)."""
    repo_path = payload["repo_path"]
    bug_id = payload["bug_id"]
    bug_data_raw = payload.get("bug_data")
    baseline_error_paths_raw = payload.get("baseline_error_paths") or []
    baseline_error_paths = [
        str(p).replace("\\", "/").lstrip("./")
        for p in baseline_error_paths_raw
        if isinstance(p, str) and p.strip()
    ]
    bug_data: Optional[DevActionInput] = None
    if isinstance(bug_data_raw, dict):
        try:
            bug_data = DevActionInput(**bug_data_raw)
        except Exception:
            bug_data = None
    workflow_id = payload.get("workflow_id")
    await log_dev_event(bug_id, "verify_fix", "🔍 Step 3/5: Verifying fix (dependencies + project check)...", "info", workflow_id)
    logging.info("[verify_fix] Starting verification in %s", repo_path)
    verify_mode = (os.getenv("DEV_AGENT_VERIFY_MODE") or "hybrid").strip().lower()
    if verify_mode not in ("edited_only", "hybrid", "strict"):
        verify_mode = "hybrid"
    enforce_completeness = (os.getenv("DEV_AGENT_VERIFY_ENFORCE_COMPLETENESS") or "").strip().lower() in ("1", "true", "yes")
    enforce_schema_refs = (os.getenv("DEV_AGENT_VERIFY_ENFORCE_SCHEMA_REFS") or "").strip().lower() in ("1", "true", "yes")
    enforce_db_migration = (os.getenv("DEV_AGENT_VERIFY_ENFORCE_DB_MIGRATION") or "1").strip().lower() in ("1", "true", "yes")
    await log_dev_event(
        bug_id,
        "verify_fix",
        (
            f"ℹ️ Verify mode: `{verify_mode}` "
            f"(enforce_completeness={'on' if enforce_completeness else 'off'}, "
            f"enforce_schema_refs={'on' if enforce_schema_refs else 'off'}, "
            f"enforce_db_migration={'on' if enforce_db_migration else 'off'})"
        ),
        "info",
        workflow_id,
    )
    logging.info(
        "[verify_fix] Active verify mode: %s | enforce_completeness=%s | enforce_schema_refs=%s | enforce_db_migration=%s",
        verify_mode,
        enforce_completeness,
        enforce_schema_refs,
        enforce_db_migration,
    )

    try:
        modified = _get_modified_files(repo_path)
        db_meta = _db_change_metadata(modified)
        await log_dev_event(
            bug_id,
            "verify_fix",
            (
                "🗃️ DB change metadata: "
                f"db_changes_detected={str(db_meta['db_changes_detected']).lower()} | "
                f"requires_db_confirmation={str(db_meta['requires_db_confirmation']).lower()} | "
                f"migration_files={','.join(db_meta['migration_files'][:10]) if db_meta['migration_files'] else '(none)'}"
            ),
            "info",
            workflow_id,
        )
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

        completeness_warnings = _functional_completeness_warnings(modified, bug_data)
        if completeness_warnings:
            warn_text = " | ".join(completeness_warnings)
            await log_dev_event(
                bug_id,
                "verify_fix",
                "⚠️ Functional completeness warnings: " + warn_text[:1800],
                "warning",
                workflow_id,
            )
            logging.warning("[verify_fix] Functional completeness warnings: %s", warn_text)
        completeness_failures = _functional_completeness_failures(modified, bug_data)
        contract = _build_implementation_contract(bug_data) if bug_data else None
        requires_migration = bool(contract and contract.get("requires_migration"))
        if enforce_db_migration and requires_migration and not db_meta["migration_files"]:
            missing_msg = (
                "DB migration enforcement failed: implementation contract requires DB/schema changes, "
                "but no migration file was detected in this bug branch context "
                "(working tree + untracked files + base-branch diff)."
            )
            await log_dev_event(
                bug_id,
                "verify_fix",
                "❌ " + missing_msg[:1800],
                "error",
                workflow_id,
            )
            return False, missing_msg
        if enforce_completeness and completeness_failures:
            fail_text = " | ".join(completeness_failures)
            await log_dev_event(
                bug_id,
                "verify_fix",
                "❌ Functional completeness failed: " + fail_text[:1800],
                "error",
                workflow_id,
            )
            return False, "Functional completeness failed:\n" + "\n".join(completeness_failures)

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
        check_timeout_sec = int(os.getenv("DEV_AGENT_CHECK_TIMEOUT_SEC", "240"))
        for check_cmd, name in (
            (["bun", "run", "check"], "bun run check"),
            (["npm", "run", "check"], "npm run check"),
        ):
            code, out, err = _run_cmd(check_cmd, repo_path, timeout_sec=check_timeout_sec)
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

        # Backward-compat strict toggle: fail on any check failure.
        if verify_mode == "strict" or os.getenv("DEV_AGENT_VERIFY_STRICT", "").strip() in ("1", "true", "yes"):
            error_msg = (last_out + "\n" + last_err).strip() or "Project check failed (syntax/type errors)."
            await log_dev_event(bug_id, "verify_fix", "❌ Verification failed:\n" + error_msg[:2000], "error", workflow_id)
            return False, error_msg

        modified = _get_modified_files(repo_path)
        full_output = (last_out + "\n" + last_err).strip()
        schema_findings = _detect_missing_schema_references(full_output)
        if schema_findings:
            await log_dev_event(
                bug_id,
                "verify_fix",
                "⚠️ Schema reference warnings (consider migration/schema update): "
                + ", ".join(schema_findings[:10]),
                "warning",
                workflow_id,
            )
            logging.warning("[verify_fix] Schema reference warnings: %s", schema_findings[:10])
            if enforce_schema_refs:
                schema_msg = "Schema reference checks failed:\n" + "\n".join(schema_findings[:20])
                await log_dev_event(
                    bug_id,
                    "verify_fix",
                    "❌ " + schema_msg[:1800],
                    "error",
                    workflow_id,
                )
                return False, schema_msg

        current_error_paths = _parse_check_error_paths(full_output, repo_path)
        baseline_error_set = [b.replace("\\", "/").lstrip("./") for b in baseline_error_paths]
        new_error_paths = [
            p for p in current_error_paths
            if not any(_path_matches_modified(p, b) for b in baseline_error_set)
        ]
        errors_in_modified = (
            [p for p in new_error_paths if any(_path_matches_modified(p, m) for m in modified)]
            if modified
            else []
        )
        logging.info(
            "[verify_fix] check failed summary: mode=%s modified=%d baseline_errors=%d current_errors=%d new_errors=%d new_errors_in_modified=%d",
            verify_mode,
            len(modified),
            len(baseline_error_set),
            len(current_error_paths),
            len(new_error_paths),
            len(errors_in_modified),
        )

        if verify_mode == "edited_only":
            if errors_in_modified:
                error_msg = (
                    "New errors in modified files:\n"
                    + "\n".join(errors_in_modified[:20])
                    + "\n\n"
                    + full_output[:1500]
                )
                await log_dev_event(
                    bug_id, "verify_fix",
                    "❌ Verification failed:\n" + error_msg[:2000],
                    "error",
                    workflow_id,
                )
                return False, error_msg

            await log_dev_event(
                bug_id, "verify_fix",
                (
                    "✅ Step 3/5 done. Edited-only mode: no new errors in modified files. "
                    f"Ignoring {len(new_error_paths)} new error path(s) outside edited files."
                ),
                "warning" if new_error_paths else "info",
                workflow_id,
            )
            logging.warning(
                "[verify_fix] Edited-only pass: outside_modified_new_errors=%d",
                len(new_error_paths),
            )
            return True, None

        if not new_error_paths:
            await log_dev_event(
                bug_id, "verify_fix",
                "✅ Step 3/5 done. Bypassing: no new Error diagnostics beyond baseline.",
                "info",
                workflow_id,
            )
            logging.info("[verify_fix] Bypass: no new errors over baseline.")
            return True, None

        # If there are no new errors in modified files, default to pass (repo has many pre-existing issues).
        # Optional strict mode can fail even when new errors are outside modified files.
        strict_outside_modified = (
            (os.getenv("DEV_AGENT_VERIFY_STRICT_OUTSIDE_MODIFIED") or "").strip().lower()
            in ("1", "true", "yes")
        )
        if not errors_in_modified and new_error_paths:
            if not strict_outside_modified:
                await log_dev_event(
                    bug_id,
                    "verify_fix",
                    (
                        "✅ Step 3/5 done. Bypassing: new diagnostics are outside modified files "
                        "(strict outside-modified check disabled)."
                    ),
                    "warning",
                    workflow_id,
                )
                logging.warning(
                    "[verify_fix] Bypass: %d new error path(s) outside modified files (strict disabled).",
                    len(new_error_paths),
                )
                return True, None

        # Fail on newly introduced errors. Prioritize modified-file errors in message.
        error_msg = full_output or "Project check failed (syntax/type errors)."
        if errors_in_modified:
            error_msg = "New errors in modified files:\n" + "\n".join(errors_in_modified[:20]) + "\n\n" + error_msg[:1500]
        else:
            error_msg = (
                "New project errors introduced outside modified files:\n"
                + "\n".join(new_error_paths[:20])
                + "\n\n"
                + error_msg[:1500]
            )
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
async def create_pull_request(config: Dict[str, Any]) -> Dict[str, Any]:
    """Commits/pushes branch, captures staging preview, and optionally raises a PR."""
    repo_path = config["repo_path"]
    branch_name = config["branch_name"]
    bug_data = DevActionInput(**config["bug_data"])
    workflow_id = config.get("workflow_id")
    
    github_token = os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN")
    
    repo_url = os.getenv("DEV_AGENT_GITHUB_REPO_URL", "https://github.com/singforhope/phwb.git")
    repo_name = repo_url.split("github.com/")[-1].replace(".git", "")
    await log_dev_event(
        bug_data.bug_id, "create_pull_request",
        "📦 Step 4/5: Committing, pushing branch, and preparing staging/PR handoff...",
        "info", workflow_id
    )
    modified_files = _get_modified_files(repo_path)
    migration_files = _detect_migration_files(modified_files)
    if migration_files:
        await log_workflow_state(
            bug_data.bug_id,
            "code_complete_db_pending",
            "DB migration changes detected: " + ", ".join(migration_files[:10]),
            workflow_id=workflow_id,
            post_comment=True,
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
        preview_url = await _deploy_and_capture_staging_preview(
            repo_path=repo_path,
            branch_name=branch_name,
            bug_id=bug_data.bug_id,
            workflow_id=workflow_id,
        )
        if preview_url:
            if migration_files:
                await log_workflow_state(
                    bug_data.bug_id,
                    "staging_ready_db_pending",
                    f"Staging preview is ready: {preview_url}. DB migration apply/confirmation is still required.",
                    workflow_id=workflow_id,
                    post_comment=True,
                )
            else:
                await log_workflow_state(
                    bug_data.bug_id,
                    "staging_ready",
                    f"Staging preview is ready: {preview_url}",
                    workflow_id=workflow_id,
                    post_comment=True,
                )
            await log_workflow_state(
                bug_data.bug_id,
                "ready_for_pr",
                "Branch pushed and staging preview captured; ready to create PR.",
                workflow_id=workflow_id,
                post_comment=False,
            )
        else:
            await log_workflow_state(
                bug_data.bug_id,
                "ready_for_pr",
                "Branch pushed and ready to create PR.",
                workflow_id=workflow_id,
                post_comment=False,
            )
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to commit/push: {e}")
        await log_dev_event(bug_data.bug_id, "create_pull_request", "❌ Failed to push branch: " + str(e), "error", workflow_id)
        raise

    pr_mode = (os.getenv("DEV_AGENT_PR_MODE") or "after_staging_approval").strip().lower()
    create_pr_now = pr_mode in ("parallel_draft", "parallel", "immediate")
    pr_url: Optional[str] = None
    pr_created = False
    pr_is_draft = (os.getenv("DEV_AGENT_PR_DRAFT_ON_PARALLEL") or "1").strip().lower() in ("1", "true", "yes")

    if preview_url and not create_pr_now:
        await log_dev_event(
            bug_data.bug_id,
            "create_pull_request",
            "⏸️ Staging-first mode active: PR creation deferred until staging validation/approval.",
            "info",
            workflow_id,
        )
    else:
        # Raise PR using PyGithub (lazy import: only in activity, not in workflow sandbox)
        try:
            from github import Github
        except ImportError:
            raise RuntimeError("PyGithub not installed. Run: pip install PyGithub")
        g = Github(github_token)
        repo = g.get_repo(repo_name)

        await log_dev_event(
            bug_data.bug_id,
            "create_pull_request",
            "🔗 Creating pull request on GitHub...",
            "info",
            workflow_id,
        )
        pr = repo.create_pull(
            title=f"Fix: {bug_data.title} (Bug #{bug_data.bug_id})",
            body=f"Automated PR generated by DevAgent to fix bug #{bug_data.bug_id}.\n\n{bug_data.description}",
            head=branch_name,
            base="main",
            draft=bool(preview_url and pr_is_draft),
        )
        pr_url = pr.html_url
        pr_created = True
        await log_dev_event(
            bug_data.bug_id,
            "create_pull_request",
            "✅ Step 4/5 done. PR: " + pr_url,
            "success",
            workflow_id,
        )
        logging.info("[create_pull_request] PR created: %s", pr_url)

    return {
        "pr_url": pr_url,
        "staging_url": preview_url,
        "pr_created": pr_created,
        "pr_mode": pr_mode,
        "branch_name": branch_name,
    }


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


def _classify_blocking_layers(error_msg: str) -> List[str]:
    msg = (error_msg or "").lower()
    layers: List[str] = []

    ui_signals = (
        ".svelte",
        "ui",
        "frontend",
        "render",
        "component",
        "page",
        "svelte",
        "css",
    )
    api_signals = (
        "api",
        "+server.ts",
        "endpoint",
        "request",
        "response",
        "405",
        "401",
        "403",
        "404",
        "406",
        "500",
        "store",
        "service",
    )
    db_signals = (
        "supabase",
        "pgrst",
        "schema cache",
        "relation",
        "foreign key",
        "database",
        "migration",
        "table",
        "column",
        "sql",
    )
    deploy_staging_signals = (
        "staging",
        "preview",
        "deploy",
        "vercel",
        "build failed",
        "could not start preview",
        "e2e",
    )

    if any(s in msg for s in ui_signals):
        layers.append("UI")
    if any(s in msg for s in api_signals):
        layers.append("API")
    if any(s in msg for s in db_signals):
        layers.append("DB")
    if any(s in msg for s in deploy_staging_signals):
        layers.append("deploy/staging")

    if not layers:
        layers.append("API")
    return layers


def _extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"https?://[^\s)>\"]+", text)
    if match:
        return match.group(0)
    return None


def _sanitize_branch_for_subdomain(branch_name: str) -> str:
    value = (branch_name or "").strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "preview"


def _normalize_branch_for_match(branch_name: str) -> str:
    return (branch_name or "").strip().lower()


def _extract_vercel_preview_url_for_branch(
    *,
    branch_name: str,
    project_id: str,
    token: str,
    team_id: Optional[str] = None,
    lookback_minutes: int = 240,
) -> Optional[str]:
    """
    Query Vercel deployments and return latest READY preview URL for branch.
    """
    if not branch_name or not project_id or not token:
        return None

    query: Dict[str, str] = {
        "projectId": project_id,
        "state": "READY",
        "target": "preview",
        "limit": "50",
    }
    if team_id:
        query["teamId"] = team_id
    url = "https://api.vercel.com/v6/deployments?" + parse.urlencode(query)
    req = request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(raw or "{}")
    deployments = payload.get("deployments") if isinstance(payload, dict) else None
    if not isinstance(deployments, list):
        return None

    now_ms = int(time.time() * 1000)
    max_age_ms = max(5, lookback_minutes) * 60 * 1000
    target_branch = _normalize_branch_for_match(branch_name)
    target_slug = _normalize_branch_for_match(_sanitize_branch_for_subdomain(branch_name))
    selected: Optional[Dict[str, Any]] = None

    for dep in deployments:
        if not isinstance(dep, dict):
            continue
        created_at = dep.get("createdAt")
        if isinstance(created_at, (int, float)):
            if (now_ms - int(created_at)) > max_age_ms:
                continue

        meta = dep.get("meta") if isinstance(dep.get("meta"), dict) else {}
        branch_candidates = [
            meta.get("githubCommitRef"),
            meta.get("githubCommitBranch"),
            meta.get("gitlabCommitRef"),
            meta.get("bitbucketCommitRef"),
            meta.get("gitCommitRef"),
            meta.get("branch"),
            meta.get("sourceBranch"),
        ]
        branch_candidates = [
            _normalize_branch_for_match(str(x))
            for x in branch_candidates
            if isinstance(x, str) and x.strip()
        ]
        if target_branch not in branch_candidates and target_slug not in branch_candidates:
            continue

        state = str(dep.get("state") or "").upper()
        if state != "READY":
            continue

        if selected is None:
            selected = dep
            continue
        prev_created = int(selected.get("createdAt") or 0)
        cur_created = int(dep.get("createdAt") or 0)
        if cur_created > prev_created:
            selected = dep

    if not selected:
        return None
    dep_url = selected.get("url")
    if isinstance(dep_url, str) and dep_url.strip():
        resolved = dep_url.strip()
        if not resolved.startswith("http://") and not resolved.startswith("https://"):
            resolved = "https://" + resolved.lstrip("/")
        return resolved
    return None


async def _deploy_and_capture_staging_preview(
    *,
    repo_path: str,
    branch_name: str,
    bug_id: int,
    workflow_id: Optional[str],
) -> Optional[str]:
    """
    Trigger staging/preview deployment and return URL when possible.
    Supports:
    - DEV_AGENT_STAGING_DEPLOY_COMMAND (optional shell command)
    - DEV_AGENT_STAGING_URL_TEMPLATE (optional URL format with {branch} placeholder)
    """
    deploy_cmd = (os.getenv("DEV_AGENT_STAGING_DEPLOY_COMMAND") or "").strip()
    url_template = (os.getenv("DEV_AGENT_STAGING_URL_TEMPLATE") or "").strip()
    branch_slug = _sanitize_branch_for_subdomain(branch_name)

    vercel_enabled = (os.getenv("DEV_AGENT_VERCEL_PREVIEW_ENABLED") or "").strip().lower() in ("1", "true", "yes")
    vercel_token = (os.getenv("DEV_AGENT_VERCEL_TOKEN") or os.getenv("VERCEL_TOKEN") or "").strip()
    vercel_project_id = (os.getenv("DEV_AGENT_VERCEL_PROJECT_ID") or "").strip()
    vercel_team_id = (os.getenv("DEV_AGENT_VERCEL_TEAM_ID") or "").strip() or None
    vercel_lookback_minutes = int((os.getenv("DEV_AGENT_VERCEL_LOOKBACK_MINUTES") or "240").strip() or "240")
    can_use_vercel_lookup = bool(vercel_enabled and vercel_token and vercel_project_id)

    if not deploy_cmd and not url_template and not can_use_vercel_lookup:
        await log_dev_event(
            bug_id,
            "staging_deploy",
            (
                "ℹ️ Staging deploy skipped (no deploy command/url template configured, "
                "and Vercel preview lookup is disabled or missing configuration)."
            ),
            "info",
            workflow_id,
        )
        return None

    await log_dev_event(
        bug_id,
        "staging_deploy",
        "🚀 Triggering staging/preview deployment...",
        "info",
        workflow_id,
    )

    resolved_url: Optional[str] = None
    if url_template:
        try:
            resolved_url = url_template.format(branch=branch_slug, branch_name=branch_name)
        except Exception:
            resolved_url = url_template

    if deploy_cmd:
        command = (
            deploy_cmd.replace("{repo_path}", repo_path)
            .replace("{branch}", branch_name)
            .replace("{branch_slug}", branch_slug)
        )
        try:
            proc = subprocess.run(
                command,
                cwd=repo_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            discovered = _extract_first_url(output)
            if discovered:
                resolved_url = discovered
            if proc.returncode != 0:
                await log_dev_event(
                    bug_id,
                    "staging_deploy",
                    "⚠️ Staging deploy command failed; continuing with PR flow.\n" + output[:1200],
                    "warning",
                    workflow_id,
                )
            else:
                await log_dev_event(
                    bug_id,
                    "staging_deploy",
                    "✅ Staging deploy command completed.",
                    "success",
                    workflow_id,
                )
        except Exception as e:
            await log_dev_event(
                bug_id,
                "staging_deploy",
                "⚠️ Staging deploy command error; continuing with PR flow. " + str(e),
                "warning",
                workflow_id,
            )

    # Optional provider-level fallback: discover preview URL from Vercel deployments by branch.
    if not resolved_url and can_use_vercel_lookup:
        try:
            resolved_url = _extract_vercel_preview_url_for_branch(
                branch_name=branch_name,
                project_id=vercel_project_id,
                token=vercel_token,
                team_id=vercel_team_id,
                lookback_minutes=vercel_lookback_minutes,
            )
            if resolved_url:
                await log_dev_event(
                    bug_id,
                    "staging_deploy",
                    "✅ Vercel preview resolved for branch: " + resolved_url,
                    "success",
                    workflow_id,
                )
            else:
                await log_dev_event(
                    bug_id,
                    "staging_deploy",
                    "ℹ️ Vercel preview lookup found no READY deployment for this branch yet.",
                    "info",
                    workflow_id,
                )
        except Exception as e:
            await log_dev_event(
                bug_id,
                "staging_deploy",
                "⚠️ Vercel preview lookup failed; continuing with PR flow. " + str(e),
                "warning",
                workflow_id,
            )

    if resolved_url:
        await log_dev_event(
            bug_id,
            "staging_deploy",
            "🔗 Staging preview URL: " + resolved_url,
            "info",
            workflow_id,
        )
    return resolved_url


def _latest_workflow_state_from_log_rows(log_rows: List[Dict[str, Any]]) -> Optional[str]:
    for row in log_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("step") or "") != "workflow_state":
            continue
        msg = str(row.get("message") or "")
        m = re.search(r"`([^`]+)`", msg)
        if m:
            return m.group(1).strip()
    return None


def _build_structured_run_summary(
    *,
    log_rows: List[Dict[str, Any]],
    pr_url: Optional[str],
    staging_url: Optional[str],
    latest_state: Optional[str],
) -> str:
    layers: List[str] = []
    verify_mode = "unknown"
    verify_result = "unknown"
    migration_files: List[str] = []
    migration_apply_status = "unknown"
    effective_staging_url = staging_url

    # Parse most-recent-first rows.
    for row in log_rows:
        if not isinstance(row, dict):
            continue
        step = str(row.get("step") or "")
        msg = str(row.get("message") or "")
        if step == "staging_deploy" and not effective_staging_url:
            found_url = _extract_first_url(msg)
            if found_url:
                effective_staging_url = found_url

        if step == "analyze_and_code" and "Implementation contract:" in msg and not layers:
            m = re.search(r"layers=([^|]+)", msg)
            if m and m.group(1).strip():
                layers = [x.strip() for x in m.group(1).split(",") if x.strip()]

        if step == "verify_fix":
            if verify_mode == "unknown":
                m = re.search(r"Verify mode:\s*`([^`]+)`", msg)
                if m:
                    verify_mode = m.group(1).strip()
            if verify_result == "unknown":
                lower = msg.lower()
                if "project check passed" in lower or "e2e smoke passed" in lower:
                    verify_result = "passed"
                elif "verification failed" in lower or msg.startswith("❌"):
                    verify_result = "failed"
            if not migration_files and "DB change metadata:" in msg and "migration_files=" in msg:
                tail = msg.split("migration_files=", 1)[1].strip()
                if tail != "(none)":
                    migration_files = [x.strip() for x in tail.split(",") if x.strip()]

        if step == "db_apply" and migration_apply_status == "unknown":
            lower = msg.lower()
            if "db apply completed. applied=" in lower and "failed=0" in lower:
                migration_apply_status = "applied_success"
            elif "db apply completed with failures" in lower:
                migration_apply_status = "applied_with_failures"
            elif "nothing to apply" in lower:
                migration_apply_status = "not_required"

    if latest_state in ("code_complete_db_pending", "staging_ready_db_pending"):
        migration_apply_status = "pending_apply"

    validation_result = "not_started"
    if latest_state == "staging_validated":
        validation_result = "approved"
    elif latest_state == "staging_rejected":
        validation_result = "rejected"
    elif latest_state in ("staging_ready", "staging_ready_db_pending"):
        validation_result = "awaiting_validation"

    return (
        "### Dev Agent run summary\n\n"
        f"- Layers changed: {', '.join(layers) if layers else 'unknown'}\n"
        f"- Verify summary: mode=`{verify_mode}`, result={verify_result}\n"
        f"- Migration files: {', '.join(migration_files) if migration_files else '(none)'}\n"
        f"- Migration apply status: {migration_apply_status}\n"
        f"- Staging link: {effective_staging_url or '(not available)'}\n"
        f"- User validation result: {validation_result}\n"
        f"- PR: {pr_url or '(not created yet)'}"
    )


@activity.defn
async def update_bug_ticket(config: Dict[str, Any]) -> None:
    """Updates Supabase bug ticket with staging/PR handoff or error."""
    from app.supabase_client import get_supabase_client
    supabase = get_supabase_client("DEV_AGENT")
    
    bug_id = config["bug_id"]
    pr_url = config.get("pr_url")
    staging_url = config.get("staging_url")
    error_msg = config.get("error_msg")
    workflow_id = config.get("workflow_id")
    
    if not supabase:
        logging.warning("[update_bug_ticket] Supabase not configured. Skipping.")
        return

    if pr_url or staging_url:
        await log_dev_event(
            bug_id,
            "update_bug_ticket",
            "📝 Step 5/5: Posting staging/PR handoff details to comments...",
            "info",
            workflow_id,
        )
        if staging_url and pr_url:
            handoff_content = (
                "✅ **Dev Agent** completed code changes.\n\n"
                f"**Staging preview:** {staging_url}\n\n"
                f"**Pull Request:** {pr_url}"
            )
        elif staging_url:
            handoff_content = (
                "✅ **Dev Agent** completed code changes and prepared a staging preview.\n\n"
                f"**Staging preview:** {staging_url}\n\n"
                "Please validate on staging. PR creation is deferred until staging approval."
            )
        else:
            handoff_content = f"✅ **Dev Agent** completed a fix and raised a Pull Request.\n\nPlease review it here: {pr_url}"

        supabase.table("phwb_bug_comments").insert({
            "bug_id": bug_id,
            "user_id": None,
            "content": handoff_content,
            "is_internal": False,
        }).execute()
        if pr_url:
            supabase.table("phwb_bugs").update({"status": "review"}).eq("id", bug_id).execute()
        db_pending = False
        staging_validated = False
        latest_state: Optional[str] = None
        summary_rows: List[Dict[str, Any]] = []
        try:
            q = (
                supabase.from_("phwb_dev_logs")
                .select("step,message,level,created_at")
                .eq("bug_id", bug_id)
            )
            if workflow_id:
                q = q.eq("workflow_id", workflow_id)
            logs_res = q.order("created_at", desc=True).limit(300).execute()
            log_rows = getattr(logs_res, "data", None) or []
            summary_rows = [r for r in log_rows if isinstance(r, dict)]
            latest_state = _latest_workflow_state_from_log_rows(summary_rows)

            db_pending = latest_state in ("code_complete_db_pending", "staging_ready_db_pending")
            staging_validated = latest_state == "staging_validated"
        except Exception as e:
            logging.warning("[update_bug_ticket] Could not determine db_pending state: %s", e)

        if db_pending:
            await log_workflow_state(
                bug_id,
                "code_complete_db_pending",
                (
                    "Handoff posted. Waiting for DB migration confirmation/apply before fully_complete."
                    if not pr_url
                    else "PR created and bug moved to Review. Waiting for DB migration confirmation/apply before fully_complete."
                ),
                workflow_id=workflow_id,
                post_comment=False,
            )
        elif pr_url and staging_validated:
            await log_workflow_state(
                bug_id,
                "fully_complete",
                "PR created, staging validated, and DB requirements satisfied.",
                workflow_id=workflow_id,
                post_comment=False,
            )
        elif pr_url:
            await log_workflow_state(
                bug_id,
                "ready_for_pr",
                "PR created and bug moved to Review. Waiting for staging validation before fully_complete.",
                workflow_id=workflow_id,
                post_comment=False,
            )
        elif staging_url:
            await log_workflow_state(
                bug_id,
                "staging_ready",
                f"Staging preview is ready for validation: {staging_url}",
                workflow_id=workflow_id,
                post_comment=False,
            )

        await log_dev_event(
            bug_id,
            "update_bug_ticket",
            (
                "🎉 Step 5/5 done. Staging handoff posted."
                if staging_url and not pr_url
                else "🎉 Step 5/5 done. Handoff posted."
            ),
            "success",
            workflow_id,
        )
        # G1: Emit a structured final summary in both logs and comments.
        summary_text = _build_structured_run_summary(
            log_rows=summary_rows,
            pr_url=pr_url,
            staging_url=staging_url,
            latest_state=latest_state,
        )
        await log_dev_event(
            bug_id,
            "run_summary",
            summary_text,
            "info",
            workflow_id,
        )
        try:
            supabase.table("phwb_bug_comments").insert({
                "bug_id": bug_id,
                "user_id": None,
                "content": summary_text,
                "is_internal": False,
            }).execute()
        except Exception as e:
            logging.warning("[update_bug_ticket] Could not post run summary comment: %s", e)
        logging.info("[update_bug_ticket] Comment + status updated for bug #%s", bug_id)

    elif error_msg:
        suggested_fix = _suggest_fix_for_error(error_msg)
        blocking_layers = _classify_blocking_layers(error_msg)
        await log_dev_event(bug_id, "update_bug_ticket", "📝 Posting error to Comments (internal)...", "info", workflow_id)
        supabase.table("phwb_bug_comments").insert({
            "bug_id": bug_id,
            "user_id": None,
            "content": (
                "❌ **Dev Agent** could not complete this fix after retry attempts.\n\n"
                "**Blocking layer(s):**\n"
                f"- {', '.join(blocking_layers)}\n\n"
                "**Error:**\n"
                f"```\n{error_msg}\n```\n\n"
                "**Suggested fix:**\n"
                f"{suggested_fix}"
            ),
            "is_internal": True,
        }).execute()
        await log_dev_event(
            bug_id,
            "failure_classification",
            "Blocking layers: " + ", ".join(blocking_layers),
            "warning",
            workflow_id,
        )
        await log_dev_event(
            bug_id,
            "update_bug_ticket",
            "❌ Error + suggested fix posted to Comments: " + error_msg[:200],
            "error",
            workflow_id,
        )
        logging.info("[update_bug_ticket] Error comment posted for bug #%s", bug_id)
    else:
        # Defensive fallback: prevents silent no-op when handoff payload is missing.
        await log_dev_event(
            bug_id,
            "update_bug_ticket",
            "⚠️ No PR URL or staging URL was provided to update_bug_ticket; nothing was posted.",
            "warning",
            workflow_id,
        )
        logging.warning(
            "[update_bug_ticket] No handoff payload (pr_url/staging_url/error_msg) for bug #%s",
            bug_id,
        )
