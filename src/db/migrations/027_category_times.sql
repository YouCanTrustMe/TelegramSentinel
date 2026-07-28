-- Digest times were a comma-separated string on categories, so every reader parsed
-- it and every writer rebuilt it. One row per time makes a slot a real thing:
-- duplicates are impossible, an out-of-range time cannot be stored at all, and
-- "who fires at 16:00" becomes a query instead of string matching.
CREATE TABLE IF NOT EXISTS category_times (
    category TEXT NOT NULL,
    hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    minute INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 59),
    PRIMARY KEY (category, hour, minute)
);

-- Backfill by splitting the old string on commas. Values that are not a valid
-- HH:MM in range are dropped rather than migrated: they could never have produced
-- a cron job anyway, and the timetable now shows such a category as "never".
WITH RECURSIVE split(name, piece, rest) AS (
    SELECT name, '', COALESCE(digest_time, '') || ',' FROM categories
    UNION ALL
    SELECT name,
           substr(rest, 1, instr(rest, ',') - 1),
           substr(rest, instr(rest, ',') + 1)
    FROM split
    WHERE rest <> ''
)
INSERT OR IGNORE INTO category_times (category, hour, minute)
SELECT name,
       CAST(substr(trim(piece), 1, instr(trim(piece), ':') - 1) AS INTEGER),
       CAST(substr(trim(piece), instr(trim(piece), ':') + 1) AS INTEGER)
FROM split
WHERE instr(trim(piece), ':') > 0
  AND CAST(substr(trim(piece), 1, instr(trim(piece), ':') - 1) AS INTEGER) BETWEEN 0 AND 23
  AND CAST(substr(trim(piece), instr(trim(piece), ':') + 1) AS INTEGER) BETWEEN 0 AND 59;

ALTER TABLE categories DROP COLUMN digest_time;
