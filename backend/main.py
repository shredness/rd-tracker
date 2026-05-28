from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, os
from datetime import datetime, timedelta
from jose import JWTError, jwt
import bcrypt as _bcrypt

app = FastAPI(title="Agon API")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://agon.savo.us,http://localhost,http://localhost:3800"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH           = os.environ.get("DB_PATH", "/data/sessions.db")
SECRET_KEY        = os.environ.get("SECRET_KEY", "agon-change-this-in-production")
ALGORITHM         = "HS256"
TOKEN_EXPIRE_DAYS = 30

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login")

ADMIN_USER = os.environ.get("ADMIN_USER", "manny")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "pR0m3th3us")
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
            total_vol  = sum(s.get("vol", 0)  for ex in exercises for s in ex.get("sets", []))
            total_time = sum(s.get("time", 0) for ex in exercises for s in ex.get("sets", []) if s.get("time", 0) > 0)
            total_density = round(total_vol / total_time, 2) if total_time > 0 else 0
            rd = round(total_vol / (total_time * bw), 2) if (total_time > 0 and bw) else 0
            conn.execute(
                "UPDATE sessions SET exercises=?, rd=?, total_density=? WHERE id=?",
                (json.dumps(exercises), rd, total_density, r["id"])
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
    if "notes" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN notes TEXT NOT NULL DEFAULT ''")

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS exercises (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            alias      TEXT,
            tool       TEXT NOT NULL DEFAULT 'Bar',
            mult       REAL NOT NULL DEFAULT 2.0,
            muscles    TEXT NOT NULL DEFAULT '[]',
            day        TEXT,
            load_hint  TEXT,
            is_bw      INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Per-user AI settings: encrypted Gemini key + model choice
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id      INTEGER PRIMARY KEY,
            ai_key_enc   TEXT,
            ai_model     TEXT DEFAULT 'gemini-2.0-flash',
            updated_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Persisted Insights conversation history (one row per message)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()

    # Run per-set time migration (only once, gated by a flag in meta table)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    flag = conn.execute("SELECT value FROM meta WHERE key='per_set_time_migration_done'").fetchone()
    if not flag:
        migrate_sessions_to_per_set_time(conn)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ('per_set_time_migration_done', '1'))
        conn.commit()
    conn.close()


init_db()


# ── Auth ──────────────────────────────────────────────────────
def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())

def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


# ── AI key encryption (Fernet, derived from SECRET_KEY) ───────
import base64 as _b64, hashlib as _hashlib
from cryptography.fernet import Fernet, InvalidToken

def _fernet():
    # Derive a stable 32-byte urlsafe key from SECRET_KEY
    digest = _hashlib.sha256(SECRET_KEY.encode()).digest()
    return Fernet(_b64.urlsafe_b64encode(digest))

def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()

def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return ""

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

class ExerciseIn(BaseModel):
    name: str
    alias: Optional[str] = ''
    tool: str = 'Bar'
    mult: float = 2.0
    muscles: list[str] = []
    day: Optional[str] = ''
    load_hint: Optional[str] = ''
    is_bw: bool = False
    sort_order: int = 0

class SessionIn(BaseModel):
    date: str
    bw: float
    rd: float
    total_density: float
    exercises: list[ExerciseData]
    notes: Optional[str] = ''

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "guest"


class AISettingsIn(BaseModel):
    ai_key: Optional[str] = None      # plaintext key from user; None = don't change
    ai_model: Optional[str] = None

class InsightsQuery(BaseModel):
    question: str


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
             "total_density": r["total_density"], "notes": r["notes"] or "",
             "exercises": json.loads(r["exercises"])} for r in rows]

