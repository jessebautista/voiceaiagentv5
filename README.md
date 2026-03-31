# AI Agent Service

A basic AI agent with a **receptionist** persona: FAQ search, booking, and calculator tools, plus a simple web chat interface.

## Prerequisites (platforms & accounts)

Set these up before running the project so your team can run it from scratch.

| Platform / tool | Required? | What for | How to get it |
|----------------|-----------|----------|----------------|
| **Python 3.9+** | Yes | Runtime | [python.org](https://www.python.org/downloads/) — use 3.9, 3.10, or 3.11. Check with `python --version`. |
| **OpenAI API key** | Yes | LLM (chat model) | Sign up at [platform.openai.com](https://platform.openai.com/signup), then create an API key under [API keys](https://platform.openai.com/api-keys). Add it as `OPENAI_API_KEY` in `.env`. Billing must be enabled. |
| **Upstash Redis** | No | Persistent chat memory | Optional. Create a Redis DB at [Upstash](https://console.upstash.com/), then copy the REST URL and token into `.env` as `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. |
| **Redis (standard)** | No | Alternative to Upstash | Optional. Use [redis.io](https://redis.io/docs/install/) or [Redis Cloud](https://redis.com/try-free/). Set `REDIS_URL` in `.env`. |
| **Pinecone** | No | Vector FAQ search (RAG) | Optional; only if you add RAG over PDFs. Sign up at [pinecone.io](https://www.pinecone.io/), create an index, and set `PINECONE_API_KEY` and `PINECONE_INDEX` in `.env`. |
| **ElevenLabs** | No | Voice (Live Connect) | Optional. Get an API key at [ElevenLabs](https://elevenlabs.io/app/settings/api-keys). Set `ELEVENLABS_API_KEY` in `.env`. See [Voice (Live Connect)](#voice-live-connect) below. |
| **Supabase** | No | CRUD (news, piano applications, etc.) | Optional. Create a project at [supabase.com](https://supabase.com), then set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env`. Table configs live in `data/tables/*.json`. See [Supabase CRUD](#supabase-crud) below. |

**Minimum to run:** Python 3.9+ and a valid **OpenAI API key**. Redis, Pinecone, ElevenLabs, and Supabase are optional.

## Structure

```
<project-root>/
├── app/
│   ├── main.py              # FastAPI server, routes (/chat, /voice, /workspace, etc.)
│   ├── agent.py             # Orchestrator: LLM + tools (LangGraph ReAct agent, workspace‑driven config)
│   ├── agents/              # Personas (receptionist, SME expert)
│   ├── tools/               # Skills: search_faq, booking, calculator, datetime, reference, Supabase query builder/processor
│   ├── workspace_config.py  # Read/write workspace config (prompt_key, per-agent overrides, enabled tools, welcome)
│   ├── supabase_client.py   # Supabase client for CRUD (optional; requires SUPABASE_* in .env)
│   ├── tables_config.py    # Loads data/tables/*.json for LLM and query builder
│   ├── memory/              # Optional Redis chat history
│   └── prompts/             # Base prompts: system_receptionist.txt, supabase_receptionist.txt
├── data/
│   ├── reference.json       # Reference data for get_reference_info tool
│   ├── workspace_config.json# Workspace settings (prompt_key, enabled tools, welcome; edited via /workspace UI)
│   ├── prompt_overrides/    # Per-agent prompt override: receptionist.txt, supabase_receptionist.txt (optional)
│   └── tables/              # Per-table config for Supabase: news.json, piano_applications.json, piano_activations.json, etc.
├── scripts/
│   └── seed_upstash.py      # Seed Upstash with sample FAQ and chat data
├── static/
│   ├── index.html           # Chat UI
│   ├── voice.html           # Live Connect voice UI + trace panel
│   ├── settings.html        # Local voice/silence settings
│   └── workspace.html       # Workspace to edit prompt, tools, welcome message
├── tests/
├── .env                     # Secrets (copy from .env.example)
├── requirements.txt
└── README.md
```

## Setup

Ensure you have the [prerequisites](#prerequisites-platforms--accounts) (Python 3.9+ and an OpenAI API key).

1. **Create virtualenv and install**
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env and set OPENAI_API_KEY
   ```

3. **Run the server**
   ```bash
   python -m app.main
   # or: uvicorn app.main:app --reload --port 8000
   ```

4. **Open the web UI**  
   Visit [http://localhost:8000](http://localhost:8000) to chat.  
   API docs: [http://localhost:8000/docs](http://localhost:8000/docs).

## Dev Fix Run (PHWB Integration)

If you are running the PHWB dev-fix flow end-to-end, use this launcher:

```bash
cd "/Users/banik/Desktop/Projects2026/voiceaiagentv5"
./scripts/run_dev_fix.sh
```

This starts the dev-fix backend stack (API + worker processes used by `/api/dev/fix`).

Quick health check:

```bash
curl -sS "http://localhost:8000/health"
```

## API

- **GET /** — Serves the chat web interface.
- **GET /health** — Health check.
- **POST /chat** — Body: `{"session_id": "...", "message": "...", "include_observability": false}`. Returns `{"reply": "...", "error": null, "trace": null}`. Set `include_observability: true` to get a `trace` array (input, LLM steps, tool calls, tool results) for the voice/trace UI.
- **GET /api/workspace/config** — Workspace settings (prompt_key, per-agent override content, default prompt, enabled tools, welcome).
- **PATCH /api/workspace/config** — Update workspace (prompt_key, system_prompt_override, enabled_tools, welcome_message). Override is stored in `data/prompt_overrides/{prompt_key}.txt`.

## Tools (agent “hands”)

- **search_faq** — Search company FAQ. Uses Upstash `faq:entries` when set (after running `scripts/seed_upstash.py`), otherwise in-memory entries. Matches on words so questions like “office and parking” find both.
- **list_available_slots** / **create_booking** — List slots and create a booking (mock; replace with calendar/CRM).
- **calculate** — Safe math for pricing or expressions.
- **get_current_datetime** — Returns the current date/time (optionally for a specific timezone) so the agent can answer “what’s the date/time?” or reason about “today”.
- **get_reference_info** — Reads `data/reference.json` and looks up structured info (e.g. policies, contact details, accepted document types) by key path like `policies.refund_days`.
- **list_supabase_tables** — Lists Supabase tables and their lookup/editable fields (from `data/tables/*.json`). Use before building a query when the user asks to add, update, or look up records.
- **build_supabase_query** — Builds a query spec (find/update/create) for a table using filters, updates, or insert payload. Pass the returned JSON to **execute_supabase_query**.
- **execute_supabase_query** — Runs the query spec against Supabase. Accepts a spec with `table_key` (and optionally `table`); if `table` is omitted, the processor resolves the Supabase table name from `data/tables/{table_key}.json`. Returns result rows or an error.

Which tools are enabled, and which **base prompt** (Demo vs Supabase receptionist) is used, is controlled from the **Workspace** page (`/workspace`), via `data/workspace_config.json`.

## Observability

- **Console logging**: Each request logs `session_id` and message preview; each response logs reply length. Tool calls (name + input/output) are logged via `app.callbacks.LoggingCallbackHandler` (handles both string and ToolMessage output from LangGraph).
- **Voice/trace UI**: The `/voice` page calls `/chat` with `include_observability=true` and renders a live **Agent trace** panel (input → LLM steps → tool calls → tool results) so you can see how the agent reasoned.
- **Recursion limit**: The agent uses a recursion limit of 50 so multi-step tool flows (e.g. list_supabase_tables → build_supabase_query → execute_supabase_query → answer) can complete without hitting the default cap.
- **Log level**: Set `LOG_LEVEL=DEBUG` in `.env` for noisier logs (e.g. agent actions).
- **LangSmith**: Set `LANGCHAIN_API_KEY` in `.env` (get one at [smith.langchain.com](https://smith.langchain.com)) to enable tracing. Traces appear in your LangSmith project for each chat (LLM calls, tool use, latency).

## Tests

```bash
pytest tests/ -v
```

## Seeding Upstash (optional)

If you use [Upstash](https://console.upstash.com/) for Redis, add `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` to `.env`, then run:

```bash
python scripts/seed_upstash.py
```

This populates Upstash with:

- **`faq:entries`** — JSON list of sample FAQ Q&A (30-day TTL).
- **`faq:0` … `faq:N`** — Same entries by index for direct lookup.
- **`agent:chat:seed-sample-session`** — Sample conversation in the same format the app uses for chat history (7-day TTL).

You can inspect and reuse these keys in the Upstash dashboard or in your app.

## Voice (Live Connect)

On the **main** branch, a voice assistant is available at **/voice**. Flow:

1. Open [http://localhost:8000/voice](http://localhost:8000/voice).
2. The app plays a welcome message using ElevenLabs TTS. The phrase comes from the workspace **welcome message** override (if set in `/workspace`), otherwise a time-based default (e.g. “Good morning, how can I help you?”).
3. Click **Start**, speak, and pause; your speech is sent to ElevenLabs STT → the same agent used for `/chat` → ElevenLabs TTS, and the reply is played back. Each turn is also logged in the **Agent trace** panel.
4. Repeat as needed; the same `session_id` is used so the agent keeps context.

**Required:** `ELEVENLABS_API_KEY` in `.env` (get it from [ElevenLabs](https://elevenlabs.io/app/settings/api-keys)). Optional: `ELEVENLABS_VOICE_ID` to choose a different voice.

## Workspace (agent behavior editor)

The **Workspace** page at **/workspace** lets you:

- Choose the **base prompt** for Live Connect and Chat: **Demo receptionist** or **Supabase receptionist**. This is the **prompt selector**; the selected prompt is stored as `prompt_key` in workspace config.
- Optionally **override** the system prompt per agent: overrides are stored in **`data/prompt_overrides/{prompt_key}.txt`** (e.g. `receptionist.txt`, `supabase_receptionist.txt`). If no override file exists, the base file in `app/prompts/` is used.
- Enable/disable individual **tools** (including Supabase tools when using the Supabase receptionist).
- Override the **voice welcome message**.

`prompt_key`, tools, and welcome message are in `data/workspace_config.json`; prompt overrides are in `data/prompt_overrides/`. The agent and voice welcome use the new settings on the next request.

## Supabase CRUD

When **Supabase receptionist** is selected in the workspace, the agent can look up, update, or create records in Supabase tables. Table definitions live in **`data/tables/`** as one JSON file per table (e.g. `news.json`, `piano_applications.json`, `piano_activations.json`). These files are **config/schema only** (they describe the table and which fields are lookup/editable); do not paste raw row data into them. Each file describes:

- **table** — Supabase table name.
- **description** — Short note for the LLM (e.g. “Blog and article records. Users often add or update by news_title.”).
- **lookup_fields** — Columns used to find a record (e.g. `news_title`, `id`).
- **primary_key** — Primary key column.
- **columns** — Column name → `{ "type": "...", "editable": true|false }`.

The agent uses **list_supabase_tables** to see available tables and fields, **build_supabase_query** to build a find/update/create spec, and **execute_supabase_query** to run it. To add another table, add a new `data/tables/<key>.json` file following the same shape; the loader in `app/tables_config.py` picks it up automatically.

**Required:** `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (or `SUPABASE_ANON_KEY`) in `.env`. Use the service role key for server-side CRUD if your policy allows.

## Optional

- **Redis / Upstash**: Set `UPSTASH_REDIS_REST_*` or `REDIS_URL` in `.env` to use `memory/redis_store` for custom persistence.
- **RAG**: Add a vector store and point `search_faq` at embeddings of `data/company_faq.pdf`.
