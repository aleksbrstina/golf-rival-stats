"""
Golf Rival - REST API.

Pokretanje:
    uvicorn src.api:app --reload
Dokumentacija (Swagger UI):
    http://localhost:8000/docs
"""
from datetime import datetime as _dt
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from src.db import get_conn

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Golf Rival Stats API",
    description="API za statistiku igraca i mapa za Golf Rival.",
    version="1.0.0",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _placeholders(values: list) -> str:
    """Vraca '?,?,?' za koriscenje u IN klauzuli."""
    return ",".join("?" * len(values))


# --------------------------------------------------------------------------- #
# /user-stats
# --------------------------------------------------------------------------- #
@app.get("/user-stats")
def user_stats(
    countries: Optional[list[str]] = Query(
        default=None,
        description="Filter po country code (npr. ?countries=SRB&countries=DEU).",
    ),
    OSs: Optional[list[str]] = Query(
        default=None,
        description="Filter po OS-u sesije (iOS / Android). "
                    "Vazi za sve statistike izvedene iz sesija/meceva.",
    ),
):
    """
    Vraca listu igraca sa statistikom, sortirano po total_playtime DESC.
    Polja: username, country, fav_map, fav_map_win_ratio, total_playtime,
           total_win_ratio, avg_matches_per_session, registration_date.
    """
    if OSs:
        bad = [o for o in OSs if o not in ("iOS", "Android")]
        if bad:
            raise HTTPException(400, f"Invalid OS values: {bad}")

    user_filter, user_params = "", []
    if countries:
        user_filter = f"WHERE country IN ({_placeholders(countries)})"
        user_params = list(countries)

    os_filter_sess, os_params_sess = "", []
    os_filter_match, os_params_match = "", []
    if OSs:
        os_filter_sess = f"WHERE device_os IN ({_placeholders(OSs)})"
        os_params_sess = list(OSs)
        os_filter_match = f"AND device_os IN ({_placeholders(OSs)})"
        os_params_match = list(OSs)

    sql = f"""
    WITH
    fu AS (
        SELECT * FROM users {user_filter}
    ),
    fs AS (
        SELECT user_id, duration_seconds
        FROM sessions {os_filter_sess}
    ),
    fm AS (
        -- Svi mecevi (za avg_matches_per_session)
        SELECT user_id, map_id, outcome
        FROM matches
        WHERE 1=1 {os_filter_match}
    ),
    fmw AS (
        -- Samo mecevi sa poznatim outcome (za win ratio)
        SELECT user_id, map_id, outcome
        FROM matches
        WHERE outcome IS NOT NULL {os_filter_match}
    ),
    sess_agg AS (
        SELECT user_id,
               COUNT(*)               AS n_sessions,
               SUM(duration_seconds)  AS total_playtime
        FROM fs
        GROUP BY user_id
    ),
    match_agg AS (
        SELECT user_id, COUNT(*) AS n_matches
        FROM fm
        GROUP BY user_id
    ),
    win_agg AS (
        SELECT user_id, AVG(outcome) AS total_win_ratio
        FROM fmw
        GROUP BY user_id
    ),
    map_winratio AS (
        SELECT user_id, map_id,
               AVG(outcome) AS map_win_ratio,
               COUNT(*)     AS map_n
        FROM fmw
        GROUP BY user_id, map_id
    ),
    fav_map AS (
        SELECT user_id, map_id, map_win_ratio
        FROM (
            SELECT user_id, map_id, map_win_ratio,
                   ROW_NUMBER() OVER (
                     PARTITION BY user_id
                     ORDER BY map_win_ratio DESC, map_n DESC, map_id
                   ) AS rn
            FROM map_winratio
        )
        WHERE rn = 1
    )
    SELECT
        fu.username,
        fu.country,
        m.map_name                                                    AS fav_map,
        fav.map_win_ratio                                             AS fav_map_win_ratio,
        COALESCE(sa.total_playtime, 0)                                AS total_playtime,
        wa.total_win_ratio                                            AS total_win_ratio,
        CAST(ma.n_matches AS REAL) / NULLIF(sa.n_sessions, 0)         AS avg_matches_per_session,
        fu.registration_date
    FROM fu
    LEFT JOIN sess_agg  sa  ON sa.user_id  = fu.user_id
    LEFT JOIN match_agg ma  ON ma.user_id  = fu.user_id
    LEFT JOIN win_agg   wa  ON wa.user_id  = fu.user_id
    LEFT JOIN fav_map   fav ON fav.user_id = fu.user_id
    LEFT JOIN maps      m   ON m.map_id    = fav.map_id
    ORDER BY total_playtime DESC
    """
    params = (
        user_params
        + os_params_sess
        + os_params_match
        + os_params_match
    )

    con = get_conn()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# /map-stats/{map_name}
