-- =========================================================
-- Golf Rival - SQLite schema
-- =========================================================

PRAGMA foreign_keys = ON;

-- ---- Maps (lookup) -------------------------------------
CREATE TABLE IF NOT EXISTS maps (
    map_id   TEXT PRIMARY KEY,
    map_name TEXT NOT NULL UNIQUE
);

-- ---- Users (one row per registration) ------------------
CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    username          TEXT NOT NULL,
    country           TEXT NOT NULL,
    registration_os   TEXT NOT NULL,    -- iOS / Android
    registration_ts   INTEGER NOT NULL, -- unix time
    registration_date TEXT NOT NULL     -- YYYY-MM-DD (UTC), za output
);

-- ---- Sessions (aggregated from session_ping events) ----
CREATE TABLE IF NOT EXISTS sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL,
    device_os        TEXT NOT NULL,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- ---- Matches (per-user-perspective) --------------------
-- Jedan fizicki mec => 1 ili 2 reda (po jedan za svakog igraca).
-- match_uuid spaja redove istog meca.
-- device_os je OS sesije u kojoj je mec odigran (NULL ako mec ne pada
-- ni u jednu sesiju ovog igraca).
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_uuid  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    opponent_id TEXT NOT NULL,
    map_id      TEXT NOT NULL,
    start_ts    INTEGER,    -- moze biti NULL ako fali match_start od ovog usera
    finish_ts   INTEGER,    -- moze biti NULL ako fali match_finish od ovog usera
    outcome     REAL,       -- 1.0 / 0.5 / 0.0; NULL ako fali finish od ovog usera
    device_os   TEXT,       -- iOS / Android / NULL
    FOREIGN KEY (map_id) REFERENCES maps(map_id)
);

-- ---- Indeksi za brze upite -----------------------------
CREATE INDEX IF NOT EXISTS idx_users_country     ON users(country);
CREATE INDEX IF NOT EXISTS idx_sessions_user     ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_os       ON sessions(device_os);
CREATE INDEX IF NOT EXISTS idx_matches_user      ON matches(user_id);
CREATE INDEX IF NOT EXISTS idx_matches_map       ON matches(map_id);
CREATE INDEX IF NOT EXISTS idx_matches_uuid      ON matches(match_uuid);
CREATE INDEX IF NOT EXISTS idx_matches_finish    ON matches(finish_ts);
CREATE INDEX IF NOT EXISTS idx_matches_device_os ON matches(device_os);