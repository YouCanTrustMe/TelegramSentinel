ALTER TABLE items ADD COLUMN embedding BLOB;
ALTER TABLE items ADD COLUMN duplicate_of INTEGER;
CREATE INDEX IF NOT EXISTS idx_items_duplicate_of ON items(duplicate_of);
