-- The message_id of the channel's own earlier post this one replies to, so the digest can
-- show the reply under it, or say what it answers when the original went out earlier.
ALTER TABLE items ADD COLUMN reply_to TEXT;
