# Running the Dev Fix Backend (voiceaiagentv5)

The **Initiate dev fix** flow in PHWB needs three processes from this repo:

1. **Temporal** – workflow queue
2. **FastAPI server** – HTTP API (`POST /api/dev/fix`)
3. **Temporal worker** – runs the DevFixWorkflow (clone, code, verify, PR, update bug)

## Option A: One script (recommended)

Install Temporal CLI, then from voiceaiagentv5 root: `./scripts/run_dev_fix.sh`. Press Ctrl+C to stop.

## Option B: Three terminals (manual)

**Terminal 1 – Temporal (CLI, no Docker/Cassandra)**

```bash
brew install temporal   # once
temporal server start-dev
```

Leave this running. Web UI: http://localhost:8233.

**Terminal 2 – FastAPI (port 8000)**

From the **voiceaiagentv5** project root (with venv activated):

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 3 – Temporal worker**

From the **voiceaiagentv5** project root (same venv):

```bash
python app/temporal_worker.py
```

Leave all three running. Then in PHWB, set `PUBLIC_VOICE_AGENT_URL=http://localhost:8000` (or leave default in dev) and use **Initiate dev fix** on a bug.

## Option C: Run script (after installing Temporal CLI)

From the **voiceaiagentv5** project root, install the **Temporal CLI** and ensure a **virtualenv** with deps (script uses `.venv` or `venv`):

```bash
# One-time: Temporal CLI (no Docker/Cassandra)
brew install temporal

# One-time: venv and deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

chmod +x scripts/run_dev_fix.sh
./scripts/run_dev_fix.sh
```

The script starts the Temporal dev server (CLI), then the API and worker. Press **Ctrl+C** to stop all. If you see `ModuleNotFoundError: No module named 'fastapi'`, run `pip install -r requirements.txt` in the venv.

## Production vs test mode

**Production:** PHWB sends only bug data (`id`, `title`, `description`, `category`, `status`). The workflow runs the full agent (analyze → code → verify → PR). Do **not** send `test_mode` for real issues.

**Optional test mode:** When `test_mode: true` is sent (e.g. from phwb-testrepo’s `npm run test-repo`), the workflow skips the LLM and applies a trivial change, then verify → PR. Use only for validating the pipeline; see “Test repo flow” below.

## Playwright E2E (optional)

When **verify_fix** passes the project check, the harness can optionally run Playwright smoke E2E **only when** the agent’s changes are **only UI** (every modified file is a UI path: `.svelte`, `src/routes/`, `src/lib/components/`, `src/app.css`, or `static/`).

- **`DEV_AGENT_RUN_E2E=1`** (or `true`/`yes`) – Enable conditional E2E. If changes are only UI, the harness runs build → preview → `npx playwright test --project=smoke` and fails verify on E2E failure.
- **`DEV_AGENT_E2E_ALWAYS=1`** – (Optional) Run E2E even when changes are not only UI (e.g. for full QA runs).

When E2E is enabled but changes are not only UI, the log shows: *“Skipping E2E (changes are not only UI)”* and verify still passes. See `docs/PLAYWRIGHT_UI_ONLY_QA_PLAN.md` in phwb-testrepo for the full flow and decision table.

## Env and prerequisites

- **.env** in voiceaiagentv5 should have at least:
  - `ANTHROPIC_API_KEY` (for the dev agent; the dev fix flow uses Claude)
  - For Dev Fix: `DEV_AGENT_GITHUB_ACCESS_TOKEN`, `DEV_AGENT_GITHUB_REPO_URL` (optional, default phwb repo), and Supabase for the PHWB DB: `SUPABASE_URL` + key (e.g. for `update_bug_ticket` and `phwb_dev_logs`). See `app/activities/dev_activities.py` and `app/supabase_client.py`.
