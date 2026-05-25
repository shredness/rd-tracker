from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, os
from datetime import datetime, timedelta
from jose import JWTError, jwt
import bcrypt as _bcrypt

app = FastAPI(title="Prometheus API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH           = os.environ.get("DB_PATH", "/data/sessions.db")
SECRET_KEY        = os.environ.get("SECRET_KEY", "prometheus-change-this-in-production")
ALGORITHM         = "HS256"
TOKEN_EXPIRE_DAYS = 30

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login")

ADMIN_USER = "manny"
ADMIN_PASS = "pR0m3th3us"
DEMO_USER  = "demo"
DEMO_PASS  = "demo"


# ── DB ────────────────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_sessions_to_per_set_time(conn):
    """
    One-time migration: move ex.time down to each set as set.time.
    After migration, ex.time is removed (density recalculated from sets).
    Idempotent — skips sets that already have a time field.
    """
    rows = conn.execute("SELECT id, exercises FROM sessions").fetchall()
    updated = 0
    for r in rows:
        exercises = json.loads(r["exercises"])
        changed = False
        for ex in exercises:
            ex_time = ex.get("time", 10)
            new_sets = []
            for s in ex.get("sets", []):
                if "time" not in s:
                    s["time"] = ex_time
                    changed = True
                new_sets.append(s)
            ex["sets"] = new_sets
            # Recalculate density from per-set data
            if changed:
                total_density = sum(
                    (s.get("vol", 0) / s.get("time", 1))
                    for s in new_sets if s.get("time", 0) > 0
                )
                ex["density"] = round(total_density, 2)
                ex["totalVol"] = round(sum(s.get("vol", 0) for s in new_sets), 1)
        if changed:
            # Recalculate session RD
            bw = conn.execute("SELECT bw FROM sessions WHERE id=?", (r["id"],)).fetchone()["bw"]
            total_ex_density = sum(ex.get("density", 0) for ex in exercises)
            rd = round(total_ex_density / bw, 2) if bw else 0
            conn.execute(
                "UPDATE sessions SET exercises=?, rd=?, total_density=? WHERE id=?",
                (json.dumps(exercises), rd, round(total_ex_density, 2), r["id"])
            )
            updated += 1
    if updated:
        conn.commit()
        print(f"Migrated {updated} sessions to per-set time model")


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL UNIQUE,
            hashed_pw  TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'guest',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL DEFAULT 1,
            date          TEXT NOT NULL,
            bw            REAL NOT NULL,
            rd            REAL NOT NULL,
            total_density REAL NOT NULL,
            exercises     TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, date)
        )
    """)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "user_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")

    conn.commit()

    # Seed admin
    if not conn.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone():
        conn.execute(
            "INSERT INTO users (username, hashed_pw, role) VALUES (?,?,?)",
            (ADMIN_USER, _bcrypt.hashpw(ADMIN_PASS.encode(), _bcrypt.gensalt()).decode(), "admin")
        )
    # Seed demo
    if not conn.execute("SELECT id FROM users WHERE username=?", (DEMO_USER,)).fetchone():
        conn.execute(
            "INSERT INTO users (username, hashed_pw, role) VALUES (?,?,?)",
            (DEMO_USER, _bcrypt.hashpw(DEMO_PASS.encode(), _bcrypt.gensalt()).decode(), "demo")
        )

    conn.commit()

    # Run per-set time migration
    migrate_sessions_to_per_set_time(conn)
    conn.close()


init_db()


# ── Auth ──────────────────────────────────────────────────────
def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())

def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()

def create_token(data: dict):
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_user(conn, username: str):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

async def current_user(token: str = Depends(oauth2)):
    err = HTTPException(status_code=401, detail="Invalid or expired token",
                        headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise err
    except JWTError:
        raise err
    conn = get_db()
    user = get_user(conn, username)
    conn.close()
    if not user:
        raise err
    return dict(user)

async def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

def data_user_id(user: dict) -> int:
    if user["role"] == "demo":
        conn = get_db()
        admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        conn.close()
        return admin["id"] if admin else 1
    return user["id"]


# ── Models ────────────────────────────────────────────────────
class SetData(BaseModel):
    reps: float
    rawLoad: float
    trueLbs: float
    vol: float
    time: float = 2.0          # per-set time in minutes

class ExerciseData(BaseModel):
    name: str
    sets: list[SetData]
    totalVol: float
    density: float
    tool: Optional[str] = "Bar"
    mult: Optional[float] = 2.0
    isBW: Optional[bool] = False
    time: Optional[float] = None  # kept for legacy compat, ignored in calc

class SessionIn(BaseModel):
    date: str
    bw: float
    rd: float
    total_density: float
    exercises: list[ExerciseData]

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "guest"


# ── Auth endpoints ────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = get_db()
    user = get_user(conn, form.username)
    conn.close()
    if not user or not verify_password(form.password, user["hashed_pw"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = create_token({"sub": user["username"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer",
            "role": user["role"], "username": user["username"]}

@app.get("/auth/me")
def me(user=Depends(current_user)):
    return {"username": user["username"], "role": user["role"], "id": user["id"]}


# ── Admin ─────────────────────────────────────────────────────
@app.get("/admin/users")
def list_users(user=Depends(admin_only)):
    conn = get_db()
    rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/admin/users")
def create_user(body: UserCreate, user=Depends(admin_only)):
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE username=?", (body.username,)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
    conn.execute("INSERT INTO users (username, hashed_pw, role) VALUES (?,?,?)",
                 (body.username, hash_password(body.password), body.role))
    conn.commit()
    conn.close()
    return {"status": "created", "username": body.username}

@app.delete("/admin/users/{username}")
def delete_user(username: str, user=Depends(admin_only)):
    if username in (ADMIN_USER, DEMO_USER):
        raise HTTPException(status_code=400, detail="Cannot delete system accounts")
    conn = get_db()
    conn.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "username": username}

@app.put("/admin/users/{username}/password")
def change_password(username: str, body: dict, user=Depends(admin_only)):
    new_pw = body.get("password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    conn = get_db()
    conn.execute("UPDATE users SET hashed_pw=? WHERE username=?",
                 (hash_password(new_pw), username))
    conn.commit()
    conn.close()
    return {"status": "updated"}


# ── Sessions ──────────────────────────────────────────────────
@app.get("/sessions")
def get_sessions(user=Depends(current_user)):
    uid = data_user_id(user)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_id=? ORDER BY date ASC", (uid,)
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "date": r["date"], "bw": r["bw"], "rd": r["rd"],
             "total_density": r["total_density"],
             "exercises": json.loads(r["exercises"])} for r in rows]

@app.post("/sessions")
def save_session(session: SessionIn, user=Depends(current_user)):
    if user["role"] == "demo":
        raise HTTPException(status_code=403, detail="Demo account is read-only")
    uid = user["id"]

    # Recalculate density and RD server-side from per-set time
    exercises_out = []
    total_density = 0.0
    for ex in session.exercises:
        set_density = sum(
            (s.vol / s.time) for s in ex.sets if s.time > 0
        )
        total_density += set_density
        exercises_out.append({
            **ex.dict(),
            "density": round(set_density, 2),
            "totalVol": round(sum(s.vol for s in ex.sets), 1),
        })

    rd = round(total_density / session.bw, 2) if session.bw else 0

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO sessions (user_id, date, bw, rd, total_density, exercises)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                bw=excluded.bw, rd=excluded.rd,
                total_density=excluded.total_density, exercises=excluded.exercises
        """, (uid, session.date, session.bw, rd, round(total_density, 2),
              json.dumps(exercises_out)))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))
    conn.close()
    return {"status": "saved", "date": session.date, "rd": rd}

