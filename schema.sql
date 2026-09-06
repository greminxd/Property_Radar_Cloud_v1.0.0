PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_url TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  fingerprint TEXT,
  category TEXT,
  title TEXT,
  price REAL,
  area_m2 REAL,
  price_m2 REAL,
  plot_type TEXT,
  planning_status TEXT,
  location TEXT,
  lat REAL,
  lon REAL,
  distance_km REAL,
  phone TEXT,
  parcel_number TEXT,
  parcel_id TEXT,
  parcel_id_confidence TEXT,
  published_text TEXT,
  published_at TEXT,
  updated_text TEXT,
  updated_at TEXT,
  source_status TEXT DEFAULT 'active',
  archive_reason TEXT,
  area_warning TEXT,
  image_url TEXT,
  location_confidence TEXT,
  area_locality TEXT,
  area_confidence TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  missing_scans INTEGER NOT NULL DEFAULT 0,
  privacy_score INTEGER,
  privacy_reasons TEXT,
  deal_label TEXT,
  median_comparable REAL,
  market_mean_comparable REAL,
  comparable_count INTEGER DEFAULT 0,
  comparison_quality TEXT,
  price_alert_reference REAL,
  last_meaningful_price_change_at TEXT,
  last_price_old REAL,
  last_price_new REAL,
  last_price_change_pct REAL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id INTEGER NOT NULL,
  seen_at TEXT NOT NULL,
  price REAL NOT NULL,
  FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS listing_blacklist (
  canonical_url TEXT PRIMARY KEY,
  reason TEXT,
  source TEXT,
  title TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcel_lookup_cache (
  lookup_key TEXT PRIMARY KEY,
  locality TEXT,
  parcel_number TEXT,
  parcel_id TEXT,
  lat REAL,
  lon REAL,
  area_m2 REAL,
  status TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
  query TEXT PRIMARY KEY,
  lat REAL,
  lon REAL,
  display_name TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  downloaded_records INTEGER DEFAULT 0,
  accepted_records INTEGER DEFAULT 0,
  active_after_scan INTEGER DEFAULT 0,
  new_count INTEGER DEFAULT 0,
  price_change_count INTEGER DEFAULT 0,
  rejected_count INTEGER DEFAULT 0,
  deactivated_count INTEGER DEFAULT 0,
  healthy_sources INTEGER DEFAULT 0,
  total_sources INTEGER DEFAULT 0,
  diagnostics_json TEXT,
  status TEXT DEFAULT 'ok'
);

CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(active);
CREATE INDEX IF NOT EXISTS idx_listings_fp ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen);
CREATE INDEX IF NOT EXISTS idx_listings_published ON listings(published_at);
CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(area_locality);
CREATE INDEX IF NOT EXISTS idx_listings_type ON listings(plot_type);
CREATE INDEX IF NOT EXISTS idx_listings_source_status ON listings(source_status);
CREATE INDEX IF NOT EXISTS idx_listings_parcel_id ON listings(parcel_id);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id, seen_at);
CREATE INDEX IF NOT EXISTS idx_scan_runs_finished ON scan_runs(finished_at);