@app.post("/sessions")
def save_session(session: SessionIn, user=Depends(current_user)):
    if user["role"] == "demo":
        raise HTTPException(status_code=403, detail="Demo account is read-only")
    uid = user["id"]

    # Recalculate RD server-side using correct formula:
    # RD = total_session_vol / (total_session_time * bodyweight)
    exercises_out = []
    total_vol  = 0.0
    total_time = 0.0
    for ex in session.exercises:
        ex_vol  = sum(s.vol  for s in ex.sets)
        ex_time = sum(s.time for s in ex.sets if s.time > 0)
        total_vol  += ex_vol
        total_time += ex_time
        exercises_out.append({
            **ex.dict(),
            "totalVol": round(ex_vol, 1),
            "density":  round(ex_vol / ex_time, 2) if ex_time > 0 else 0,
        })

    total_density = round(total_vol / total_time, 2) if total_time > 0 else 0
    rd = round(total_vol / (total_time * session.bw), 2) if (total_time > 0 and session.bw) else 0

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO sessions (user_id, date, bw, rd, total_density, exercises, notes)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                bw=excluded.bw, rd=excluded.rd,
                total_density=excluded.total_density, exercises=excluded.exercises,
                notes=excluded.notes
        """, (uid, session.date, session.bw, rd, round(total_density, 2),
              json.dumps(exercises_out), session.notes or ""))
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

    # Group by week-ending Thursday (Sat-Thu workout week, matching original spreadsheet)
    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # Days to next Thursday: Thu=0, Fri=6, Sat=5, Sun=4, Mon=3, Tue=2, Wed=1
    days_to_thursday = {0:3, 1:2, 2:1, 3:0, 4:6, 5:5, 6:4}

    by_week: dict = {}
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
        except ValueError:
            continue
        delta = days_to_thursday[d.weekday()]
        thu = d + timedelta(days=delta)
        key = f"{thu.month}/{thu.day}/{thu.year}"
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


@app.get("/exercises/bank")
def get_exercise_bank(user=Depends(current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM exercises ORDER BY sort_order, name").fetchall()
    conn.close()
    return [{"id":r["id"],"name":r["name"],"alias":r["alias"],"tool":r["tool"],
             "mult":r["mult"],"muscles":json.loads(r["muscles"]),"day":r["day"],
             "loadHint":r["load_hint"],"isBW":bool(r["is_bw"]),"sortOrder":r["sort_order"]} for r in rows]

@app.post("/exercises/bank")
def add_exercise(body: ExerciseIn, user=Depends(current_user)):
    if user["role"] not in ("admin", "guest"):
        raise HTTPException(status_code=403, detail="Read-only account")
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO exercises (name, alias, tool, mult, muscles, day, load_hint, is_bw, sort_order)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                alias=excluded.alias, tool=excluded.tool, mult=excluded.mult,
                muscles=excluded.muscles, day=excluded.day, load_hint=excluded.load_hint,
                is_bw=excluded.is_bw, sort_order=excluded.sort_order
        """, (body.name, body.alias, body.tool, body.mult, json.dumps(body.muscles),
              body.day, body.load_hint, int(body.is_bw), body.sort_order))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
    conn.close()
    return {"status": "upserted", "name": body.name}

@app.put("/exercises/bank/{ex_name}")
def update_exercise(ex_name: str, body: ExerciseIn, user=Depends(current_user)):
    if user["role"] not in ("admin", "guest"):
        raise HTTPException(status_code=403, detail="Read-only account")
    conn = get_db()
    result = conn.execute(
        "UPDATE exercises SET alias=?, tool=?, mult=?, muscles=?, day=?, load_hint=?, is_bw=?, sort_order=? WHERE name=?",
        (body.alias, body.tool, body.mult, json.dumps(body.muscles),
         body.day, body.load_hint, int(body.is_bw), body.sort_order, ex_name))
    if result.rowcount == 0:
        # Doesn't exist — insert it
        conn.execute(
            "INSERT INTO exercises (name, alias, tool, mult, muscles, day, load_hint, is_bw, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
            (ex_name, body.alias, body.tool, body.mult, json.dumps(body.muscles),
             body.day, body.load_hint, int(body.is_bw), body.sort_order))
    conn.commit()
    conn.close()
    return {"status": "upserted"}

