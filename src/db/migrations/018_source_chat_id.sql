ALTER TABLE sources ADD COLUMN chat_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_sources_chat_id ON sources(chat_id);
