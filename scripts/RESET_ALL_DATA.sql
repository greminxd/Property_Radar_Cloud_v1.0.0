-- Property Radar v1.4.4
-- FULL DATA RESET (schema/tables are preserved).
-- Run AFTER Setup Cloud. The next scan will repopulate listings and download a
-- fresh RCN archive from GUGiK.
PRAGMA foreign_keys = ON;

DELETE FROM price_history;
DELETE FROM listings;
DELETE FROM scan_runs;
DELETE FROM geocode_cache;
DELETE FROM parcel_lookup_cache;
DELETE FROM rcn_transactions;

DELETE FROM system_state
WHERE key IN (
  'listing_parser_version','rcn_parser_version','rcn_status',
  'scan_status','scan_started_at','scan_phase','scan_progress','scan_github_run_id','scan_trigger',
  'last_scan_finished_at','last_error','location_validation'
);
