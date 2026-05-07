# TelegramSentinel

Personal AI news digest bot. Collects posts from Telegram channels and RSS feeds, classifies them with Groq LLM, and delivers a structured daily digest to a Telegram supergroup. Includes a real-time keyword alert system (Radar) and a full inline-keyboard admin UI — no config files needed after initial setup.

## Features

- **Multi-source collection** — Telegram channels (via userbot) + RSS/Atom feeds
- **AI classification** — Groq `llama-3.3-70b` assigns 1–5 importance score and writes a ≤15-word Ukrainian summary per item
- **Per-source prompt instructions** — custom AI hints per source (e.g. "keep proper nouns", "focus on numbers")
- **Digest** — sent on a configurable schedule, grouped by category → source, items sorted by importance; same-topic follow-ups within a source are AI-merged into one entry
- **Blocked words filter** — items matching any blocked word are silently excluded from the digest
- **Radar** — real-time keyword alerts: userbot listens to monitored chats, fires an alert to the admin the moment a keyword is matched; per-keyword/chat cooldown prevents spam
- **Pending sources** — channels that can't be joined immediately are saved and retried automatically every hour
- **Full inline-keyboard admin UI** — manage everything from within Telegram, no terminal needed after deploy

## How it works

```
[Telegram channels]  ──┐
                        ├──▶  Groq classifier  ──▶  SQLite
[RSS feeds]          ──┘      (score + summary)
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

[Radar]  userbot real-time handler ──▶ keyword match ──▶ admin alert
```

## Digest format

Each source block is a collapsible `<blockquote expandable>`:

```
📋 Digest — 07 May 2026

💸 crypto
▸ CoinTelegraph
  14⏰ · ★★★★☆ · Ethereum ETF volume hits record high 🔗
  15⏰ · ★★★☆☆ · Stablecoins may reach $4T by 2030 🔗
▸ Capitanik
  ...
```

Items show: `{hour}⏰ · {★ rating} · {summary} 🔗`

## Architecture

Two Pyrogram clients run concurrently in one async process:

| Client | Role |
|---|---|
| `sentinel_userbot` | User account — polls channels every 5 min via `get_chat_history`, handles Radar real-time events |
| `sentinel_bot` | Bot token — sends digests, handles all admin commands via inline keyboard |

Outbound messages go through the Bot HTTP API (`sender.py`), not MTProto.

### Key modules

```
main.py                         entrypoint — starts both clients, scheduler, collectors
src/
  config.py                     pydantic-settings, single Settings instance
  scheduler.py                  APScheduler — one job per unique digest_time across categories;
                                  rebuilt live on category changes
  collectors/
    telegram_collector.py       userbot polls get_chat_history every 5 min
    rss_collector.py            feedparser, polls every 15 min
    folder_manager.py           maintains "Sentinel" folder in userbot account
  processor/
    classifier.py               Groq → score 1-5 + Ukrainian summary ≤15 words;
                                  rate-limited to 29 RPM (free tier)
    deduplicator.py             message_id = tg_{channel}_{id} or md5(url:id)
  dispatcher/
    digest_builder.py           groups items, AI-merges same-topic per source,
                                  applies blocked-word filter, splits at 4000 chars
    sender.py                   Bot HTTP API: send_message, send_document, pin/unpin
  radar/
    matcher.py                  case-insensitive substring match
    cooldown.py                 in-memory (keyword, chat_id) → last_alert timestamp
    handlers.py                 userbot on_message/on_edited_message → alert to admin
  bot/
    commands.py                 registers all bot handlers on startup
    handlers/
      categories.py             /categories inline UI (add/edit/reorder/delete)
      sources.py                source view, add/rename/reassign/remove, per-source prompt
      blocked.py                /blocked — add/remove blocked words
      radar.py                  /radar — keywords, monitored chats, blacklist, status
      misc.py                   /stats, /logs, /digest, /help
      conversation.py           all text-input wizards (add category/source/keyword/etc.)
    keyboards.py                InlineKeyboardMarkup builders
    state.py                    shared _pending wizard-state dict
  db/
    models.py                   all async DB helpers (aiosqlite, every fn opens own connection)
    migrations/                 *.sql applied in lexical order; _migrations table tracks state
```

### Database

SQLite at `data/sentinel.db`.

| Table | Purpose |
|---|---|
| `sources` | channels and feeds; `status` = active/pending; `sort_order`; `prompt_extra` |
| `categories` | name, emoji, `digest_time` (HH:MM or comma-separated), `sort_order` |
| `items` | collected posts; `sent` flag; `summary` with ★ prefix |
| `digest_log` | per-digest audit: item count, status |
| `blocked_words` | lowercase words; matched against summary + raw text |
| `app_settings` | key/value: `pinned_digest_message_id`, etc. |
| `radar_keywords` | keywords to watch for |
| `radar_chats` | chats monitored by radar |
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
| `/radar` | Real-time keyword alert settings — keywords, monitored chats, blacklist, alert history |
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
