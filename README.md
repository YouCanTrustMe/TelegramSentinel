# TelegramSentinel

Personal AI news digest bot. Collects posts from Telegram channels and RSS feeds, classifies them with Groq LLM, and delivers a structured daily digest to a Telegram supergroup. A full inline-keyboard admin UI means no config files are needed after initial setup.

## Features

- **Multi-source collection** — Telegram channels (via userbot) + RSS/Atom feeds
- **AI classification** — a concise Ukrainian summary (≤15 words, up to ~25 for detail-heavy items) plus a key phrase used as the link anchor; each task is routed to a free-tier model and fails over across separate provider quotas (Mistral, Gemini, Groq) so no single daily quota stalls a digest
- **Per-source prompt instructions** — custom AI hints per source (e.g. "keep proper nouns", "focus on numbers", "no merge", "no translate")
- **Digest** — sent on a per-category schedule, grouped by category → source; same-topic follow-ups within a source are AI-merged into one entry
- **Content filter** — natural-language rules (e.g. "local traffic accidents without casualties", "ads and promos") that an LLM applies each digest to silently exclude matching items
- **Cross-source dedup** — when several sources report the same story, only the highest-priority one is shown; the others become clickable source links beside it. Gemini embeddings (`GEMINI_API_KEY`) pre-select candidates and an LLM confirms each is the same event before hiding it, so vocabulary overlap alone never drops a distinct story; fail-open throughout
- **Pending sources** — channels that can't be joined immediately are saved and retried automatically every hour
- **Full inline-keyboard admin UI** — manage everything from within Telegram, no terminal needed after deploy

## How it works

```
[Telegram channels]  ──┐
                        ├──▶  Groq classifier  ──▶  SQLite
[RSS feeds]          ──┘      (Ukrainian summary)
                                      │
                          APScheduler (per-category cron)
                                      │
                                      ▼
                          Digest builder
                           • group by category / source
                           • AI merges same-topic items
                           • cross-source dedup (Gemini embeddings + LLM confirm)
                           • LLM content filter
                           • expandable blockquote per source
                                      │
                                      ▼
                              Bot HTTP API  ──▶  Telegram supergroup
```

## Digest format

Each source block is a collapsible `<blockquote expandable>`:

```
📋 Digest — 07 May 2026

💸 crypto
▸ CoinTelegraph
  14⏰ · Ethereum ETF volume hits record high
  15⏰ · Stablecoins may reach $4T by 2030
▸ Capitanik
  ...
```

Items show `{hour}⏰ · {summary}` — the key phrase inside each summary is the clickable link to the source post.

## Architecture

Two Pyrogram clients run concurrently in one async process:

| Client | Role |
|---|---|
| `sentinel_userbot` | User account — polls channels via `get_chat_history` (no real-time handler) |
| `sentinel_bot` | Bot token — sends digests, handles all admin commands via inline keyboard |

Outbound messages go through the Bot HTTP API (`sender.py`), not MTProto.

### Key modules

