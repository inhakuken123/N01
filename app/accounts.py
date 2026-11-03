"""accounts.py — 账号与密码。

存储：<程序目录>/accounts.json
    {"users":[{"uid":"00","name":"管理员","role":"admin",
               "salt":"...","hash":"...","iter":200000,"created":"..."}]}

密码只存 PBKDF2-HMAC-SHA256 的哈希，不存明文。每个账号独立 salt。
角色只有两种：admin（管理员）和 user（普通用户）。

命令行（在程序根目录执行）：
    python -m app.accounts list
    python -m app.accounts add --uid 23 --name 同学A --role user
    python -m app.accounts passwd --uid 00
    python -m app.accounts role --uid 23 --role admin
    python -m app.accounts remove --uid 23
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import secrets
import sys
from datetime import datetime, timezone

from .paths import BASE

FILE = BASE / "accounts.json"
ITER = 200_000
ROLES = ("admin", "user")


# ---------------------------------------------------------------- 存取

def load() -> dict:
    if not FILE.exists():
        return {"users": []}
    try:
        data = json.loads(FILE.read_text(encoding="utf-8-sig"))
        data.setdefault("users", [])
        return data
    except Exception:
        return {"users": []}


def save(data: dict) -> None:
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find(data: dict, uid: str) -> dict | None:
    for u in data["users"]:
        if str(u.get("uid")) == str(uid):
            return u
    return None


# ---------------------------------------------------------------- 密码

def _hash(password: str, salt: bytes, iterations: int) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.b64encode(dk).decode("ascii")


def make_record(uid: str, name: str, role: str, password: str,
                relation: str = "", openid: str = "",
                union_openid: str = "") -> dict:
    salt = secrets.token_bytes(16)
    return {
        "uid": str(uid),
        "name": name,
        "role": role if role in ROLES else "user",
        "relation": relation.strip(),      # 和 uid00 的关系：同学 / 朋友 / 网友…
        "openid": str(openid).strip(),     # QQ 官方机器人的 user_openid
        "union_openid": str(union_openid).strip(),   # 跨应用统一 OpenID，可能为空
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": _hash(password, salt, ITER),
        "iter": ITER,
        "created": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def verify(user: dict, password: str) -> bool:
    try:
        salt = base64.b64decode(user["salt"])
        calc = _hash(password, salt, int(user.get("iter", ITER)))
        return secrets.compare_digest(calc, user["hash"])
    except Exception:
        return False


# ---------------------------------------------------------------- 增删改

def add(uid: str, name: str, role: str, password: str,
        relation: str = "", openid: str = "", union_openid: str = "") -> bool:
    data = load()
    if find(data, uid):
        return False
    data["users"].append(
        make_record(uid, name, role, password, relation, openid, union_openid))
    save(data)
    return True


def find_by_openid(openid: str) -> dict | None:
    """按 openid 找人。QQ 官方机器人收到的消息里，发送者就是 user_openid。

    先比 openid，再比 union_openid（跨应用统一 ID，可能为空）。
    """
    key = str(openid).strip()
    if not key:
        return None
    for u in load()["users"]:
        if str(u.get("openid") or "").strip() == key:
            return u
    for u in load()["users"]:
        if str(u.get("union_openid") or "").strip() == key:
            return u
    return None


def set_openid(uid: str, openid: str, union_openid: str = "") -> bool:
    data = load()
    u = find(data, uid)
    if not u:
        return False
    u["openid"] = str(openid).strip()
    u["union_openid"] = str(union_openid).strip()
    save(data)
    return True


def set_relation(uid: str, relation: str) -> bool:
    data = load()
    u = find(data, uid)
    if not u:
        return False
    u["relation"] = (relation or "").strip()
    save(data)
    return True


def set_name(uid: str, name: str) -> bool:
    data = load()
    u = find(data, uid)
    if not u:
        return False
    u["name"] = (name or "").strip()
    save(data)
    return True


def set_password(uid: str, password: str) -> bool:
    data = load()
    u = find(data, uid)
    if not u:
        return False
    rec = make_record(u["uid"], u.get("name", ""), u.get("role", "user"), password)
    u.update({k: rec[k] for k in ("salt", "hash", "iter")})
    save(data)
    return True


def set_role(uid: str, role: str) -> bool:
    if role not in ROLES:
        return False
    data = load()
    u = find(data, uid)
    if not u:
        return False
    u["role"] = role
    save(data)
    return True


def remove(uid: str) -> bool:
    data = load()
    before = len(data["users"])
    data["users"] = [u for u in data["users"] if str(u.get("uid")) != str(uid)]
    if len(data["users"]) == before:
        return False
    save(data)
    return True


def public_list() -> list[dict]:
    """给管理员看的列表，不含哈希。"""
    return [{k: u.get(k) for k in
             ("uid", "name", "role", "relation", "openid", "union_openid", "created")}
            for u in load()["users"]]


# ---------------------------------------------------------------- 首次初始化

def seed_if_empty(default_uid: str = "00", name: str = "管理员") -> str | None:
    """没有任何账号时，建一个管理员并返回随机密码。已有账号则返回 None。"""
    data = load()
    if data["users"]:
        return None
    pwd = secrets.token_urlsafe(9)
    data["users"].append(make_record(default_uid, name, "admin", pwd))
    save(data)
    return pwd


# ---------------------------------------------------------------- CLI

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]

    def opt(name: str, default: str = "") -> str:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    if cmd == "list":
        users = public_list()
        if not users:
            print("还没有账号。启动服务时会自动创建管理员。")
            return
        print(f"{'uid':<6}{'角色':<8}{'名字':<12}{'关系':<10}{'openid':<18}创建时间")
        for u in users:
            print(f"{u['uid']:<6}{u['role']:<8}{u.get('name',''):<12}"
                  f"{u.get('relation','') or '—':<10}"
                  f"{(u.get('openid') or '—')[:16]:<18}{u.get('created','')}")

    elif cmd == "add":
        uid = opt("--uid")
        if not uid:
            print("需要 --uid")
            return
        name = opt("--name", f"uid{uid}")
        role = opt("--role", "user")
        relation = opt("--relation", "")
        oid = opt("--openid", "")
        uoid = opt("--union-openid", "")
        pw = getpass.getpass("设置密码：")
        if len(pw) < 4:
            print("密码太短（至少 4 位）")
            return
        if pw != getpass.getpass("再输一次："):
            print("两次不一致")
            return
        print("已创建" if add(uid, name, role, pw, relation, oid, uoid)
              else f"uid{uid} 已存在")

    elif cmd == "relation":
        uid, rel = opt("--uid"), opt("--relation")
        print("已修改" if set_relation(uid, rel) else "没有这个 uid")

    elif cmd == "openid":
        uid = opt("--uid")
        oid, uoid = opt("--openid"), opt("--union-openid")
        print("已修改" if set_openid(uid, oid, uoid) else "没有这个 uid")

    elif cmd == "passwd":
        uid = opt("--uid")
        pw = getpass.getpass("新密码：")
        if pw != getpass.getpass("再输一次："):
            print("两次不一致")
            return
        print("已修改" if set_password(uid, pw) else "没有这个 uid")

    elif cmd == "role":
        uid, role = opt("--uid"), opt("--role")
        print("已修改" if set_role(uid, role) else "失败：检查 uid 和 role（admin/user）")

    elif cmd == "remove":
        print("已删除" if remove(opt("--uid")) else "没有这个 uid")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
