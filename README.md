# TelegramSentinel

Personal AI news digest bot. Collects posts from Telegram channels and RSS feeds, classifies them with Groq LLM, and delivers a structured daily digest to a Telegram supergroup. Includes a keyword alert system (Radar) and a full inline-keyboard admin UI — no config files needed after initial setup.

## Features

- **Multi-source collection** — Telegram channels (via userbot) + RSS/Atom feeds
- **AI classification** — Groq `llama-3.3-70b` writes a concise Ukrainian summary (≤15 words, up to ~25 for detail-heavy items) and a key phrase used as the link anchor
- **Per-source prompt instructions** — custom AI hints per source (e.g. "keep proper nouns", "focus on numbers", "no merge", "no translate")
- **Digest** — sent on a per-category schedule, grouped by category → source; same-topic follow-ups within a source are AI-merged into one entry
- **Blocked words filter** — items matching any blocked word are silently excluded from the digest
- **Cross-source dedup** — when several sources report the same story, only the highest-priority one is shown; the others become clickable source links beside it (Gemini embeddings, `GEMINI_API_KEY`; starts in shadow mode and is fail-open, so nothing is hidden until validated)
- **Radar** — keyword alerts: a collector polls monitored chats every 60s and fires an alert to the admin the moment a keyword is matched; a daily job verifies the monitored chats are still reachable
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
                           • blocked-word filter
                           • expandable blockquote per source
                                      │
                                      ▼
                              Bot HTTP API  ──▶  Telegram supergroup

[Radar]  collector polls monitored chats (60s) ──▶ keyword match ──▶ admin alert
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
| `sentinel_userbot` | User account — polls channels and Radar chats via `get_chat_history` (no real-time handler) |
| `sentinel_bot` | Bot token — sends digests, handles all admin commands via inline keyboard |

Outbound messages go through the Bot HTTP API (`sender.py`), not MTProto.

### Key modules

```
main.py                         entrypoint — starts both clients, scheduler, collectors
src/
  config.py                     pydantic-settings, single Settings instance
  scheduler.py                  APScheduler — a catch-all job plus per-time jobs for
                                  categories with a custom digest_time; rebuilt on changes
  collectors/
    telegram_collector.py       userbot polls get_chat_history every 5 min
    rss_collector.py            feedparser, polls every 15 min
    radar_collector.py          polls monitored chats every 60s → radar handler
    folder_manager.py           maintains "Sentinel" folder in userbot account
  processor/
    classifier.py               Groq prompts → Ukrainian summary + key phrase
    groq_client.py              shared Groq client + token-bucket rate limit + backoff/quota handling
    deduplicator.py             message_id = tg_{channel}_{id} or md5(url:id)
  dispatcher/
    digest_builder.py           groups items, AI-merges same-topic per source,
                                  applies blocked-word filter, splits long digests
    sender.py                   Bot HTTP API: send_message, send_document, pin/unpin
    admin_alert.py              forwards selected warnings/errors to the admin (throttled)
  radar/
    matcher.py                  case-insensitive substring keyword match
    handlers.py                 process_radar_message — blacklist/keyword filter, builds admin alert
    verify.py                   daily reachability check for monitored chats
  bot/
    commands.py                 registers all bot handlers on startup
    handlers/
      categories.py             /categories inline UI (add/edit/reorder/delete)
      sources.py                source view, add/rename/reassign/remove, per-source prompt
      blocked.py                /blocked — add/remove blocked words
      radar.py                  /radar entrypoint; radar_common/keywords/chats/blacklist.py — UI per sub-feature
      misc.py                   /stats, /logs, /digest, /help
      conversation.py           all text-input wizards (add category/source/keyword/etc.)
    keyboards.py                InlineKeyboardMarkup builders
    state.py                    shared _pending wizard-state dict
  db/
    base.py                     connection, migration runner, app settings
    sources.py / items.py / categories.py / blocked.py / radar.py
                                  per-domain async DB helpers (aiosqlite)
    models.py                   facade re-exporting the whole DB surface
    migrations/                 *.sql applied in lexical order; _migrations table tracks state
```

### Database

SQLite at `data/sentinel.db`.

| Table | Purpose |
|---|---|
| `sources` | channels and feeds; `status` = active/pending; `sort_order`; `prompt_extra` |
| `categories` | name, emoji, `digest_time` (HH:MM or comma-separated), `sort_order` |
| `items` | collected posts; `sent` flag; `summary` + `key_phrase` |
| `digest_log` | per-digest audit: item count, status |
| `blocked_words` | lowercase words; matched against summary + raw text |
| `app_settings` | key/value: `pinned_digest_message_id`, etc. |
| `radar_keywords` | keywords to watch for |
| `radar_chats` | chats monitored by radar; `last_seen_msg_id` for polling |
| `radar_keyword_chats` | which keywords are linked to which chats |
| `radar_blacklist` | user IDs to ignore in radar |
| `radar_alert_log` | history of fired alerts |

## Stack

| Layer | Tech |
|---|---|
| Collectors | Pyrogram 2.0, feedparser |
| AI | Groq — `llama-3.3-70b-versatile` |
| Storage | SQLite + aiosqlite |
| Scheduler | APScheduler 3.x |
| Bot | Pyrogram bot + Bot HTTP API |
| Deploy | Docker + docker-compose (OCI ARM64) |

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
| `/blocked` | Blocked words filter — words matching any item's summary or raw text suppress it from the digest |
| `/radar` | Keyword alert settings — keywords, monitored chats, blacklist, status |
| `/digest` | Trigger digest immediately |
| `/stats` | Items collected in last 24h, broken down by category and source |
| `/logs` | Last 20 log lines; button to download full log file |
| `/cancel` | Cancel current input wizard |

## Deployment (OCI Always Free)

ARM64 instance. Data and sessions persist in local volumes:

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
