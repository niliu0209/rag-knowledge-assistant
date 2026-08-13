#!/usr/bin/env bash
# S2-4 恢复演练：真实卷备份 → 副本卷 rag-restore-test（独立 project）还原
#   → 数据核对（登录/文档列表/问答记录/真实问答/Key 可用）→ 证据存档 → 清理
# 绝不动生产数据卷 rag-data；演练栈用独立卷，down -v 即还原原状。
# 用法：scripts/restore_test.sh [备份文件]   # 默认取 backups/ 最新一份
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
BACKUP="${1:-$(ls -1t backups/rag-backup-*.tar.gz 2>/dev/null | head -1)}"
if [ -z "${BACKUP:-}" ] || [ ! -f "$BACKUP" ]; then
    echo "错误：未找到备份文件（backups/rag-backup-*.tar.gz 或传入参数）" >&2
    exit 1
fi
TS="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="$ROOT/dev-docs/stages/s2-4-restore-drill-$TS.txt"
echo "恢复演练 $(date -u '+%F %TZ')  备份：$BACKUP" > "$EVIDENCE"

echo "==> [1/6] 演练栈清理 + 副本卷准备（rag-restore-test）"
docker compose -f compose.restore-test.yaml -p rag-restore-test down -v >/dev/null 2>&1 || true
docker volume rm rag-restore-test >/dev/null 2>&1 || true
docker volume create rag-restore-test >/dev/null

echo "==> [2/6] 基线采集（生产栈 rag-data）"
BASELINE="$(docker compose exec -T api python3 - <<'PY' | tee -a "$EVIDENCE"
import json, sqlite3
con = sqlite3.connect("/data/rag.db")
def one(q):
    return con.execute(q).fetchone()[0]
baseline = {
    "admin_docs": one("select count(d.id) from documents d join users u on u.id=d.user_id where u.username='admin'"),
    "friend3_docs": one("select count(d.id) from documents d join users u on u.id=d.user_id where u.username='friend3'"),
    "qa_records": one("select count(*) from qa_records"),
    "users": one("select count(*) from users"),
    "last_question": con.execute("select question from qa_records order by rowid desc limit 1").fetchone()[0],
}
print("基线:", json.dumps(baseline, ensure_ascii=False))
PY
)"
BASELINE="$(echo "$BASELINE" | sed 's/^基线: //')"

echo "==> [3/6] 还原到副本卷（restore_backup 内置全量 sha256 校验，篡改即拒绝）"
BACKUP_ABS="$(realpath "$BACKUP")"
docker run --rm \
    -v rag-restore-test:/data \
    -v "$BACKUP_ABS":/tmp/restore.tar.gz:ro \
    rag-knowledge-assistant-api \
    python3 -c "from app.services.backup import restore_backup; import json; m = restore_backup('/tmp/restore.tar.gz', '/data'); print(json.dumps({'files': len(m['files']), 'created_at': m['created_at']}, ensure_ascii=False))" | tee -a "$EVIDENCE"

echo "==> [4/6] 起演练栈（独立 project rag-restore-test，api/ui/caddy 同镜像、无宿主端口映射）"
docker compose -f compose.restore-test.yaml -p rag-restore-test up -d
# 等 api 健康（栈内探测，60s 超时）
for i in $(seq 1 30); do
    docker compose -f compose.restore-test.yaml -p rag-restore-test exec -T api \
        python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" >/dev/null 2>&1 && break
    [ "$i" = 30 ] && { echo "错误：演练 api 未在 60s 内就绪" >&2; exit 1; }
    sleep 2
done
echo "    演练 api 健康就绪"

echo "==> [5/6] 数据核对 + 真实问答"
docker compose -f compose.restore-test.yaml -p rag-restore-test exec -T api python3 - "$BASELINE" <<'PY' | tee -a "$EVIDENCE"
import json, sys, urllib.request
from http.cookiejar import CookieJar

BASE = "http://localhost:8000"
baseline = json.loads(sys.argv[1])
fails: list[str] = []

def call(method, path, body=None, jar=None):
    # 注意：CookieJar 实现 __len__，空 jar 是 falsy——`jar or CookieJar()` 会新建
    # 无主 jar 导致 cookie 丢失（登录 200 后 me 401），必须显式判 None
    if jar is None:
        jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with opener.open(req) as resp:
        return json.loads(resp.read())

def login(u, p):
    jar = CookieJar()
    call("POST", "/api/auth/login", {"username": u, "password": p}, jar)
    me = call("GET", "/api/auth/me", jar=jar)["user"]
    assert me["username"] == u, f"me 用户不符：{me}"
    return jar

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# 1/2. 登录（session 认证可用）+ 文档列表（API 视角）
jar_admin = login("admin", "AdminPass!@#2026")
check("admin 登录", True, "session 认证通过")
jar_f3 = login("friend3", "Friend3Pass!@#2026")
check("friend3 登录", True, "session 认证通过")
admin_docs = call("GET", "/api/documents", jar=jar_admin)
f3_docs = call("GET", "/api/documents", jar=jar_f3)
check("admin 文档数", len(admin_docs) == baseline["admin_docs"], f"{len(admin_docs)} = 基线 {baseline['admin_docs']}")
check("friend3 文档数", len(f3_docs) == baseline["friend3_docs"], f"{len(f3_docs)} = 基线 {baseline['friend3_docs']}")

# 3. 真实问答（重放最近一次成功提问：向量检索命中引用 + LLM 回答）
qa = call("POST", "/api/qa", {"question": baseline["last_question"]}, jar_admin)
check("问答引用命中", bool(qa.get("citations")), f"citations={len(qa.get('citations', []))}")
check("问答回答生成", bool(qa.get("answer")), f"answer={qa.get('answer', '')[:60]}…")
print("    provider:", qa.get("provider"))

# 4. Key 可用（provider_settings 解密 + 共享预设回落生效）
prov = call("GET", "/api/providers", jar=jar_admin)
check("provider 配置可读", "current" in prov, f"presets={len(prov.get('presets', []))}")

# 5. 问答记录随备份恢复（sqlite 视角）
import sqlite3
con = sqlite3.connect("/data/rag.db")
n_qa = con.execute("select count(*) from qa_records").fetchone()[0]
n_users = con.execute("select count(*) from users").fetchone()[0]
check("问答记录数", n_qa >= baseline["qa_records"], f"{n_qa} >= 基线 {baseline['qa_records']}")
check("用户数", n_users == baseline["users"], f"{n_users} = 基线 {baseline['users']}")

if fails:
    print("核对未通过:", fails)
    sys.exit(1)
print("核对全部通过")
PY

echo "==> [6/6] 清理：演练栈 down + 删副本卷"
docker compose -f compose.restore-test.yaml -p rag-restore-test down -v >/dev/null
# down -v 已删卷；rm 兜底幂等（卷不存在时忽略）
docker volume rm rag-restore-test >/dev/null 2>&1 || true

echo
echo "演练完成（备份：$BACKUP）——证据存档：$EVIDENCE"
