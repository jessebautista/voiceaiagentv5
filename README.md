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

**Minimum to run:** Python 3.9+ and a valid **OpenAI API key**. Redis and Pinecone are optional.

## Structure

```
<project-root>/
├── app/
│   ├── main.py              # Entry point: FastAPI server and routes
│   ├── agent.py             # Orchestrator: LLM + tools (LangGraph ReAct agent)
│   ├── agents/              # Personas (receptionist, SME expert)
│   ├── tools/               # Skills: search_faq, booking, calculator
│   ├── memory/              # Optional Redis chat history
│   └── prompts/             # System prompt and reasoning steps
├── data/                    # Knowledge base (e.g. company_faq.pdf)
├── scripts/
│   └── seed_upstash.py      # Seed Upstash with sample FAQ and chat data
├── static/                  # Web UI (index.html)
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

## Observability

- **Console logging**: Each request logs `session_id` and message preview; each response logs reply length. Tool calls (name + input/output) are logged via `app.callbacks.LoggingCallbackHandler`.
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

## Optional

- **Redis / Upstash**: Set `UPSTASH_REDIS_REST_*` or `REDIS_URL` in `.env` to use `memory/redis_store` for custom persistence.
- **RAG**: Add a vector store and point `search_faq` at embeddings of `data/company_faq.pdf`.
