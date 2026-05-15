ALTER TABLE radar_chats ADD COLUMN chat_id INTEGER;
ALTER TABLE radar_chats ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE radar_chats ADD COLUMN last_verified_at TEXT;
