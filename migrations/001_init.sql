PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('submit_daily_limit', '3'),
    ('video_max_mb', '50'),
    ('video_max_seconds', '300'),
    ('photo_max_mb', '10'),
    ('require_subscription', '1'),
    ('random_button_under_posts', '0'),
    ('rules_text', 'Присылай смешные видео и фото — лучшие попадут в канал. Один файл за раз. Без рекламы, жестокости и политики.');

CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    submissions_total INTEGER NOT NULL DEFAULT 0,
    approved_total    INTEGER NOT NULL DEFAULT 0,
    last_random_post  INTEGER,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    is_ordered  INTEGER NOT NULL DEFAULT 0,
    season      INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    source_chat_id     INTEGER NOT NULL,
    source_message_id  INTEGER NOT NULL,
    media_type         TEXT NOT NULL,
    file_id            TEXT,
    card_caption       TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    admin_chat_id      INTEGER,
    admin_message_id   INTEGER,
    reject_reason      TEXT,
    created_at         TEXT NOT NULL,
    reviewed_at        TEXT,
    reviewer_id        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions (user_id, created_at);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    submission_id INTEGER REFERENCES submissions(id),
    collection_id INTEGER REFERENCES collections(id),
    season        INTEGER,
    episode       INTEGER,
    media_type    TEXT NOT NULL,
    file_id       TEXT,
    caption       TEXT,
    author_id     INTEGER REFERENCES users(id),
    published_at  TEXT NOT NULL,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel_id, message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episode
    ON posts (collection_id, season, episode)
    WHERE episode IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_posts_collection
    ON posts (collection_id, season, episode, id);

CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    collection_id INTEGER REFERENCES collections(id),
    season        INTEGER,
    episode       INTEGER,
    caption       TEXT,
    publish_at    TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'waiting',
    error         TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_due ON queue (status, publish_at);

CREATE TABLE IF NOT EXISTS admin_sessions (
    user_id    INTEGER PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
