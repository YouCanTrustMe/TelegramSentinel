-- Radar was extracted into its own project (TelegramRadar) with its own
-- database. Drop the now-orphan radar tables from Sentinel. The data was
-- migrated out beforehand; the historical radar migrations are kept so the
-- schema history stays intact.
DROP TABLE IF EXISTS radar_keyword_chats;
DROP TABLE IF EXISTS radar_alert_log;
DROP TABLE IF EXISTS radar_blacklist;
DROP TABLE IF EXISTS radar_chats;
DROP TABLE IF EXISTS radar_keywords;
