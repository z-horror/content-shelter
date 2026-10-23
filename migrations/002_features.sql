ALTER TABLE collections ADD COLUMN last_episode INTEGER NOT NULL DEFAULT 0;

ALTER TABLE submissions ADD COLUMN file_unique_id TEXT;
ALTER TABLE submissions ADD COLUMN comment_text TEXT;

ALTER TABLE posts ADD COLUMN file_unique_id TEXT;

ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN ban_reason TEXT;
ALTER TABLE users ADD COLUMN banned_at TEXT;
