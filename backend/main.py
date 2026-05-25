from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, os
from datetime import datetime

app = FastAPI(title="RD Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", "/data/sessions.db")

SEED_WEEKLY = [
    {"week": "12/4/2025",  "rd": 1.70, "wt": 219},
    {"week": "12/11/2025", "rd": 2.04, "wt": 215},
    {"week": "12/18/2025", "rd": 2.17, "wt": 215},
    {"week": "12/25/2025", "rd": 1.97, "wt": 214},
    {"week": "1/8/2026",   "rd": 1.81, "wt": 212},
    {"week": "1/15/2026",  "rd": 2.25, "wt": 211},
    {"week": "1/22/2026",  "rd": 2.30, "wt": 210},
    {"week": "1/29/2026",  "rd": 2.35, "wt": 210},
    {"week": "2/5/2026",   "rd": 2.37, "wt": 209},
    {"week": "2/12/2026",  "rd": 2.57, "wt": 207},
    {"week": "2/19/2026",  "rd": 2.62, "wt": 206},
    {"week": "2/26/2026",  "rd": 2.69, "wt": 205},
    {"week": "3/5/2026",   "rd": 2.77, "wt": 203},
    {"week": "3/12/2026",  "rd": 2.73, "wt": 203},
    {"week": "3/19/2026",  "rd": 2.39, "wt": 202},
    {"week": "3/26/2026",  "rd": 2.59, "wt": 201},
    {"week": "4/2/2026",   "rd": 2.77, "wt": 201},
    {"week": "4/9/2026",   "rd": 2.82, "wt": 202},
    {"week": "4/16/2026",  "rd": 2.91, "wt": 202},
    {"week": "4/23/2026",  "rd": 2.37, "wt": 200},
    {"week": "4/30/2026",  "rd": 2.63, "wt": 200},
    {"week": "5/7/2026",   "rd": 2.69, "wt": 199},
    {"week": "5/14/2026",  "rd": 2.95, "wt": 199},
    {"week": "5/21/2026",  "rd": 2.89, "wt": 199},
    {"week": "5/28/2026",  "rd": 2.79, "wt": 200},
]


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            bw REAL NOT NULL,
            rd REAL NOT NULL,
            total_density REAL NOT NULL,
            exercises TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


init_db()


class SetData(BaseModel):
    reps: float
    rawLoad: float
    trueLbs: float
    vol: float


class ExerciseData(BaseModel):
    name: str
    time: float
    sets: list[SetData]
    totalVol: float
    density: float
    tool: Optional[str] = "Bar"
    mult: Optional[float] = 2.0
    isBW: Optional[bool] = False


class SessionIn(BaseModel):
    date: str
    bw: float
    rd: float
    total_density: float
    exercises: list[ExerciseData]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sessions")
def get_sessions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sessions ORDER BY date ASC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "date": r["date"],
            "bw": r["bw"],
            "rd": r["rd"],
            "total_density": r["total_density"],
            "exercises": json.loads(r["exercises"]),
        }
        for r in rows
    ]


@app.post("/sessions")
def save_session(session: SessionIn):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO sessions (date, bw, rd, total_density, exercises)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                bw=excluded.bw,
                rd=excluded.rd,
                total_density=excluded.total_density,
                exercises=excluded.exercises
            """,
            (
                session.date,
                session.bw,
                session.rd,
                session.total_density,
                json.dumps([e.dict() for e in session.exercises]),
            ),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))
    conn.close()
    return {"status": "saved", "date": session.date}


@app.delete("/sessions/{date}")
def delete_session(date: str):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE date = ?", (date,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "date": date}


@app.get("/trend")
def get_trend():
    conn = get_db()
    rows = conn.execute("SELECT date, bw, rd FROM sessions ORDER BY date ASC").fetchall()
    conn.close()

    by_week: dict = {}
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
        except ValueError:
            continue
        days_to_sunday = (6 - d.weekday()) % 7
        from datetime import timedelta
        sun = d + timedelta(days=days_to_sunday)
        key = f"{sun.month}/{sun.day}/{sun.year}"
        if key not in by_week:
            by_week[key] = []
        by_week[key].append({"rd": r["rd"], "wt": r["bw"]})

    merged = list(SEED_WEEKLY)
    for week, entries in by_week.items():
        avg_rd = sum(e["rd"] for e in entries) / len(entries)
        avg_wt = sum(e["wt"] for e in entries) / len(entries)
        existing = next((i for i, w in enumerate(merged) if w["week"] == week), None)
        if existing is not None:
            merged[existing] = {
                "week": week,
                "rd": round((merged[existing]["rd"] + avg_rd) / 2, 2),
                "wt": round((merged[existing]["wt"] + avg_wt) / 2, 1),
            }
        else:
            merged.append({"week": week, "rd": round(avg_rd, 2), "wt": round(avg_wt, 1)})

    def sort_key(w):
        try:
            return datetime.strptime(w["week"], "%m/%d/%Y")
        except ValueError:
            return datetime.min

    merged.sort(key=sort_key)
    return merged