- **Python**: 3.9+ with `pip install -r requirements.txt`.
- **Temporal CLI**: `brew install temporal` for local dev (recommended). Or use [Temporal Cloud](https://temporal.io/cloud) and set `TEMPORAL_URL` in `.env`.

### Optional model routing (coding quality tuning)

You can tune model selection per node in the dev agent pipeline:

- `DEV_AGENT_MODEL_DEFAULT` – model used by coder/reviewer by default.
- `DEV_AGENT_MODEL_PLANNER` – model used by planner node.
- `DEV_AGENT_MODEL_ESCALATED` – stronger model used for coder/reviewer when retry context is detected.
- `DEV_AGENT_MODEL_ESCALATE_ON_RETRY` – `1/true/yes` (default) enables auto-escalation on retry contexts.
- `DEV_AGENT_ANALYZE_TIMEOUT_SEC` – max seconds for each `analyze_and_code` attempt before auto-fail/retry (default `1200`).

If not set, all nodes use the current built-in default model.

### Verify mode (stability vs strictness)

Verification behavior is controlled by `DEV_AGENT_VERIFY_MODE`:

- `edited_only` - default for noisy repos; fail only when new errors are detected in files modified by the current run.
- `hybrid` - baseline-aware mode; compares current errors against preflight baseline and allows unrelated/baseline errors.
- `strict` - fail on any check failure.

Scope/category hints (UI/Backend/etc.) are advisory only. They are logged as non-blocking scope observations for reviewer context and do not directly fail `verify_fix`.

Related toggles:

- `DEV_AGENT_VERIFY_STRICT=1` - backward-compatible strict override (equivalent to strict fail behavior).
- `DEV_AGENT_CHECK_TIMEOUT_SEC` - timeout for project check command in verify (default `240`).
- `DEV_AGENT_VERIFY_STRICT_OUTSIDE_MODIFIED=1` - in hybrid mode, fail when new errors are outside modified files.
- `DEV_AGENT_VERIFY_ENFORCE_COMPLETENESS=1` - promote critical completeness checks (missing required UI/API/DB-migration layers) to blocking failures.
- `DEV_AGENT_VERIFY_ENFORCE_SCHEMA_REFS=1` - fail verify when output shows missing schema objects (missing table/relation/schema cache).
- `DEV_AGENT_VERIFY_ENFORCE_DB_MIGRATION=1` - when contract requires DB/schema changes, fail verify unless a migration file is detected from bug-branch context (working tree + untracked + base-branch diff).

Runtime logs now include active verify mode at the start of each `verify_fix` run.
Runtime logs may also include non-blocking scope observations when ticket hints and changed files diverge.

### Schema audit before coding

`analyze_and_code` now performs a best-effort DB schema audit before prompting the coder:

- infers candidate table(s) from bug context (e.g., Events -> `phwb_events`)
- scans snake_case field candidates in the ticket text
- probes table/column existence via Supabase and logs findings

Audit output is written to `phwb_dev_logs` and injected into the coding prompt as a "DB schema audit" section so the agent can decide migration requirements earlier.

### DB migration preview/apply APIs

The backend provides migration APIs for confirmation flow:

- `POST /api/dev/fix/migrations/preview` - returns detected migration files, SQL summaries, and risk tags.
- `POST /api/dev/fix/migrations/apply` - applies migration SQL (or dry-run) using a Supabase RPC function.
  - Request supports `confirm_destructive` for destructive SQL operations.

Migration apply is guarded by env vars:

- `DEV_AGENT_DB_AUTO_APPLY_ENABLED` - must be `1/true/yes` to allow apply endpoint.
- `DEV_AGENT_DB_APPLY_REQUIRES_CONFIRMATION` - when enabled, request must include `confirm: true`.
- `DEV_AGENT_DB_APPLY_TARGET` - target environment label (e.g. `dev`, `staging`, `prod`).
- `DEV_AGENT_DB_APPLY_TOKEN` - optional shared token expected in `x-dev-agent-db-token` header.
- `DEV_AGENT_DB_APPLY_RPC_NAME` / `DEV_AGENT_DB_APPLY_RPC_ARG` - Supabase RPC function/arg used to execute SQL (defaults: `exec_sql` / `sql`).

Safety behavior:

- Destructive SQL (`drop table/column/constraint`, `truncate`) is never applied unless the request explicitly includes:
  - `confirm: true`
  - `confirm_destructive: true`
- If apply fails, response now includes `rollback_guidance` and the backend logs/posts rollback guidance for operators.

### Staging preview integration (Phase D1)

After verify succeeds and the branch is pushed, the workflow can trigger a staging/preview deployment and capture a preview URL.

- `DEV_AGENT_STAGING_DEPLOY_COMMAND` - optional shell command run from cloned repo after push.
  - Placeholders: `{repo_path}`, `{branch}`, `{branch_slug}`
- `DEV_AGENT_STAGING_URL_TEMPLATE` - optional URL template for predictable preview hosts.
  - Placeholders: `{branch}` and `{branch_name}`
  - Example: `https://{branch}.phwb.singforhope.org`
- `DEV_AGENT_VERCEL_PREVIEW_ENABLED=1` - enable Vercel API-based preview lookup by branch.
- `DEV_AGENT_VERCEL_TOKEN` - Vercel API token used for deployment lookup.
- `DEV_AGENT_VERCEL_PROJECT_ID` - Vercel project ID for the PHWB frontend.
- `DEV_AGENT_VERCEL_TEAM_ID` - optional Vercel team ID.
- `DEV_AGENT_VERCEL_LOOKBACK_MINUTES` - lookup window for READY preview deployments (default `240`).
- `DEV_AGENT_VERCEL_POLL_TIMEOUT_SEC` - max wait for READY preview before fallback/fail (default `300`).
- `DEV_AGENT_VERCEL_POLL_INTERVAL_SEC` - polling interval for Vercel READY checks (default `10`).
- `DEV_AGENT_STAGING_REQUIRE_READY_BEFORE_HANDOFF=1` - require READY staging URL before posting final handoff comment.

Behavior:

- If either value is configured, the workflow logs/publishes staging state:
  - `staging_ready` (no DB pending)
  - `staging_ready_db_pending` (migration still pending)
- If URL template/command is not set, but Vercel lookup is enabled and configured, the workflow attempts to resolve preview URL from Vercel deployments for the pushed branch.
- With polling enabled, workflow waits for Vercel preview to become READY before handoff (up to configured timeout).
- Preview URL is written to `phwb_dev_logs` and bug comments as workflow-state messages.
- If not configured, flow continues with existing `ready_for_pr` behavior.

### Staging-first primary handoff (Phase D2)

The workflow supports staging-first delivery:

- `DEV_AGENT_PR_MODE=after_staging_approval` (default):
  - when staging URL is available, the workflow posts staging handoff and defers PR creation.
- `DEV_AGENT_PR_MODE=parallel_draft`:
  - creates PR immediately in parallel; PR is created as draft by default when staging URL exists.
- `DEV_AGENT_PR_DRAFT_ON_PARALLEL=1`:
  - keeps parallel PR as draft (set to `0` to create normal PR).

Output behavior:

- Workflow result and bug comments now prioritize staging URL when present.
- `fully_complete` is only set when PR exists and DB requirements are satisfied.

### QA policy alignment (Phase F1)

Recommended QA operating model:

- **Edited-file fast gate**: keep `verify_fix` as the quick stability gate (`edited_only` by default in noisy repos).
- **Outcome-based final QA**: validate the requested behavior on the staging preview for the affected workflow.
- **DB-aware QA**: treat DB-pending states as not QA-passable until migration status is confirmed/applied.

In short: file-scoped verify for speed, staging outcome validation for release confidence.

## Test repo flow (branch + PR, no LLM)

To run the full pipeline up to **creating a branch and PR** without the LLM (quick, ~1–2 min):

1. **From this repo (voiceaiagentv5):** Start Temporal + API + worker: `./scripts/run_dev_fix.sh`
2. **From phwb-testrepo** (the other repo that has the test script): `cd /path/to/phwb-testrepo` then `npm run test-repo` (or `TEST_MODE=1 npm run test-dev-fix`).  
   Do **not** run `npm run test-repo` from voiceaiagentv5 — that script lives in phwb-testrepo.

This creates a test bug, starts the workflow with **test_mode: true**, which: clones the repo, applies a trivial change (e.g. appends a comment to README), runs verify, then pushes the branch and creates a PR. Check the bug’s **Comments** tab for the PR link.

## Test agent coding (no Temporal)

To verify the dev agent can actually write code before running the full workflow:

From **voiceaiagentv5** root (venv activated):

```bash
python scripts/test_agent_coding.py
```

This clones the repo to a temp dir, runs the Planner → Coder → Reviewer with a simple instruction (“add a line to README.md”), then prints `git status` and `git diff`. If you see file changes, the agent’s coding capability is working. Requires: `ANTHROPIC_API_KEY`, `DEV_AGENT_GITHUB_ACCESS_TOKEN`, `DEV_AGENT_GITHUB_REPO_URL` (optional), and **node/npx** (for the MCP filesystem server).

## Quick check

- Health: [http://localhost:8000/health](http://localhost:8000/health)
- API docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Trigger from PHWB: Bugs → open a bug → **Initiate dev fix**, or use the simulation page at `/bugs/dev-fix-simulation`.