```
main.py                         entrypoint — starts both clients, scheduler, collectors
src/
  config.py                     pydantic-settings, single Settings instance
  scheduler.py                  APScheduler — one digest job per distinct time in
                                  category_times (no catch-all); maintenance jobs are
                                  added once, digest jobs rebuilt on schedule changes
  common/
    util.py                     small shared helpers (row_get, needs_summary)
    media.py                    media token ↔ emoji table
    schedule.py                 reading a category's digest times (no other deps)
  collectors/
    telegram_collector.py       userbot polls get_chat_history every 5 min
    rss_collector.py            feedparser, polls every 15 min (ingests title + description)
    folder_manager.py           maintains "Sentinel" folder in userbot account
  processor/
    llm/
      llm_client.py             provider-agnostic LLM transport: per-task routing +
                                  failover across separate free quotas, JSON repair, key alerts
      classifier.py             summarise / batch / group / content-filter calls
      prompts.py                LLM prompt templates
    dedup/
      deduplicator.py           message_id = tg_{channel}_{id} or md5(url:id)
      embedder.py               Gemini embedding transport + cosine/blob helpers
      cross_dedup.py            cross-source dedup + within-source clustering on shared embeddings
      merge.py                  within-source same-event merge (LLM-arbitrated)
  dispatcher/
    digest_builder.py           groups items, AI-merges same-topic per source, dedups
                                  cross-source, applies the LLM content filter, splits long digests
    sender.py                   Bot HTTP API: send_message, send_document, pin/unpin
    admin_alert.py              forwards selected warnings/errors to the admin (throttled)
  bot/
    commands.py                 registers all bot handlers on startup
    handlers/
      categories.py             /categories inline UI (add/edit/reorder/delete)
      sources.py                source view, add/rename/reassign/remove, per-source prompt
      blocked.py                /blocked — manage LLM content-filter rules
      timetable.py              digest schedule — pick a time, toggle categories on/off
      misc.py                   /stats, /logs, /digest, /help
      conversation.py           all text-input wizards (add category/source/rule/etc.)
    keyboards.py                InlineKeyboardMarkup builders
    state.py                    shared _pending wizard-state dict
  db/
    base.py                     connection, migration runner, app settings
    sources.py / items.py / categories.py / blocked.py
                                  per-domain async DB helpers (aiosqlite)
    models.py                   facade re-exporting the whole DB surface
    migrations/                 *.sql applied in lexical order; _migrations table tracks state
```

### Database

SQLite at `data/sentinel.db`.

| Table | Purpose |
|---|---|
| `sources` | channels and feeds; `status` = active/pending; `sort_order`; `prompt_extra` |
| `categories` | name, emoji, `sort_order` |
| `category_times` | one row per digest time: `(category, hour, minute)`; a category with no row is never sent |
| `items` | collected posts; `sent` flag; `summary` + `key_phrase`; `embedding` + `duplicate_of` for cross-source dedup |
| `digest_log` | per-digest audit: item count, status |
| `blocked_words` | content-filter rule descriptions; matched semantically by the LLM each digest |
| `app_settings` | key/value: `pinned_digest_message_id`, etc. |

## Stack

| Layer | Tech |
|---|---|
| Collectors | Pyrogram 2.0, feedparser |
| AI | Mistral (`mistral-small`) → Gemini (`3.5-flash-lite`) → Groq (`gpt-oss-120b`) + Gemini embeddings for dedup |
| Storage | SQLite + aiosqlite |
| Scheduler | APScheduler 3.x |
| Bot | Pyrogram bot + Bot HTTP API |
| Deploy | Docker + docker-compose (OCI x86_64) |

## Setup

### 1. Prerequisites

- Python 3.12+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Groq API key (free tier: 30 RPM) from [console.groq.com](https://console.groq.com)

### 2. Install

```bash
git clone https://github.com/YouCanTrustMe/TelegramSentinel.git
cd TelegramSentinel
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# fill in all values
```

### 4. Generate Pyrogram session (once, interactive)

Run locally — this requires entering your phone number and OTP:

```bash
python -c "
import asyncio
from pyrogram import Client
from src.config import settings

async def gen():
    async with Client(
        'sessions/sentinel_userbot',
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        phone_number=settings.telegram_phone,
    ):
        print('Session saved.')

asyncio.run(gen())
"
```

### 5. Run

```bash
# local
python main.py

# Docker
docker compose up --build -d
```

## Bot commands

All commands are private-chat only, admin-only (set via `TELEGRAM_ADMIN_ID`).

| Command | Description |
|---|---|
| `/categories` | Manage categories and sources — add, edit, reorder, set digest time, bulk-set AI prompt |
| `/blocked` | Content filter — manage natural-language rules; an LLM suppresses matching items each digest |
| `/digest` | Trigger digest immediately |
| `/stats` | Items collected in last 24h, broken down by category and source |
| `/logs` | Last 20 log lines; button to download full log file |
| `/cancel` | Cancel current input wizard |

## Deployment (OCI Always Free)

x86_64 instance. Data and sessions persist in local volumes:

```yaml
volumes:
  - ./data:/app/data
  - ./sessions:/app/sessions
```

Generate the Pyrogram session locally first, copy `sessions/` to the server, then:

```bash
docker compose up --build -d
```

Always rebuild after code changes — `docker compose restart` does not apply new code.

## License

MIT
