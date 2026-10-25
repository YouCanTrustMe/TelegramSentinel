-- A forum story's votes when it reached the feed (Hacker News "Points: 590"), shown as "▲ 590".
ALTER TABLE items ADD COLUMN votes INTEGER;
