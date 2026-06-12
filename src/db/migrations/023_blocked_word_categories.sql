CREATE TABLE IF NOT EXISTS blocked_word_categories (
    word_id  INTEGER NOT NULL REFERENCES blocked_words(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (word_id, category)
);
