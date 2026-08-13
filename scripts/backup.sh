#!/usr/bin/env bash
# S2-4 定时备份宿主编排（cron 入口）：
#   容器内在线备份（sqlite Connection.backup() 保证一致性）→ cp 取回宿主 backups/
#   → 独立哈希复核（验收锚点「备份产物清单可核验」）→ 轮转保留最近 N 份
# 用法：scripts/backup.sh [KEEP]   # KEEP 默认 5
# 产物：backups/rag-backup-<UTC时间戳>.tar.gz（内含 manifest.json，权限 700/600）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
KEEP="${1:-5}"
BACKUP_DIR="$ROOT/backups"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="rag-backup-$TS.tar.gz"

mkdir -p -m 700 "$BACKUP_DIR"

# 1) 容器内在线备份（exec api 执行，服务代码与数据同卷）
docker compose exec -T api python3 -c "from app.services.backup import build_backup; build_backup('/data', '/tmp/$NAME')"

# 2) 取回宿主并清理容器内临时文件
docker compose cp "api:/tmp/$NAME" "$BACKUP_DIR/$NAME"
docker compose exec -T api rm -f "/tmp/$NAME"
chmod 600 "$BACKUP_DIR/$NAME"

# 3) 独立哈希复核（宿主侧逐文件 sha256 与 manifest 比对，不依赖服务内部校验）
python3 - "$BACKUP_DIR/$NAME" <<'PY'
import hashlib, json, sys, tarfile
from pathlib import Path
p = Path(sys.argv[1])
with tarfile.open(p, "r:gz") as tf:
    names = tf.getnames()
    if "manifest.json" not in names:
        sys.exit(f"错误：备份包缺少 manifest.json：{p}")
    manifest = json.loads(tf.extractfile("manifest.json").read())
    for entry in manifest["files"]:
        data = tf.extractfile(entry["path"]).read()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            sys.exit(f"错误：sha256 不匹配 {entry['path']}，产物可疑")
print(f"校验通过：{p.name}（{len(manifest['files'])} 个文件，创建于 {manifest['created_at']}）")
for entry in manifest["files"]:
    print(f"  {entry['path']}  {entry['sha256'][:16]}…")
PY

# 4) 轮转保留最近 KEEP 份（同服务纯函数，宿主 python3 直接调用）
PYTHONPATH="$ROOT" python3 - "$BACKUP_DIR" "$KEEP" <<'PY'
import sys
from pathlib import Path
from app.services.backup import rotate_backups
for d in rotate_backups(Path(sys.argv[1]), int(sys.argv[2])):
    print(f"轮转删除（保留 {sys.argv[2]} 份）：{d.name}")
PY
