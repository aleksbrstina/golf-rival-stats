# Golf Rival Stats API

REST API and visualization for player and map statistics derived from raw
Golf Rival event logs (registrations, sessions, matches).

Built for the Nordeus Job Fair 2026 Data Engineering Challenge.

---

## Approach

The pipeline is split into two stages:

1. **ETL (`src/etl.py`)** — runs once. Reads `events.jsonl` and `maps.jsonl`,
   validates and deduplicates events, aggregates session pings into sessions,
   pairs `match_start`/`match_finish` events into matches, and writes a
   normalized SQLite database (`data/golf.db`).
2. **API (`src/api.py`)** — a FastAPI app that serves SQL-backed endpoints
   reading directly from the SQLite database. Each request runs a single
   query against the prepared schema, so endpoints respond in milliseconds.

This separation means the input file is processed only once; queries do not
re-parse JSONL.

### Database schema

Four tables:

| Table      | Purpose                                                          |
|------------|------------------------------------------------------------------|
| `maps`     | Lookup of `map_id` → `map_name`.                                 |
| `users`    | One row per registered user (earliest registration kept).        |
| `sessions` | One row per session, aggregated from session pings.              |
| `matches`  | One row **per player perspective** of a match. Two rows of the same physical match share a `match_uuid`. Includes `device_os` (the OS of the session in which the match was played, NULL if the match falls outside any of that user's sessions). |

See [`src/schema.sql`](src/schema.sql) for the full DDL and indexes.

### Why per-user-perspective rows in `matches`?

The challenge says a match may have only a partial set of events, including
cases where one player emitted no events at all. Storing one row per player
perspective lets us model partial matches naturally. `match_uuid` links the
two rows belonging to the same physical match, so distinct match counting
(e.g. `match_cnt` in `/map-stats`) is just `COUNT(DISTINCT match_uuid)`.

### How sessions are detected

Sessions are split using the 120-second gap rule only, without relying on the state column. Pings with a missing state value (99 in the input) are rejected earlier, during validation, because they do not meet the stated event requirements.

### Match pairing

Match events are grouped by canonical player pair (sorted `(user_id, opponent_id)`)
and `map_id`. Within each group, events are walked in chronological order and
split into cycles: a new cycle begins when a `match_start` arrives after a
`match_finish` was already seen in the current cycle. Each cycle becomes one
logical match. A cycle is dropped if it does not collectively contain at least
one `match_start` and one `match_finish` (per spec).

### Win-ratio convention

`outcome` values are 1 (win), 0.5 (draw), 0 (loss). All "win ratio" fields
in this project are computed as `AVG(outcome)` — i.e. average score per
match, treating a draw as half a win. This matches the convention used in
the challenge's example table.

---

## Installation

Requires **Python 3.10+**.

```bash
# clone & enter
git clone <your-repo-url> golf-rival-stats
cd golf-rival-stats

# create and activate venv
python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows PowerShell:
.\venv\Scripts\Activate.ps1

# install dependencies
pip install -r requirements.txt
```

Place `events.jsonl` and `maps.jsonl` inside the `data/` folder.

---

## Running

### 1. Build the database (once)

```bash
python src/etl.py
```

You should see output ending with something like:

- `[ETL] Removed 110 duplicate id rows`
- `[ETL] 4594 valid events; dropped 296 ...`
- `[ETL] 48 unique registered users`
- `[ETL] Built 621 sessions from 3406 pings`
- `[ETL] Built 458 match-rows (242 unique matches; dropped 8 incomplete cycles)`
- `[ETL] DB written to data/golf.db`

### 2. Start the API

```bash
uvicorn src.api:app --reload
```

The server listens on `http://localhost:8000`. Auto-generated Swagger UI is
available at `http://localhost:8000/docs`.

---

## API reference

### `GET /user-stats`

Per-player aggregate statistics, ordered by `total_playtime` descending.

| Query param  | Type           | Description                                                                           |
|--------------|----------------|---------------------------------------------------------------------------------------|
| `countries`  | list, optional | Country codes filter, repeatable. Example: `?countries=SRB&countries=DEU`.            |
| `OSs`        | list, optional | OS filter (`iOS` / `Android`). Applies to the user's **session OS** — playtime, win ratio, fav map and matches-per-session are restricted to data from sessions of the given OS. |

Response items contain: `username`, `country`, `fav_map`, `fav_map_win_ratio`,
`total_playtime`, `total_win_ratio`, `avg_matches_per_session`, `registration_date`.

```bash
curl 'http://localhost:8000/user-stats?countries=SRB'
curl 'http://localhost:8000/user-stats?OSs=iOS'
curl 'http://localhost:8000/user-stats?countries=SRB&countries=DEU&OSs=Android'
```

### `GET /map-stats/{map_name}`

Per-day map statistics, ordered by `date` descending.

| Param        | Type            | Description                                                                |
|--------------|-----------------|----------------------------------------------------------------------------|
| `map_name`   | path            | Name of the map (case-sensitive, e.g. `Inferno`).                          |
| `date_from`  | query, optional | Inclusive lower bound (`YYYY-MM-DD`).                                      |
| `date_to`    | query, optional | Inclusive upper bound (`YYYY-MM-DD`).                                      |

Returns 404 if the map name does not exist.

Response items contain: `date`, `avg_playtime`, `best_player_username`, `match_cnt`.

`best_player_username` is the player with the highest **cumulative** win ratio
on the map up to and including the given date (wins / matches across all
matches on that map up to that date, with ties broken by total matches and
then lexicographic `user_id`).

```bash
curl 'http://localhost:8000/map-stats/Inferno'
curl 'http://localhost:8000/map-stats/Lake?date_from=2026-04-05'
curl 'http://localhost:8000/map-stats/Forest?date_from=2026-04-04&date_to=2026-04-06'
```

### `GET /maps`

Lists available map names. Used by the chart frontend.

### `GET /chart`

HTML page rendering a live line chart of `match_cnt` per map for the last 7
days. Uses Chart.js loaded from CDN; pulls live data from `/map-stats` for
each map.

Open `http://localhost:8000/chart` in a browser.

---

## Data cleaning

The ETL discards events for the following reasons (counts on the supplied
sample dataset):

| Reason                          | Count |
|---------------------------------|-------|
| Duplicate `id`                  | 110   |
| `ping_bad_state`                | 99    |
| `ping_bad_os`                   | 79    |
| `missing_user_id`               | 66    |
| `match_start_missing_fields`    | 23    |
| `match_finish_missing_fields`   | 17    |
| `match_finish_bad_outcome`      | 10    |
| `registration_missing_fields`   | 2     |
| Events from non-registered user | 286   |
| Incomplete match cycles         | 8     |

Validation rules:

- All top-level fields (`id`, `timestamp`, `event_type`, `user_id`, `event_data`) must be present.
- Timestamps must be positive integers.
- `device_os` must be `iOS` or `Android` (where applicable).
- `state` must be one of `started`, `in_progress`, `ended`.
- `outcome` must be one of `0`, `0.5`, `1`.
- `map_id` in match events must exist in `maps.jsonl`.
- A user cannot play against themselves (`user_id == opponent_id`).
- For duplicate `id` values, the earliest event by timestamp is kept.
- For users with multiple registrations, the earliest registration is kept.
- Events from users that never registered (i.e. no surviving registration event)
  are dropped — without registration metadata, they cannot appear in either API.

---

## Assumptions

- "Win ratio" everywhere is computed as `AVG(outcome)` (draws contribute 0.5).
- A match is considered to have happened on a date if the **maximum**
  `match_finish` timestamp across both player rows falls on that date.
- Match duration is `max(finish_ts) - min(start_ts)` across both player rows
  of the same `match_uuid`. Matches with `start_ts = NULL` (no surviving
  `match_start` from either player) are excluded from `avg_playtime` but
  still counted in `match_cnt`.
- The `OSs` filter on `/user-stats` is interpreted as restricting the data
  used in *all* derived statistics (playtime, win ratio, fav map, matches
  per session). Each match is tagged with the OS of the session in which it
  occurred. Matches that fall outside any session of that user have a NULL
  `device_os` and are excluded when an OS filter is applied.
- All timestamps are interpreted as UTC for date bucketing.
- Tie-breaking rules:
  - Favorite map: highest map win ratio → most matches on map → alphabetical map_id.
  - Best player on map: highest cumulative win ratio → most matches → alphabetical user_id.

---

## Project structure

```text
golf-rival-stats/
├── data/
│   ├── events.jsonl            # input
│   ├── maps.jsonl              # input
│   └── golf.db                 # produced by etl.py
├── src/
│   ├── schema.sql              # SQLite DDL
│   ├── db.py                   # SQLite connection helper
│   ├── etl.py                  # ETL pipeline
│   ├── api.py                  # FastAPI app
│   └── static/
│       └── chart.html          # visualization page (Chart.js)
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Bonus features

- **SQL database** — data is persisted in SQLite and all queries are SQL.
- **Detailed data cleaning** — every dropped event is categorised and
  reported during ETL.
- **Sessions without `state`** — implemented via the 120-second-gap rule;
  the `state` column is not relied upon (resolved as a side effect of the
  cleaner approach).
- **Visualization** — `/chart` renders a 7-day line chart with one line per
  map, fed live from `/map-stats`.
- **Auto-generated API docs** — Swagger UI at `/docs`.

---

## Tech stack

- Python 3.10+
- FastAPI + Uvicorn
- SQLite (standard library `sqlite3`)
- Chart.js 4.x (CDN, used by `/chart` only)
