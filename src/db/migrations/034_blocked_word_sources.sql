-- No foreign key on source_id: a rule limited to a source that is later removed keeps that
-- id, so it judges nothing instead of falling back to every source of its categories.
CREATE TABLE IF NOT EXISTS blocked_word_sources (
    word_id   INTEGER NOT NULL REFERENCES blocked_words(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL,
    PRIMARY KEY (word_id, source_id)
);
