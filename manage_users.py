#!/usr/bin/env python3
"""选股雷达 用户管理 CLI 工具

用法:
  python3 manage_users.py generate --tier vip --days 30 --count 10 [--note "首批"]
  python3 manage_users.py list-codes [--only-unused]
  python3 manage_users.py list-users
  python3 manage_users.py set-tier <username> --tier svip --days 90
  python3 manage_users.py reset-password <username> [--password NEWPWD]
  python3 manage_users.py delete-user <username>
  python3 manage_users.py stats
"""
import sys, json, sqlite3, argparse, secrets, string
from datetime import datetime, timedelta
from pathlib import Path
from werkzeug.security import generate_password_hash

DB_PATH = Path(__file__).resolve().parent / "users.db"
TIER_NAMES = {"normal": "普通", "vip": "VIP", "svip": "超级VIP"}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def gen_code(prefix):
    """生成兑换码：TIER-XXXXXX (6位大写字母数字)"""
    chars = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(chars) for _ in range(6))
    return f"{prefix}-{body}"


def cmd_generate(args):
    tier = args.tier
    if tier not in ("vip", "svip"):
        print("错误：--tier 只能是 vip 或 svip")
        sys.exit(1)
    prefix = tier.upper()
    conn = get_conn()
    created = []
    now = datetime.now().isoformat(timespec="seconds")
    for _ in range(args.count):
        # 避免碰撞
        for _try in range(10):
            code = gen_code(prefix)
            if not conn.execute("SELECT 1 FROM redeem_codes WHERE code=?", (code,)).fetchone():
                break
        conn.execute(
            "INSERT INTO redeem_codes (code, tier, valid_days, batch_note, created_at) VALUES (?,?,?,?,?)",
            (code, tier, args.days, args.note or "", now),
        )
        created.append(code)
    conn.commit()
    conn.close()
    print(f"已生成 {len(created)} 个 {TIER_NAMES[tier]} 兑换码（有效期 {'永久' if not args.days else str(args.days)+'天'}）：")
    for c in created:
        print(f"  {c}")


def cmd_list_codes(args):
    conn = get_conn()
    if args.only_unused:
        rows = conn.execute("SELECT * FROM redeem_codes WHERE used_by IS NULL ORDER BY created_at DESC")
    else:
        rows = conn.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC")
    print(f"{'兑换码':<14} {'等级':<8} {'有效期':<8} {'状态':<8} {'使用者':<12} {'使用时间':<20} {'备注'}")
    print("-" * 100)
    for r in rows:
        status = "已用" if r["used_by"] else "可用"
        valid = "永久" if not r["valid_days"] else f"{r['valid_days']}天"
        print(f"{r['code']:<14} {TIER_NAMES.get(r['tier'], r['tier']):<8} {valid:<8} {status:<8} {r['used_by'] or '-':<12} {r['used_at'] or '-':<20} {r['batch_note'] or ''}")
    conn.close()


def cmd_list_users(args):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC")
    print(f"{'手机号/邮箱':<24} {'等级':<8} {'到期':<20} {'注册时间':<20} {'已用兑换码'}")
    print("-" * 110)
    for r in rows:
        codes = json.loads(r["redeemed_codes"] or "[]")
        code_str = ", ".join(c["code"] for c in codes) or "-"
        print(f"{r['username']:<24} {TIER_NAMES.get(r['tier'], r['tier']):<8} {r['tier_expires'] or '永久':<20} {r['created_at']:<20} {code_str}")
    conn.close()


def cmd_set_tier(args):
    if args.tier not in ("normal", "vip", "svip"):
        print("错误：--tier 只能是 normal/vip/svip")
        sys.exit(1)
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (args.username,)).fetchone()
    if not row:
        print(f"错误：用户 {args.username} 不存在")
        sys.exit(1)
    if args.tier == "normal":
        conn.execute("UPDATE users SET tier='normal', tier_expires=NULL WHERE username=?", (args.username,))
    else:
        expires = (datetime.now() + timedelta(days=args.days)).isoformat(timespec="seconds") if args.days else None
        conn.execute("UPDATE users SET tier=?, tier_expires=? WHERE username=?", (args.tier, expires, args.username))
    conn.commit()
    conn.close()
    print(f"已将 {args.username} 设置为 {TIER_NAMES[args.tier]}")


def cmd_reset_password(args):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (args.username,)).fetchone()
    if not row:
        print(f"错误：用户 {args.username} 不存在")
        sys.exit(1)
    pwd = args.password or secrets.token_urlsafe(8)
    conn.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(pwd), args.username))
    conn.commit()
    conn.close()
    print(f"已重置 {args.username} 的密码：{pwd}")


def cmd_delete_user(args):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (args.username,)).fetchone()
    if not row:
        print(f"错误：用户 {args.username} 不存在")
        sys.exit(1)
    conn.execute("DELETE FROM users WHERE username=?", (args.username,))
    conn.commit()
    conn.close()
    print(f"已删除用户 {args.username}")


def cmd_stats(args):
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    by_tier = {r["tier"]: r["c"] for r in conn.execute("SELECT tier, COUNT(*) c FROM users GROUP BY tier")}
    codes_total = conn.execute("SELECT COUNT(*) c FROM redeem_codes").fetchone()["c"]
    codes_used = conn.execute("SELECT COUNT(*) c FROM redeem_codes WHERE used_by IS NOT NULL").fetchone()["c"]
    conn.close()
    print(f"用户总数：{total}")
    for t in ("normal", "vip", "svip"):
        print(f"  {TIER_NAMES[t]}：{by_tier.get(t, 0)}")
    print(f"\n兑换码总数：{codes_total}（已用 {codes_used}，可用 {codes_total - codes_used}）")


def main():
    p = argparse.ArgumentParser(description="选股雷达用户管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="生成兑换码")
    g.add_argument("--tier", required=True, choices=["vip", "svip"])
    g.add_argument("--days", type=int, default=30, help="有效天数 (0=永久)")
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--note", default="")
    g.set_defaults(func=cmd_generate)

    lc = sub.add_parser("list-codes", help="查看兑换码")
    lc.add_argument("--only-unused", action="store_true")
    lc.set_defaults(func=cmd_list_codes)

    lu = sub.add_parser("list-users", help="查看用户")
    lu.set_defaults(func=cmd_list_users)

    st = sub.add_parser("set-tier", help="设置用户等级")
    st.add_argument("username")
    st.add_argument("--tier", required=True, choices=["normal", "vip", "svip"])
    st.add_argument("--days", type=int, default=30)
    st.set_defaults(func=cmd_set_tier)

    rp = sub.add_parser("reset-password", help="重置密码")
    rp.add_argument("username")
    rp.add_argument("--password", default="")
    rp.set_defaults(func=cmd_reset_password)

    du = sub.add_parser("delete-user", help="删除用户")
    du.add_argument("username")
    du.set_defaults(func=cmd_delete_user)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