@app.delete("/sessions/{date}")
def delete_session(date: str, user=Depends(current_user)):
    if user["role"] == "demo":
        raise HTTPException(status_code=403, detail="Demo account is read-only")
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE user_id=? AND date=?", (user["id"], date))
    conn.commit()
    conn.close()
    return {"status": "deleted", "date": date}


# ── Trend ─────────────────────────────────────────────────────
@app.get("/trend")
def get_trend(user=Depends(current_user)):
    uid = data_user_id(user)
    conn = get_db()
    rows = conn.execute(
        "SELECT date, bw, rd FROM sessions WHERE user_id=? ORDER BY date ASC", (uid,)
    ).fetchall()
    conn.close()

    by_week: dict = {}
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
        except ValueError:
            continue
        days_to_sunday = (6 - d.weekday()) % 7
        sun = d + timedelta(days=days_to_sunday)
        key = f"{sun.month}/{sun.day}/{sun.year}"
        if key not in by_week:
            by_week[key] = []
        by_week[key].append({"rd": r["rd"], "wt": r["bw"]})

    result = []
    for week, entries in by_week.items():
        result.append({
            "week": week,
            "rd":   round(sum(e["rd"] for e in entries) / len(entries), 2),
            "wt":   round(sum(e["wt"] for e in entries) / len(entries), 1),
        })
    result.sort(key=lambda w: datetime.strptime(w["week"], "%m/%d/%Y"))
    return result


# ── Progress ──────────────────────────────────────────────────
@app.get("/progress/{exercise_name}")
def get_progress(exercise_name: str, user=Depends(current_user)):
    uid = data_user_id(user)
    conn = get_db()
    rows = conn.execute(
        "SELECT date, bw, rd, exercises FROM sessions WHERE user_id=? ORDER BY date ASC", (uid,)
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        exercises = json.loads(r["exercises"])
        match = next((e for e in exercises
                      if e.get("name","").lower() == exercise_name.lower()), None)
        if not match:
            continue
        sets = match.get("sets", [])
        if not sets:
            continue
        top_set  = max(sets, key=lambda s: s.get("trueLbs", 0))
        total_vol = sum(s.get("vol", 0) for s in sets)
        result.append({
            "date":     r["date"],
            "bw":       r["bw"],
            "rd":       r["rd"],
            "topLoad":  round(top_set.get("trueLbs", 0), 1),
            "topReps":  int(top_set.get("reps", 0)),
            "totalVol": round(total_vol, 1),
            "density":  round(match.get("density", 0), 2),
            "sets":     len(sets),
        })
    return result


@app.get("/exercises/logged")
def get_logged_exercises(user=Depends(current_user)):
    uid = data_user_id(user)
    conn = get_db()
    rows = conn.execute(
        "SELECT exercises FROM sessions WHERE user_id=?", (uid,)
    ).fetchall()
    conn.close()
    names = set()
    for r in rows:
        for ex in json.loads(r["exercises"]):
            if ex.get("name"):
                names.add(ex["name"])
    return sorted(names)
