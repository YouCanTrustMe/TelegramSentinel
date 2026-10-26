-- A Telegram post's views, forwards and reactions, refreshed on every poll until it is sent
-- (shadow data: nothing reads it for the digest yet).
ALTER TABLE items ADD COLUMN views INTEGER;
ALTER TABLE items ADD COLUMN forwards INTEGER;
ALTER TABLE items ADD COLUMN reactions INTEGER;
ALTER TABLE items ADD COLUMN engagement_at TEXT;
