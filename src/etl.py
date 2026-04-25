"""
Golf Rival - ETL pipeline.

Cita events.jsonl + maps.jsonl, validira i deduplicira event-e,
agregira session_pings u sesije, sparuje match_start/match_finish
u meceve, i upisuje sve u SQLite bazu.

Pokretanje:
    python src/etl.py
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ROOT          = Path(__file__).resolve().parent.parent
EVENTS_FILE   = ROOT / "data" / "events.jsonl"
MAPS_FILE     = ROOT / "data" / "maps.jsonl"
DB_FILE       = ROOT / "data" / "golf.db"
SCHEMA_FILE   = ROOT / "src"  / "schema.sql"

VALID_OS      = {"iOS", "Android"}
VALID_STATES  = {"started", "in_progress", "ended"}
VALID_OUTCOME = {0.0, 0.5, 1.0}

SESSION_GAP_SECONDS = 120  # PDF: pingovi >120s razmaka = razlicite sesije


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Korak 1: Ucitavanje + validacija raw event-ova
# --------------------------------------------------------------------------- #
def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def is_valid_event(e: dict, valid_map_ids: set[str]) -> tuple[bool, str]:
    """Vraca (ok, razlog). Razlog je prazan kad je ok=True."""
    # Top-level obavezna polja
    for f in ("id", "timestamp", "event_type", "user_id", "event_data"):
        if e.get(f) is None:
            return False, f"missing_{f}"

    if not isinstance(e["timestamp"], int) or e["timestamp"] <= 0:
        return False, "bad_timestamp"

    et = e["event_type"]
    ed = e["event_data"]
    if not isinstance(ed, dict):
        return False, "event_data_not_dict"

    if et == "registration":
        if not all(ed.get(f) for f in ("country", "device_os", "username")):
            return False, "registration_missing_fields"
        if ed["device_os"] not in VALID_OS:
            return False, "registration_bad_os"

    elif et == "session_ping":
        if ed.get("state") not in VALID_STATES:
            return False, "ping_bad_state"
        if ed.get("device_os") not in VALID_OS:
            return False, "ping_bad_os"

    elif et == "match_start":
        if not ed.get("map_id") or not ed.get("opponent_id"):
            return False, "match_start_missing_fields"
        if ed["map_id"] not in valid_map_ids:
            return False, "match_start_unknown_map"
        if ed["opponent_id"] == e["user_id"]:
            return False, "match_start_self_match"

    elif et == "match_finish":
        if not ed.get("map_id") or not ed.get("opponent_id"):
            return False, "match_finish_missing_fields"
        if ed["map_id"] not in valid_map_ids:
            return False, "match_finish_unknown_map"
        if ed["opponent_id"] == e["user_id"]:
            return False, "match_finish_self_match"
        if ed.get("outcome") not in VALID_OUTCOME:
            return False, "match_finish_bad_outcome"
    else:
        return False, "unknown_event_type"

    return True, ""


def dedupe_by_id(events: list[dict]) -> tuple[list[dict], int]:
    """Cuva najraniji event po id (prema specifikaciji zadatka)."""
    by_id: dict[int, dict] = {}
    for e in events:
        prev = by_id.get(e["id"])
        if prev is None or e["timestamp"] < prev["timestamp"]:
            by_id[e["id"]] = e
    dropped = len(events) - len(by_id)
    return list(by_id.values()), dropped


# --------------------------------------------------------------------------- #
# Korak 2: Sesije (gap-based agregacija)
# --------------------------------------------------------------------------- #
def build_sessions(pings: list[dict]) -> list[dict]:
    """
    Grupise session_pingove u sesije po pravilu razmaka >120s.
    Kolona 'state' je samo informativna; ovo je robusno na nedostajuce
    ili pogresne 'state' vrednosti (resava i bonus zadatak).
    """
    by_user: dict[str, list[dict]] = defaultdict(list)
    for p in pings:
        by_user[p["user_id"]].append(p)

    sessions = []
    for user_id, user_pings in by_user.items():
        user_pings.sort(key=lambda x: x["timestamp"])
        cur = None
        for p in user_pings:
            ts = p["timestamp"]
            os_ = p["event_data"]["device_os"]
            if cur is None or ts - cur["end_ts"] > SESSION_GAP_SECONDS:
                if cur is not None:
                    sessions.append(cur)
                cur = {"user_id": user_id, "device_os": os_,
                       "start_ts": ts, "end_ts": ts}
            else:
                cur["end_ts"] = ts
        if cur is not None:
            sessions.append(cur)

    for s in sessions:
        s["duration_seconds"] = s["end_ts"] - s["start_ts"]
    return sessions


# --------------------------------------------------------------------------- #
# Korak 3: Mecevi (sparivanje match_start + match_finish)
# --------------------------------------------------------------------------- #
def build_matches(
    match_events: list[dict], sessions: list[dict]
) -> tuple[list[dict], int]:
    """
    Grupise match event-e po (kanonski_par_igraca, map_id), zatim deli na
    sekvencijalne cikluse. Novi ciklus pocinje kad match_start dodje nakon
    sto smo vec videli match_finish u tekucem ciklusu.

    Svaki ciklus postaje jedan logicki mec (deljeni match_uuid). U okviru
    ciklusa, emitujemo jedan red po igracu koji ima event-e u ciklusu.
    Svaki red dobija i device_os iz sesije u kojoj je mec odigran
    (NULL ako mec ne pada ni u jednu sesiju tog igraca).

    Po PDF specifikaciji: mec je validan samo ako njegovi event-i kolektivno
    sadrze bar jedan match_start I bar jedan match_finish.
    """
    # Indeks sesija po useru za brzi lookup
    sess_by_user: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        sess_by_user[s["user_id"]].append(s)
    for lst in sess_by_user.values():
        lst.sort(key=lambda x: x["start_ts"])

    def session_os(user_id: str, ts: int | None) -> str | None:
        if ts is None:
            return None
        for s in sess_by_user.get(user_id, ()):
            if s["start_ts"] <= ts <= s["end_ts"]:
                return s["device_os"]
        return None

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for e in match_events:
        a, b = e["user_id"], e["event_data"]["opponent_id"]
        pair = tuple(sorted((a, b)))
        key = (pair, e["event_data"]["map_id"])
        grouped[key].append(e)

    rows = []
    invalid_cycles = 0
    for (pair, map_id), evs in grouped.items():
        evs.sort(key=lambda x: x["timestamp"])
        # Deli na cikluse
        cycles, cur, seen_finish = [], [], False
        for e in evs:
            if e["event_type"] == "match_start" and seen_finish:
                cycles.append(cur)
                cur, seen_finish = [e], False
            else:
                cur.append(e)
                if e["event_type"] == "match_finish":
                    seen_finish = True
        if cur:
            cycles.append(cur)

        for cycle in cycles:
            has_start  = any(x["event_type"] == "match_start"  for x in cycle)
            has_finish = any(x["event_type"] == "match_finish" for x in cycle)
            if not (has_start and has_finish):
                invalid_cycles += 1
                continue

            match_uuid = str(uuid.uuid4())
            players_in_cycle = {x["user_id"] for x in cycle}
            for player in players_in_cycle:
                opp = pair[0] if player == pair[1] else pair[1]
                p_events = [x for x in cycle if x["user_id"] == player]
                starts  = [x for x in p_events if x["event_type"] == "match_start"]
                finishs = [x for x in p_events if x["event_type"] == "match_finish"]
                start_ts  = min((x["timestamp"] for x in starts),  default=None)
                finish_ts = max((x["timestamp"] for x in finishs), default=None)
                # Prvo pokusaj OS po start_ts; ako nije u sesiji, probaj finish_ts
                dev_os = session_os(player, start_ts) or session_os(player, finish_ts)
                rows.append({
                    "match_uuid":  match_uuid,
                    "user_id":     player,
                    "opponent_id": opp,
                    "map_id":      map_id,
                    "start_ts":    start_ts,
                    "finish_ts":   finish_ts,
                    "outcome":     finishs[-1]["event_data"]["outcome"] if finishs else None,
                    "device_os":   dev_os,
                })
    return rows, invalid_cycles


# --------------------------------------------------------------------------- #
# Korak 4: Upis u SQLite
# --------------------------------------------------------------------------- #
def write_db(maps, users, sessions, matches):
    if DB_FILE.exists():
        DB_FILE.unlink()
    con = sqlite3.connect(DB_FILE)
    with SCHEMA_FILE.open(encoding="utf-8") as f:
        con.executescript(f.read())

    con.executemany(
        "INSERT INTO maps(map_id, map_name) VALUES (?, ?)",
        [(m["id"], m["name"]) for m in maps],
    )
    con.executemany(
        "INSERT INTO users(user_id, username, country, registration_os, "
        "registration_ts, registration_date) VALUES (?, ?, ?, ?, ?, ?)",
        [(u["user_id"], u["username"], u["country"], u["registration_os"],
          u["registration_ts"], u["registration_date"]) for u in users],
    )
    con.executemany(
        "INSERT INTO sessions(user_id, device_os, start_ts, end_ts, "
        "duration_seconds) VALUES (?, ?, ?, ?, ?)",
        [(s["user_id"], s["device_os"], s["start_ts"], s["end_ts"],
          s["duration_seconds"]) for s in sessions],
    )
    con.executemany(
        "INSERT INTO matches(match_uuid, user_id, opponent_id, map_id, "
        "start_ts, finish_ts, outcome, device_os) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(m["match_uuid"], m["user_id"], m["opponent_id"], m["map_id"],
          m["start_ts"], m["finish_ts"], m["outcome"], m["device_os"])
         for m in matches],
    )
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    print(f"[ETL] Reading {EVENTS_FILE.name} and {MAPS_FILE.name}")
    raw_events = load_jsonl(EVENTS_FILE)
    raw_maps   = load_jsonl(MAPS_FILE)
    valid_map_ids = {m["id"] for m in raw_maps}
    print(f"   loaded {len(raw_events)} raw events, {len(raw_maps)} maps")

    # ----- Dedup po id (cuva najraniji) -----
    deduped, n_dup = dedupe_by_id(raw_events)
    print(f"[ETL] Removed {n_dup} duplicate id rows")

    # ----- Validacija -----
    valid, dropped = [], Counter()
    for e in deduped:
        ok, reason = is_valid_event(e, valid_map_ids)
        if ok:
            valid.append(e)
        else:
            dropped[reason] += 1
    print(f"[ETL] {len(valid)} valid events; dropped {sum(dropped.values())}:")
    for reason, n in dropped.most_common():
        print(f"        {reason:35s} {n}")

    # ----- Korisnici (registration; cuva najraniji ako ima vise) -----
    regs = sorted([e for e in valid if e["event_type"] == "registration"],
                  key=lambda x: x["timestamp"])
    users_by_id = {}
    for e in regs:
        if e["user_id"] not in users_by_id:
            users_by_id[e["user_id"]] = {
                "user_id": e["user_id"],
                "username": e["event_data"]["username"],
                "country":  e["event_data"]["country"],
                "registration_os": e["event_data"]["device_os"],
                "registration_ts": e["timestamp"],
                "registration_date": ts_to_date(e["timestamp"]),
            }
    users = list(users_by_id.values())
    print(f"[ETL] {len(users)} unique registered users "
          f"({len(regs) - len(users)} duplicate registrations dropped)")

    # ----- Drop event-ova od neregistrovanih korisnika -----
    known = {u["user_id"] for u in users}
    pre = len(valid)
    valid = [e for e in valid if e["user_id"] in known
             or e["event_type"] == "registration"]
    print(f"[ETL] Dropped {pre - len(valid)} events from unknown users")

    # ----- Sesije -----
    pings = [e for e in valid if e["event_type"] == "session_ping"]
    sessions = build_sessions(pings)
    print(f"[ETL] Built {len(sessions)} sessions from {len(pings)} pings")

    # ----- Mecevi -----
    match_events = [e for e in valid
                    if e["event_type"] in ("match_start", "match_finish")]
    matches, n_invalid_cycles = build_matches(match_events, sessions)
    n_unique = len({m['match_uuid'] for m in matches})
    n_no_os  = sum(1 for m in matches if m['device_os'] is None)
    print(f"[ETL] Built {len(matches)} match-rows "
          f"({n_unique} unique matches; "
          f"dropped {n_invalid_cycles} incomplete cycles; "
          f"{n_no_os} rows without session OS)")

    # ----- Upis u bazu -----
    write_db(raw_maps, users, sessions, matches)
    print(f"[ETL] DB written to {DB_FILE}")


if __name__ == "__main__":
    main()