# --------------------------------------------------------------------------- #
@app.get("/map-stats/{map_name}")
def map_stats(
    map_name: str,
    date_from: Optional[str] = Query(
        default=None,
        description="Pocetni datum (YYYY-MM-DD), ukljucivo. Opciono.",
    ),
    date_to: Optional[str] = Query(
        default=None,
        description="Krajnji datum (YYYY-MM-DD), ukljucivo. Opciono.",
    ),
):
    """
    Vraca dnevnu statistiku za zadatu mapu.
    Polja: date, avg_playtime, best_player_username, match_cnt.
    best_player_username = igrac sa najvecim KUMULATIVNIM win ratio-om
                           na ovoj mapi do ukljucujuci dati datum.
    """
    for label, d in (("date_from", date_from), ("date_to", date_to)):
        if d:
            try:
                _dt.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, f"{label} mora biti YYYY-MM-DD")

    con = get_conn()
    try:
        if not con.execute(
            "SELECT 1 FROM maps WHERE map_name = ?", (map_name,)
        ).fetchone():
            raise HTTPException(404, f"Mapa '{map_name}' ne postoji")

        date_filter = ""
        params: list = [map_name]
        if date_from:
            date_filter += " AND ms.finish_date >= ?"
            params.append(date_from)
        if date_to:
            date_filter += " AND ms.finish_date <= ?"
            params.append(date_to)

        sql = f"""
        WITH
        target_map AS (
            SELECT map_id FROM maps WHERE map_name = ?
        ),
        match_summary AS (
            SELECT
                match_uuid,
                map_id,
                MIN(start_ts)                      AS start_ts,
                MAX(finish_ts)                     AS finish_ts,
                DATE(MAX(finish_ts), 'unixepoch')  AS finish_date
            FROM matches
            WHERE finish_ts IS NOT NULL
            GROUP BY match_uuid
        ),
        target_matches AS (
            SELECT ms.*
            FROM match_summary ms
            JOIN target_map tm ON ms.map_id = tm.map_id
            WHERE 1=1 {date_filter}
        ),
        daily AS (
            SELECT
                finish_date                        AS date,
                COUNT(*)                           AS match_cnt,
                ROUND(AVG(finish_ts - start_ts))   AS avg_playtime
            FROM target_matches
            GROUP BY finish_date
        ),
        player_matches AS (
            SELECT
                m.user_id,
                DATE(m.finish_ts, 'unixepoch')     AS d,
                m.outcome
            FROM matches m
            JOIN target_map tm ON m.map_id = tm.map_id
            WHERE m.finish_ts IS NOT NULL AND m.outcome IS NOT NULL
        ),
        result_dates AS (SELECT DISTINCT date FROM daily),
        player_cumulative AS (
            SELECT
                rd.date           AS d,
                pm.user_id,
                AVG(pm.outcome)   AS win_ratio,
                COUNT(*)          AS n
            FROM result_dates rd
            JOIN player_matches pm ON pm.d <= rd.date
            GROUP BY rd.date, pm.user_id
        ),
        best_per_date AS (
            SELECT d, user_id, win_ratio, n,
                ROW_NUMBER() OVER (
                    PARTITION BY d
                    ORDER BY win_ratio DESC, n DESC, user_id
                ) AS rn
            FROM player_cumulative
        )
        SELECT
            daily.date,
            daily.avg_playtime,
            u.username  AS best_player_username,
            daily.match_cnt
        FROM daily
        LEFT JOIN best_per_date bp ON bp.d = daily.date AND bp.rn = 1
        LEFT JOIN users u          ON u.user_id = bp.user_id
        ORDER BY daily.date DESC
        """
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# /maps - lista svih mapa (potrebno za chart frontend)
# --------------------------------------------------------------------------- #
@app.get("/maps")
def list_maps():
    """Vraca listu naziva svih dostupnih mapa."""
    con = get_conn()
    try:
        rows = con.execute(
            "SELECT map_name FROM maps ORDER BY map_name"
        ).fetchall()
    finally:
        con.close()
    return [r["map_name"] for r in rows]


# --------------------------------------------------------------------------- #
# /chart - HTML stranica sa line chart-om za poslednjih 7 dana
# --------------------------------------------------------------------------- #
@app.get("/chart")
def chart_page():
    """Servira HTML stranicu koja crta match_cnt po danu za svaku mapu."""
    return FileResponse(STATIC_DIR / "chart.html")