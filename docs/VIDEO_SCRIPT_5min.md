# 5-Minute Video Script: How We Built This AI App

**Target length:** ~5 minutes at ~150 words/min → ~750 words  
**Tone:** Clear, explanatory, suitable for screen recording + voiceover.

---

## [0:00–0:30] HOOK & INTRO

**[Screen: Show the app — chat or voice page]**

"We built an AI receptionist that can chat, talk on the phone, search FAQs, make bookings, and even update your database — and it remembers you across sessions. In the next five minutes I’ll walk you through how we put it together."

**[Screen: Optional — simple architecture diagram or folder tree]**

"The stack is: a FastAPI backend, an LLM agent powered by LangGraph, optional voice via ElevenLabs, and optional memory in Redis. Let’s start with the big picture."

---

## [0:30–1:30] ARCHITECTURE OVERVIEW

**[Screen: Project structure or README]**

"Everything starts in **main.py**. That’s our FastAPI app. It serves the web UI — chat, voice, workspace, settings — and exposes the API. The root URL serves the chat page; we have routes for **/chat**, **/voice**, **/voice/tts**, **/voice/stt**, and **/api/workspace/config** for editing the agent’s behavior."

"The brain lives in **agent.py**. We use **LangGraph**’s `create_react_agent`: one LLM — we use OpenAI’s GPT-4o-mini by default — plus a set of tools the agent can call. Each user message goes to the agent; it can reason and call tools in a loop until it has a final answer. Conversation state is kept with a **MemorySaver** keyed by session ID, so the same thread stays in context."

"So: FastAPI for HTTP and the UI, LangGraph for the agent loop, and the real power is in the **tools** and **memory** we plug in. Let’s look at those next."

---

## [1:30–2:30] THE AGENT: PROMPTS & WORKSPACE

**[Screen: app/prompts/ or Workspace UI]**

"The agent’s personality and instructions come from **system prompts**. We have base prompts in **app/prompts** — for example a demo receptionist and a Supabase receptionist — and we can override them per agent in **data/prompt_overrides** without touching code."

"Which prompt and which tools are active is controlled by the **Workspace**. The Workspace page lets you pick the base prompt — demo or Supabase receptionist — edit an override, turn tools on or off, and set the voice welcome message. That’s stored in **data/workspace_config.json** and in the override files. On the next request, the agent just uses the new config. So we can tune behavior for different use cases without redeploying."

"The agent is a single ReAct loop: user message in, optional memory context prepended, then the LLM can respond or call tools; tool results go back in and the loop continues until the model returns a final answer. That reply is what we send back to the client."

---

## [2:30–3:30] TOOLS — THE AGENT’S “HANDS”

**[Screen: app/tools/ or list in README]**

"The agent doesn’t just talk — it has **tools**. In **app/tools** we define functions the LLM can call. For example: **search_faq** for company FAQ, **list_available_slots** and **create_booking** for appointments, **calculate** for math, **get_current_datetime** for date and time, and **get_reference_info** to read from a reference JSON file."

"When Supabase is configured we add **list_supabase_tables**, **build_supabase_query**, and **execute_supabase_query**. The agent can list tables, build a find-or-update-or-create spec, and execute it — so it can look up or change records in your database from natural language. Table configs live in **data/tables** as JSON: table name, description, lookup fields, and which columns are editable."

"We also have **email** tools: search emails, get content, parse PDF attachments, and pull text from Google Doc links. So if a user says they forwarded a PDF, the agent can find the email, extract the content, and for example create a program or record in Supabase. Which tools are enabled is again controlled from the Workspace."

---

## [3:30–4:15] STATEFUL MEMORY (REDIS)

**[Screen: app/memory/ or memory section in README]**

"To make the agent feel stateful we added **memory**. We use Redis — Upstash or standard Redis — for two things: **episodic** and **procedural** memory."

"**Episodic memory** stores facts, preferences, and user instructions — like ‘add 2026 in the program title when creating programs.’ After each turn we extract such facts from the conversation and store them per user. **Procedural memory** tracks which workflows the user runs and how often they succeed — for example ‘update_program’ with its steps and success rate."

"Before each request we run **cost-controlled recall**: we pull relevant memories with a token budget — about 500 tokens — so we don’t blow the context window. That string is prepended to the user message so the agent sees it. We also have an editable **data/memory/agent_instructions.txt** file that tells the agent how to use that context. So the agent can remember preferences and past actions across sessions without us hard-coding them."

---

## [4:15–5:00] VOICE & WRAP-UP

**[Screen: /voice page or voice endpoints]**

"For **voice**, we use **ElevenLabs**. The **/voice** page is a Live Connect–style UI: it plays a welcome phrase — from the workspace welcome message or a time-based default — then you speak. Your audio goes to ElevenLabs **speech-to-text**, the text is sent to the same **/chat** agent we already described, and the reply is turned into audio with **text-to-speech** and played back. Same session ID, so the agent keeps the conversation context. Optional **include_observability** lets the voice UI show the agent trace — tool calls and results — so you can see how it reasoned."

**[Screen: .env.example or README setup]**

"To run it: clone the repo, create a venv, install from **requirements.txt**, copy **.env.example** to **.env**, and set at least **OPENAI_API_KEY**. Then run **python -m app.main** and open localhost:8000. Redis, ElevenLabs, Supabase, and Gmail are optional; the app works with just the API key and gets more capable as you add them."

"So that’s the app: FastAPI and LangGraph, prompts and workspace, tools and memory, and optional voice — all wired so one agent can chat, talk, search, book, and update your data. Thanks for watching."

---

## PRODUCTION TIPS

- **Timing:** Read the script aloud and trim or expand to hit 5 minutes; cut less critical detail if over.
- **B-roll:** Show `app/main.py`, `app/agent.py`, `app/tools/`, `app/memory/`, Workspace UI, and `/voice` while narrating.
- **Diagrams:** A simple diagram (API → Agent → Tools + Memory) at 0:30 and again at 4:00 helps.
- **Demo:** Optional 30-second live demo (one chat turn + one tool call, or one voice turn) can replace some of the “tools” or “voice” narration.