@app.delete("/exercises/bank/{ex_name}")
def delete_exercise(ex_name: str, user=Depends(current_user)):
    if user["role"] not in ("admin", "guest"):
        raise HTTPException(status_code=403, detail="Read-only account")
    conn = get_db()
    conn.execute("DELETE FROM exercises WHERE name=?", (ex_name,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

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

# ── AI Settings endpoints ─────────────────────────────────────
@app.get("/ai/settings")
def get_ai_settings(user=Depends(current_user)):
    """Return whether a key is set + masked preview + model. Never returns the raw key."""
    conn = get_db()
    row = conn.execute("SELECT ai_key_enc, ai_model FROM user_settings WHERE user_id=?",
                       (user["id"],)).fetchone()
    conn.close()
    if not row or not row["ai_key_enc"]:
        return {"has_key": False, "masked": "", "model": (row["ai_model"] if row else "gemini-1.5-flash")}
    plain = decrypt_secret(row["ai_key_enc"])
    masked = ("•" * max(0, len(plain) - 4) + plain[-4:]) if plain else ""
    return {"has_key": bool(plain), "masked": masked, "model": row["ai_model"] or "gemini-1.5-flash"}

@app.post("/ai/settings")
def save_ai_settings(body: AISettingsIn, user=Depends(current_user)):
    if user["role"] == "demo":
        raise HTTPException(status_code=403, detail="Demo accounts cannot store API keys")
    conn = get_db()
    existing = conn.execute("SELECT user_id FROM user_settings WHERE user_id=?",
                            (user["id"],)).fetchone()
    # Build the update — always write both fields atomically
    key_enc = None
    if body.ai_key is not None and body.ai_key.strip():
        key_enc = encrypt_secret(body.ai_key.strip())
    if existing:
        cur = conn.execute("SELECT ai_key_enc, ai_model FROM user_settings WHERE user_id=?",
                           (user["id"],)).fetchone()
        final_key   = key_enc if key_enc is not None else cur["ai_key_enc"]
        final_model = body.ai_model if body.ai_model else (cur["ai_model"] or "gemini-2.0-flash")
        conn.execute(
            "UPDATE user_settings SET ai_key_enc=?, ai_model=?, updated_at=datetime('now') WHERE user_id=?",
            (final_key, final_model, user["id"])
        )
    else:
        conn.execute("INSERT INTO user_settings (user_id, ai_key_enc, ai_model) VALUES (?,?,?)",
                     (user["id"], key_enc, body.ai_model or "gemini-2.0-flash"))
    conn.commit()
    conn.close()
    return {"status": "saved"}

@app.delete("/ai/settings")
def delete_ai_settings(user=Depends(current_user)):
    if user["role"] == "demo":
        raise HTTPException(status_code=403, detail="Demo accounts have no stored key")
    conn = get_db()
    conn.execute("UPDATE user_settings SET ai_key_enc=NULL, updated_at=datetime('now') WHERE user_id=?",
                 (user["id"],))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

# ── Insights: build a compact data context for the LLM ────────
def build_training_context(uid: int) -> str:
    """Produce a compact text summary of the user's training data for the LLM."""
    conn = get_db()
    rows = conn.execute(
        "SELECT date, bw, rd, total_density, exercises, notes FROM sessions WHERE user_id=? ORDER BY date",
        (uid,)
    ).fetchall()
    conn.close()
    if not rows:
        return "No training sessions logged yet."

    lines = []
    lines.append(f"Total sessions: {len(rows)}")
    lines.append(f"Date range: {rows[0]['date']} to {rows[-1]['date']}")
    lines.append("")
    lines.append("RD = Relative Density = total session volume / (total time x bodyweight).")
    lines.append("Higher RD means more work done per unit time per pound of bodyweight.")
    lines.append("")
    lines.append("SESSION LOG (date | bodyweight | RD | exercises[load x reps]):")

    for r in rows:
        try:
            exs = json.loads(r["exercises"])
        except Exception:
            exs = []
        ex_parts = []
        for ex in exs:
            sets = ex.get("sets", [])
            if not sets:
                continue
            # Compact: name maxload xtotalreps
            max_load = max((s.get("trueLbs") or s.get("rawLoad") or 0) for s in sets)
            total_reps = sum(s.get("reps", 0) for s in sets)
            ex_parts.append(f"{ex.get('name','?')} {max_load:g}x{total_reps:g}")
        note = f" | note: {r['notes']}" if r["notes"] else ""
        lines.append(f"{r['date']} | {r['bw']:g}lb | RD {r['rd']:.2f} | {'; '.join(ex_parts)}{note}")

    return "\n".join(lines)


SYSTEM_PROMPT = """You are an analytical strength-training assistant inside an app called Agon.
The user tracks workouts using a metric called RD (Relative Density). You have access to their
full session history below. Answer their questions directly and concisely, grounding every claim
in the actual data. Use specific dates, loads, and numbers. If the data doesn't support an answer,
say so plainly. Do not invent data. Keep answers tight and useful — this is a knowledgeable user
who wants signal, not filler. When discussing trends, cite the specific sessions that show them."""


def _is_claude_model(model: str) -> bool:
    return model.startswith("claude-")

async def call_llm(api_key: str, model: str, context: str, history: list, question: str) -> str:
    """Route to Gemini or Anthropic depending on the model name."""
    import httpx
    if _is_claude_model(model):
        return await _call_anthropic(api_key, model, context, history, question)
    return await _call_gemini(api_key, model, context, history, question)

async def _call_gemini(api_key: str, model: str, context: str, history: list, question: str) -> str:
    """Call the Gemini API with training context + conversation history."""
    import httpx
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent"
    contents = []
    contents.append({
        "role": "user",
        "parts": [{"text": f"{SYSTEM_PROMPT}\n\n=== TRAINING DATA ===\n{context}\n\n=== END DATA ===\n\nAcknowledge you have the data and are ready."}]
    })
    contents.append({
        "role": "model",
        "parts": [{"text": "I have your full training history loaded and I'm ready to analyze it. What would you like to know?"}]
    })
    for msg in history:
        contents.append({
            "role": "user" if msg["role"] == "user" else "model",
            "parts": [{"text": msg["content"]}]
        })
    contents.append({"role": "user", "parts": [{"text": question}]})
    payload = {"contents": contents}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, params={"key": api_key}, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Gemini API error ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise HTTPException(status_code=502, detail="Gemini returned an unexpected response shape")

async def _call_anthropic(api_key: str, model: str, context: str, history: list, question: str) -> str:
    """Call the Anthropic Messages API with training context + conversation history."""
    import httpx
    url = "https://api.anthropic.com/v1/messages"
    messages = []
    # Prior conversation turns
    for msg in history:
        messages.append({
            "role": "user" if msg["role"] == "user" else "assistant",
            "content": msg["content"]
        })
    messages.append({"role": "user", "content": question})
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": f"{SYSTEM_PROMPT}\n\n=== TRAINING DATA ===\n{context}\n\n=== END DATA ===",
        "messages": messages
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Anthropic API error ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError):
            raise HTTPException(status_code=502, detail="Anthropic returned an unexpected response shape")


@app.get("/insights/history")
def get_insights_history(user=Depends(current_user)):
    """Return persisted conversation. Demo gets nothing (shared identity)."""
    if user["role"] == "demo":
        return {"messages": []}
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, created_at FROM insights_messages WHERE user_id=? ORDER BY id",
        (user["id"],)
    ).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}

