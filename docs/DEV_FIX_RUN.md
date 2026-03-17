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
