CREATE TABLE IF NOT EXISTS radar_keyword_chats (
    keyword_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (keyword_id, chat_id),
    FOREIGN KEY (keyword_id) REFERENCES radar_keywords(id) ON DELETE CASCADE,
    FOREIGN KEY (chat_id) REFERENCES radar_chats(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_radar_kw_chats_chat ON radar_keyword_chats(chat_id);
CREATE INDEX IF NOT EXISTS idx_radar_kw_chats_kw ON radar_keyword_chats(keyword_id);
