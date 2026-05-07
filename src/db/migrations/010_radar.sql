CREATE TABLE IF NOT EXISTS radar_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS radar_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_ref TEXT NOT NULL UNIQUE,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS radar_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS radar_alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    chat_ref TEXT NOT NULL,
    author_id INTEGER,
    message_text TEXT,
    message_url TEXT,
    alerted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
