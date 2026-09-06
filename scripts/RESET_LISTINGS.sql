-- Property Radar v1.4.4
-- CLEAN START FOR OFFERS ONLY.
-- Keeps the RCN transaction archive, but removes every scraped listing and its
-- derived caches/history. Run AFTER Setup Cloud so all current tables exist.
PRAGMA foreign_keys = ON;

DELETE FROM price_history;
DELETE FROM listings;
DELETE FROM scan_runs;
DELETE FROM geocode_cache;
DELETE FROM parcel_lookup_cache;

-- Force the next scan to rebuild listing-derived state with the current parser.
DELETE FROM system_state
WHERE key IN (
  'listing_parser_version',
  'scan_status','scan_started_at','scan_phase','scan_progress','scan_github_run_id','scan_trigger',
  'last_scan_finished_at','last_error','location_validation'
);
