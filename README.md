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

**Minimum to run:** Python 3.9+ and a valid **OpenAI API key**. Redis, Pinecone, and ElevenLabs are optional.

## Structure

```
<project-root>/
├── app/
│   ├── main.py              # FastAPI server, routes (/chat, /voice, /workspace, etc.)
│   ├── agent.py             # Orchestrator: LLM + tools (LangGraph ReAct agent, workspace‑driven config)
│   ├── agents/              # Personas (receptionist, SME expert)
│   ├── tools/               # Skills: search_faq, booking, calculator, datetime, reference JSON
│   ├── workspace_config.py  # Read/write workspace config (prompt override, enabled tools, welcome)
│   ├── memory/              # Optional Redis chat history
│   └── prompts/             # System prompt and reasoning steps
├── data/
│   ├── reference.json       # Reference data for get_reference_info tool
│   └── workspace_config.json# Current workspace settings (overrides; edited via /workspace UI)
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

## API

- **GET /** — Serves the chat web interface.
- **GET /health** — Health check.
- **POST /chat** — Body: `{"session_id": "...", "message": "..."}`. Returns `{"reply": "...", "error": null}`.

## Tools (agent “hands”)

- **search_faq** — Search company FAQ. Uses Upstash `faq:entries` when set (after running `scripts/seed_upstash.py`), otherwise in-memory entries. Matches on words so questions like “office and parking” find both.
- **list_available_slots** / **create_booking** — List slots and create a booking (mock; replace with calendar/CRM).
- **calculate** — Safe math for pricing or expressions.
- **get_current_datetime** — Returns the current date/time (optionally for a specific timezone) so the agent can answer “what’s the date/time?” or reason about “today”.
- **get_reference_info** — Reads `data/reference.json` and looks up structured info (e.g. policies, contact details, accepted document types) by key path like `policies.refund_days`.

Which tools are enabled is controlled from the **Workspace** page (`/workspace`), via `data/workspace_config.json`.

## Observability

- **Console logging**: Each request logs `session_id` and message preview; each response logs reply length. Tool calls (name + input/output) are logged via `app.callbacks.LoggingCallbackHandler`.
- **Voice/trace UI**: The `/voice` page calls `/chat` with `include_observability=true` and renders a live **Agent trace** panel (input → LLM steps → tool calls → tool results) so you can see how the agent reasoned.
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

- Edit the **receptionist system prompt** (or fall back to `app/prompts/system_receptionist.txt`).
- Enable/disable individual **tools** (FAQ search, booking, calculator, datetime, reference).
- Override the **voice welcome message**.

Changes are persisted in `data/workspace_config.json` and picked up automatically by the agent and voice welcome on the next request.

## Optional

- **Redis / Upstash**: Set `UPSTASH_REDIS_REST_*` or `REDIS_URL` in `.env` to use `memory/redis_store` for custom persistence.
- **RAG**: Add a vector store and point `search_faq` at embeddings of `data/company_faq.pdf`.
