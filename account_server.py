#!/usr/bin/env python3
"""选股雷达 VIP 账户服务
端口 5060，独立 systemd 服务。
路由前缀 /account-api/*，避开 nginx 的 /auth/ 和 /api/ 冲突。

端点：
  POST /account-api/register  {username, password}      注册
  POST /account-api/login     {username, password}      登录（设 cookie）
  POST /account-api/logout                            登出
  GET  /account-api/me                                当前用户信息
  POST /account-api/redeem    {code}                   兑换码升级
  GET  /account-api/admin/stats                       管理统计（需 admin session）
"""
import os, json, sqlite3, time, secrets, re
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from flask import Flask, request, jsonify, session, make_response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("ACCOUNT_SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=7)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

DB_PATH = Path(__file__).resolve().parent / "users.db"
ADMIN_USERNAME = os.environ.get("ACCOUNT_ADMIN", "walle")
# 登录限速：IP → [timestamp,...]
_login_attempts = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # 秒
RATE_LIMIT_MAX = 5      # 次数


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT 'normal',
        tier_expires TEXT,
        created_at TEXT NOT NULL,
        redeemed_codes TEXT DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        tier TEXT NOT NULL,
        valid_days INTEGER NOT NULL DEFAULT 0,
        used_by TEXT,
        used_at TEXT,
        batch_note TEXT,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def rate_limited(ip):
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    return len(_login_attempts[ip]) >= RATE_LIMIT_MAX


def record_attempt(ip):
    _login_attempts[ip].append(time.time())


def current_user(conn=None):
    uid = session.get("uid")
    if not uid:
        return None
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if own_conn:
        conn.close()
    if not row:
        return None
    # 检查等级是否过期
    user = dict(row)
    if user["tier"] != "normal" and user.get("tier_expires"):
        try:
            expires = datetime.fromisoformat(user["tier_expires"])
            if datetime.now() > expires:
                # 自动降级
                conn2 = get_conn()
                conn2.execute("UPDATE users SET tier='normal', tier_expires=NULL WHERE id=?", (uid,))
                conn2.commit()
                conn2.close()
                user["tier"] = "normal"
                user["tier_expires"] = None
        except Exception:
            pass
    return user


def json_resp(data, status=200):
    resp = make_response(jsonify(data), status)
    return resp


# 手机号：11位数字，1开头；邮箱：标准格式
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def validate_identifier(identifier):
    """返回 (ok, error_message, id_type)"""
    identifier = (identifier or "").strip()
    if not identifier:
        return False, "请输入手机号或邮箱", None
    if PHONE_RE.match(identifier):
        return True, None, "phone"
    if EMAIL_RE.match(identifier):
        return True, None, "email"
    return False, "请输入有效的手机号（11位）或邮箱", None


# ─── 注册 ───
@app.post("/account-api/register")
def register():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("username") or data.get("identifier") or "").strip()
    password = data.get("password") or ""
    ok, err, id_type = validate_identifier(identifier)
    if not ok:
        return json_resp({"error": err}, 400)
    if len(password) < 6:
        return json_resp({"error": "密码至少6位"}, 400)
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (identifier,)).fetchone():
        conn.close()
        return json_resp({"error": "该手机号/邮箱已注册"}, 409)
    conn.execute(
        "INSERT INTO users (username, password_hash, tier, created_at) VALUES (?,?,?,?)",
        (identifier, generate_password_hash(password), "normal", datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return json_resp({"ok": True, "message": "注册成功，请登录"})


# ─── 登录 ───
@app.post("/account-api/login")
def login():
    ip = request.headers.get("X-Real-IP") or request.remote_addr
    if rate_limited(ip):
        return json_resp({"error": f"尝试过多，请{RATE_LIMIT_WINDOW}秒后再试"}, 429)
    data = request.get_json(silent=True) or {}
    identifier = (data.get("username") or data.get("identifier") or "").strip()
    password = data.get("password") or ""
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (identifier,)).fetchone()
    record_attempt(ip)
    if not row or not check_password_hash(row["password_hash"], password):
        conn.close()
        return json_resp({"error": "手机号/邮箱或密码错误"}, 401)
    session.permanent = True
    session["uid"] = row["id"]
    session["username"] = row["username"]
    conn.close()
    user = current_user()
    return json_resp({
        "ok": True,
        "username": user["username"],
        "tier": user["tier"],
        "tier_expires": user.get("tier_expires"),
    })


# ─── 登出 ───
@app.post("/account-api/logout")
def logout():
    session.clear()
    return json_resp({"ok": True})


# ─── 当前用户 ───
@app.get("/account-api/me")
def me():
    user = current_user()
    if not user:
        return json_resp({"logged_in": False, "tier": "normal"})
    return json_resp({
        "logged_in": True,
        "username": user["username"],
        "tier": user["tier"],
        "tier_expires": user.get("tier_expires"),
    })


# ─── 兑换码升级 ───
@app.post("/account-api/redeem")
def redeem():
    user = current_user()
    if not user:
        return json_resp({"error": "请先登录"}, 401)
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return json_resp({"error": "请输入兑换码"}, 400)
    conn = get_conn()
    rc = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
    if not rc:
        conn.close()
        return json_resp({"error": "兑换码不存在"}, 404)
    if rc["used_by"]:
        conn.close()
        return json_resp({"error": "兑换码已被使用"}, 409)
    # 计算到期时间
    if rc["valid_days"] and rc["valid_days"] > 0:
        expires = (datetime.now() + timedelta(days=rc["valid_days"])).isoformat(timespec="seconds")
    else:
        expires = None  # 永久
    # 升级（svip > vip > normal，只升不降）
    tier_rank = {"normal": 0, "vip": 1, "svip": 2}
    if tier_rank.get(rc["tier"], 0) < tier_rank.get(user["tier"], 0):
        conn.close()
        return json_resp({"error": f"你当前是{user['tier']}，不能降级"}, 400)
    conn.execute(
        "UPDATE users SET tier=?, tier_expires=? WHERE id=?",
        (rc["tier"], expires, user["id"]),
    )
    conn.execute(
        "UPDATE redeem_codes SET used_by=?, used_at=? WHERE code=?",
        (user["username"], datetime.now().isoformat(timespec="seconds"), code),
    )
    # 记录已用兑换码
    redeemed = json.loads(user.get("redeemed_codes") or "[]")
    redeemed.append({"code": code, "tier": rc["tier"], "at": datetime.now().isoformat(timespec="seconds")})
    conn.execute("UPDATE users SET redeemed_codes=? WHERE id=?", (json.dumps(redeemed, ensure_ascii=False), user["id"]))
    conn.commit()
    conn.close()
    return json_resp({
        "ok": True,
        "message": f"升级成功！当前等级：{rc['tier']}",
        "tier": rc["tier"],
        "tier_expires": expires,
    })


# ─── 管理统计（需 admin 登录）───
@app.get("/account-api/admin/stats")
def admin_stats():
    user = current_user()
    if not user or user["username"] != ADMIN_USERNAME:
        return json_resp({"error": "无权限"}, 403)
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    by_tier = {r["tier"]: r["c"] for r in conn.execute("SELECT tier, COUNT(*) c FROM users GROUP BY tier")}
    codes_total = conn.execute("SELECT COUNT(*) c FROM redeem_codes").fetchone()["c"]
    codes_used = conn.execute("SELECT COUNT(*) c FROM redeem_codes WHERE used_by IS NOT NULL").fetchone()["c"]
    conn.close()
    return json_resp({
        "users_total": total,
        "users_by_tier": by_tier,
        "codes_total": codes_total,
        "codes_used": codes_used,
        "codes_available": codes_total - codes_used,
    })


def require_admin():
    """返回当前 admin 用户 dict，否则返回 None"""
    user = current_user()
    if not user or user["username"] != ADMIN_USERNAME:
        return None
    return user


# ─── 管理员：生成兑换码 ───
@app.post("/account-api/admin/generate")
def admin_generate():
    if not require_admin():
        return json_resp({"error": "无权限"}, 403)
    import secrets as _s, string as _st
    data = request.get_json(silent=True) or {}
    tier = data.get("tier")
    days = int(data.get("days", 30) or 0)
    count = min(int(data.get("count", 1) or 1), 200)
    note = data.get("note", "")
    if tier not in ("vip", "svip"):
        return json_resp({"error": "tier 只能是 vip/svip"}, 400)
    prefix = tier.upper()
    chars = _st.ascii_uppercase + _st.digits
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    created = []
    for _ in range(count):
        for _try in range(10):
            code = f"{prefix}-" + "".join(_s.choice(chars) for _ in range(6))
            if not conn.execute("SELECT 1 FROM redeem_codes WHERE code=?", (code,)).fetchone():
                break
        conn.execute(
            "INSERT INTO redeem_codes (code, tier, valid_days, batch_note, created_at) VALUES (?,?,?,?,?)",
            (code, tier, days, note, now),
        )
        created.append(code)
    conn.commit()
    conn.close()
    return json_resp({"ok": True, "codes": created})


# ─── 管理员：查看兑换码 ───
@app.get("/account-api/admin/codes")
def admin_codes():
    if not require_admin():
        return json_resp({"error": "无权限"}, 403)
    only_unused = request.args.get("only-unused") == "1"
    conn = get_conn()
    if only_unused:
        rows = conn.execute("SELECT * FROM redeem_codes WHERE used_by IS NULL ORDER BY created_at DESC LIMIT 500").fetchall()
    else:
        rows = conn.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC LIMIT 500").fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return json_resp(result)


# ─── 管理员：查看用户 ───
@app.get("/account-api/admin/users")
def admin_users():
    if not require_admin():
        return json_resp({"error": "无权限"}, 403)
    conn = get_conn()
    rows = conn.execute("SELECT id, username, tier, tier_expires, created_at FROM users ORDER BY created_at DESC LIMIT 500").fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return json_resp(result)


# ─── 管理员：手动设置等级 ───
@app.post("/account-api/admin/set-tier")
def admin_set_tier():
    if not require_admin():
        return json_resp({"error": "无权限"}, 403)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    tier = data.get("tier")
    days = int(data.get("days", 30) or 0)
    if tier not in ("normal", "vip", "svip"):
        return json_resp({"error": "tier 非法"}, 400)
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        conn.close()
        return json_resp({"error": "用户不存在"}, 404)
    if tier == "normal":
        conn.execute("UPDATE users SET tier='normal', tier_expires=NULL WHERE username=?", (username,))
    else:
        expires = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds") if days else None
        conn.execute("UPDATE users SET tier=?, tier_expires=? WHERE username=?", (tier, expires, username))
    conn.commit()
    conn.close()
    return json_resp({"ok": True, "username": username, "tier": tier})


# ─── 健康检查 ───
@app.get("/account-api/health")
def health():
    return json_resp({"ok": True, "service": "account-api", "time": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5060, debug=False)
else:
    init_db()
