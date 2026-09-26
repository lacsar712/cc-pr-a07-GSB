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


def now():
    return datetime.now(timezone.utc)


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
    created_at timestamptz NOT NULL,
    rinse_token_id integer
);

CREATE TABLE IF NOT EXISTS rinse_tokens (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    token_date date NOT NULL,
    status text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    used_at timestamptz,
    used_by text,
    voided_at timestamptz,
    voided_by text,
    void_reason text NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS rinse_tokens_one_active_per_day
    ON rinse_tokens (token_date) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS rinse_token_events (
    id serial PRIMARY KEY,
    token_id integer NOT NULL,
    code text NOT NULL,
    action text NOT NULL,
    detail text NOT NULL DEFAULT '',
    actor text NOT NULL,
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
    rinse_token: str


class VoidIn(BaseModel):
    reason: str = ""


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


def token_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "token_date": str(row["token_date"]),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "used_at": row.get("used_at"),
        "used_by": row.get("used_by"),
        "voided_at": row.get("voided_at"),
        "voided_by": row.get("voided_by"),
        "void_reason": row.get("void_reason", ""),
    }


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rinse_token_id integer")
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            ts = now()
            conn.execute(
                """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by, created_at)
                   VALUES
                   ('封面-01', 0.05, -0.04, 'pending', '', '', 'printer', %s),
                   ('内页-09', 0.40, 0.02, 'pending', '', '', 'printer', %s)""",
                (ts, ts),
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


@app.post("/api/jobs", status_code=202)
def enqueue(body: JobIn, user: dict = Depends(require_writer)):
    code = body.rinse_token.strip()
    if not code:
        raise HTTPException(status_code=400, detail="投递必须填写当日墨路冲洗口令")
    ts = now()
    with connect() as conn:
        try:
            with conn.transaction():
                consumed = conn.execute(
                    """UPDATE rinse_tokens
                       SET status = 'used', used_at = %s, used_by = %s
                       WHERE code = %s AND status = 'active' AND token_date = CURRENT_DATE
                       RETURNING id""",
                    (ts, user["username"], code),
                ).fetchone()
                if consumed is None:
                    existing = conn.execute(
                        "SELECT id, status, token_date FROM rinse_tokens WHERE code = %s",
                        (code,),
                    ).fetchone()
                    if existing is None:
                        raise HTTPException(status_code=400, detail="口令错误，投递退回")
                    if str(existing["token_date"]) != str(ts.date()):
                        raise HTTPException(status_code=400, detail="隔日旧令，投递退回")
                    if existing["status"] == "used":
                        raise HTTPException(status_code=400, detail="口令已使用，不可再次投递")
                    raise HTTPException(status_code=400, detail="口令已作废，投递退回")

                row = conn.execute(
                    """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, created_by, created_at, rinse_token_id)
                       VALUES (%s, %s, %s, 'pending', %s, %s, %s)
                       RETURNING id, sheet, status, verdict""",
                    (body.sheet.strip(), body.cyan_mm, body.magenta_mm,
                     user["username"], ts, consumed["id"]),
                ).fetchone()
                conn.execute(
                    """INSERT INTO rinse_token_events (token_id, code, action, detail, actor, created_at)
                       VALUES (%s, %s, 'consumed', %s, %s, %s)""",
                    (consumed["id"], code, f"投递入队 #{row['id']}（{body.sheet.strip()}）",
                     user["username"], ts),
                )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=400, detail="口令状态异常，投递退回")
        conn.commit()
    return row


@app.get("/api/rinse-tokens")
def list_rinse_tokens(_user: dict = Depends(current_user)):
    with connect() as conn:
        active = conn.execute(
            """SELECT id, code, token_date, status, created_by, created_at,
                      used_at, used_by, voided_at, voided_by, void_reason
               FROM rinse_tokens
               WHERE status = 'active' AND token_date = CURRENT_DATE
               ORDER BY id DESC"""
        ).fetchall()
        records = conn.execute(
            """SELECT id, code, token_date, status, created_by, created_at,
                      used_at, used_by, voided_at, voided_by, void_reason,
                      CASE
                        WHEN status = 'voided' THEN 'voided'
                        WHEN status = 'used' THEN 'used'
                        WHEN status = 'active' AND token_date < CURRENT_DATE THEN 'expired'
                        ELSE status
                      END AS state
               FROM rinse_tokens
               WHERE status <> 'active' OR token_date <> CURRENT_DATE
               ORDER BY id DESC"""
        ).fetchall()
        events = conn.execute(
            """SELECT id, token_id, code, action, detail, actor, created_at
               FROM rinse_token_events ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    return {
        "active": [token_payload(r) for r in active],
        "records": [{**token_payload(r), "state": r["state"]} for r in records],
        "events": events,
    }


@app.post("/api/rinse-tokens/generate", status_code=201)
def generate_rinse_token(user: dict = Depends(require_writer)):
    ts = now()
    code = f"MR{ts:%Y%m%d}-{secrets.token_hex(3).upper()}"
    with connect() as conn:
        try:
            with conn.transaction():
                stale = conn.execute(
                    """UPDATE rinse_tokens SET status = 'expired'
                       WHERE status = 'active' AND token_date < CURRENT_DATE
                       RETURNING id, code"""
                ).fetchall()
                for s in stale:
                    conn.execute(
                        """INSERT INTO rinse_token_events (token_id, code, action, detail, actor, created_at)
                           VALUES (%s, %s, 'expired', '隔日未用，自动失效', %s, %s)""",
                        (s["id"], s["code"], user["username"], ts),
                    )
                dup = conn.execute(
                    """SELECT id FROM rinse_tokens
                       WHERE status = 'active' AND token_date = CURRENT_DATE
                       FOR UPDATE"""
                ).fetchone()
                if dup is not None:
                    raise HTTPException(status_code=409, detail="今日已有有效口令，请先作废再重新生成")
                row = conn.execute(
                    """INSERT INTO rinse_tokens (code, token_date, status, created_by, created_at)
                       VALUES (%s, CURRENT_DATE, 'active', %s, %s)
                       RETURNING id, code, token_date, status, created_by, created_at,
                                 used_at, used_by, voided_at, voided_by, void_reason""",
                    (code, user["username"], ts),
                ).fetchone()
                conn.execute(
                    """INSERT INTO rinse_token_events (token_id, code, action, detail, actor, created_at)
                       VALUES (%s, %s, 'generated', '生成当日冲洗口令', %s, %s)""",
                    (row["id"], code, user["username"], ts),
                )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="今日已有有效口令，请先作废再重新生成")
        conn.commit()
    return token_payload(row)


@app.post("/api/rinse-tokens/{token_id}/void")
def void_rinse_token(token_id: int, body: VoidIn, user: dict = Depends(require_writer)):
    ts = now()
    with connect() as conn:
        with conn.transaction():
            row = conn.execute(
                """UPDATE rinse_tokens
                   SET status = 'voided', voided_at = %s, voided_by = %s, void_reason = %s
                   WHERE id = %s AND status = 'active'
                   RETURNING id, code""",
                (ts, user["username"], body.reason.strip(), token_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=409, detail="口令不在有效状态，无法作废")
            conn.execute(
                """INSERT INTO rinse_token_events (token_id, code, action, detail, actor, created_at)
                   VALUES (%s, %s, 'voided', %s, %s, %s)""",
                (row["id"], row["code"], body.reason.strip() or "手动作废", user["username"], ts),
            )
        conn.commit()
    return {"id": token_id, "status": "voided"}
