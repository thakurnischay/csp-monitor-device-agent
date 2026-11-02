-- Removes ALL Email Campaigns tables. THIS DESTROYS CAMPAIGN HISTORY. Take a backup of monitor.db first.
-- Not needed for a normal rollback: the previous version simply ignores these tables.
DROP TABLE IF EXISTS email_events;
DROP TABLE IF EXISTS email_attempts;
DROP TABLE IF EXISTS email_campaign_recipients;
DROP TABLE IF EXISTS email_campaigns;
DROP TABLE IF EXISTS email_suppressions;
DROP TABLE IF EXISTS operator_reconciliations;
DROP TABLE IF EXISTS operator_devices;
DROP TABLE IF EXISTS campaign_operators;
DROP TABLE IF EXISTS operator_imports;
