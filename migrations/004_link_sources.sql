ALTER TABLE submissions ADD COLUMN source_url TEXT;
ALTER TABLE submissions ADD COLUMN source_meta TEXT;

CREATE INDEX IF NOT EXISTS idx_submissions_source_url ON submissions (source_url);

INSERT OR IGNORE INTO settings (key, value) VALUES ('link_upload_enabled', '1');