@app.delete("/insights/history")
def clear_insights_history(user=Depends(current_user)):
    if user["role"] == "demo":
        return {"status": "noop"}
    conn = get_db()
    conn.execute("DELETE FROM insights_messages WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    return {"status": "cleared"}

@app.post("/insights/ask")
async def insights_ask(body: InsightsQuery, user=Depends(current_user)):
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty question")

    # Get the user's key + model
    conn = get_db()
    settings = conn.execute("SELECT ai_key_enc, ai_model FROM user_settings WHERE user_id=?",
                            (user["id"],)).fetchone()
    conn.close()
    if not settings or not settings["ai_key_enc"]:
        raise HTTPException(status_code=400, detail="No API key configured. Add one in Settings.")
    api_key = decrypt_secret(settings["ai_key_enc"])
    if not api_key:
        raise HTTPException(status_code=400, detail="Stored key could not be decrypted. Re-enter it in Settings.")
    model = settings["ai_model"] or "gemini-2.0-flash"

    # Build context from the data the user is allowed to see
    uid = data_user_id(user)
    context = build_training_context(uid)

    # Load prior conversation (demo has none)
    history = []
    if user["role"] != "demo":
        conn = get_db()
        rows = conn.execute(
            "SELECT role, content FROM insights_messages WHERE user_id=? ORDER BY id",
            (user["id"],)
        ).fetchall()
        conn.close()
        history = [dict(r) for r in rows]

    answer = await call_llm(api_key, model, context, history, q)

    # Persist both turns (not for demo)
    if user["role"] != "demo":
        conn = get_db()
        conn.execute("INSERT INTO insights_messages (user_id, role, content) VALUES (?,?,?)",
                     (user["id"], "user", q))
        conn.execute("INSERT INTO insights_messages (user_id, role, content) VALUES (?,?,?)",
                     (user["id"], "model", answer))
        conn.commit()
        conn.close()

    return {"answer": answer}

@app.post("/ai/test")
async def test_ai_key(body: AISettingsIn, user=Depends(current_user)):
    """Test a Gemini key with a trivial call. Uses provided key, or stored key if none given."""
    import httpx
    key = (body.ai_key or "").strip()
    model = body.ai_model or "gemini-1.5-flash"
    if not key:
        conn = get_db()
        row = conn.execute("SELECT ai_key_enc FROM user_settings WHERE user_id=?",
                           (user["id"],)).fetchone()
        conn.close()
        if row and row["ai_key_enc"]:
            key = decrypt_secret(row["ai_key_enc"])
    if not key:
        raise HTTPException(status_code=400, detail="No key to test")

    try:
        import httpx
        if _is_claude_model(model):
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            payload = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "Reply OK"}]}
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=payload)
        else:
            url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent"
            payload = {"contents": [{"role": "user", "parts": [{"text": "Reply with just: OK"}]}]}
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, params={"key": key}, json=payload)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Connection failed: {str(e)[:200]}")
    if resp.status_code == 200:
        return {"ok": True, "model": model}
    raise HTTPException(status_code=400, detail=f"Key test failed ({resp.status_code}): {resp.text[:200]}")
