-- Short name a rule's block in the digest's Filtered section carries. NULL keeps the old
-- title, the rule's first clause cut at a word boundary.
ALTER TABLE blocked_words ADD COLUMN label TEXT;
