import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54394/printreg")
SECRET = os.environ.get("JWT_SECRET", "print-register-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "printer": {"role": "writer", "password_hash": pwd.hash("print123456")},
    "checker": {"role": "reader", "password_hash": pwd.hash("check123456")},
}


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    sheet text NOT NULL,
    cyan_mm double precision NOT NULL,
    magenta_mm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS flush_tokens (
    id serial PRIMARY KEY,
    token text NOT NULL UNIQUE,
    valid_date date NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    used_by text,
    used_at timestamptz,
    job_id integer
);
CREATE TABLE IF NOT EXISTS flush_token_events (
    id serial PRIMARY KEY,
    token_id integer NOT NULL REFERENCES flush_tokens(id),
    event text NOT NULL,
    actor text NOT NULL,
    job_id integer,
    created_at timestamptz NOT NULL
);
"""


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    sheet: str
    cyan_mm: float
    magenta_mm: float
    flush_token: str


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="仅印刷员可操作")
    return user


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by, created_at)
                   VALUES
                   ('封面-01', 0.05, -0.04, 'pending', '', '', 'printer', %s),
                   ('内页-09', 0.40, 0.02, 'pending', '', '', 'printer', %s)""",
                (now, now),
            )
        conn.commit()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "print-register-review"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode({"sub": body.username.strip(), "role": user["role"], "exp": exp}, SECRET, algorithm="HS256")
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/jobs")
def list_jobs(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            "SELECT id, sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()


@app.post("/api/flush-tokens", status_code=201)
def generate_flush_token(user: dict = Depends(require_writer)):
    now = datetime.now(timezone.utc)
    token = f"FL{now:%Y%m%d}-{secrets.token_hex(3).upper()}"
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO flush_tokens (token, valid_date, status, created_by, created_at)
               VALUES (%s, %s, 'active', %s, %s)
               RETURNING id, token, valid_date, status, created_by, created_at""",
            (token, now.date(), user["username"], now),
        ).fetchone()
        conn.execute(
            """INSERT INTO flush_token_events (token_id, event, actor, job_id, created_at)
               VALUES (%s, 'generated', %s, NULL, %s)""",
            (row["id"], user["username"], now),
        )
        conn.commit()
    return row


@app.get("/api/flush-tokens")
def list_flush_tokens(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            """SELECT id, token, valid_date, status, created_by, created_at, used_by, used_at, job_id
               FROM flush_tokens ORDER BY id DESC"""
        ).fetchall()


@app.get("/api/flush-tokens/events")
def list_flush_token_events(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            """SELECT e.id, e.event, e.actor, e.job_id, e.created_at, t.token
               FROM flush_token_events e
               JOIN flush_tokens t ON t.id = e.token_id
               ORDER BY e.id DESC"""
        ).fetchall()


@app.post("/api/jobs", status_code=202)
def enqueue(body: JobIn, user: dict = Depends(require_writer)):
    now = datetime.now(timezone.utc)
    with connect() as conn:
        token_row = conn.execute(
            "SELECT id, status, valid_date FROM flush_tokens WHERE token = %s FOR UPDATE",
            (body.flush_token.strip(),),
        ).fetchone()
        if token_row is None:
            raise HTTPException(status_code=400, detail="冲洗口令错误，已退回")
        if token_row["status"] != "active":
            raise HTTPException(status_code=400, detail="冲洗口令已作废，已退回")
        if token_row["valid_date"] < now.date():
            raise HTTPException(status_code=400, detail="冲洗口令非当日有效，已退回")
        row = conn.execute(
            """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, created_by, created_at)
               VALUES (%s, %s, %s, 'pending', %s, %s)
               RETURNING id, sheet, status, verdict""",
            (body.sheet.strip(), body.cyan_mm, body.magenta_mm, user["username"], now),
        ).fetchone()
        conn.execute(
            """UPDATE flush_tokens
               SET status = 'used', used_by = %s, used_at = %s, job_id = %s
               WHERE id = %s""",
            (user["username"], now, row["id"], token_row["id"]),
        )
        conn.execute(
            """INSERT INTO flush_token_events (token_id, event, actor, job_id, created_at)
               VALUES (%s, 'used', %s, %s, %s)""",
            (token_row["id"], user["username"], row["id"], now),
        )
        conn.commit()
    return row
