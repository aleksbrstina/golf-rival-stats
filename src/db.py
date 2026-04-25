"""Mali helper za SQLite konekciju."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "golf.db"

def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError(
            f"Baza ne postoji: {DB_PATH}. Pokreni prvo: python src/etl.py"
        )
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con