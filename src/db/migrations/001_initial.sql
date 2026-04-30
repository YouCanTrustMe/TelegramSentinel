CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('telegram', 'rss')),
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    emoji TEXT NOT NULL DEFAULT '📌',
    topic_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    message_id TEXT NOT NULL UNIQUE,
    original_url TEXT,
    raw_text TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    importance TEXT CHECK(importance IN ('high', 'low')),
    published_at TEXT,
    processed_at TEXT,
    sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    items_total INTEGER NOT NULL DEFAULT 0,
    items_high INTEGER NOT NULL DEFAULT 0,
    items_low INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'
);

CREATE INDEX IF NOT EXISTS idx_items_importance ON items(importance, sent);
CREATE INDEX IF NOT EXISTS idx_items_processed_at ON items(processed_at);
