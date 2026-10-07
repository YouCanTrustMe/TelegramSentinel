-- Opt a source out of the 14-day "silent" push: some channels post rarely by nature.
-- Rename/ban/private alerts are separate and are not affected.
ALTER TABLE sources ADD COLUMN silent_alert_muted INTEGER NOT NULL DEFAULT 0;
