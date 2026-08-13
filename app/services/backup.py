"""BackupService：数据备份 / 恢复 / 轮转（owner：services/backup.py，S2-4）。

规则来源：stage2-mvp.md S2-4 执行拆解（2026-08-14 数据卷实测定案）。
owner 合同：
  - 备份内容：rag.db + chroma/ + uploads/ + secrets/（rag_key.bin 加密主密钥——
    缺它恢复后 API Key 无法解密）；排除 logs/（可重建）与 rag.db.pre-v*-*.bak
    （历史迁移备份，不再备份）
  - SQLite 一致性：python sqlite3 Connection.backup()（在线备份 API，
    journal_mode=delete 下也安全）
  - 产物：rag-backup-<UTC 时间戳>.tar.gz，内嵌 manifest.json
    （files: [{path, sha256}] + created_at）——「备份产物清单可核验」
  - 恢复：先校验 manifest 存在且全部文件 sha256 与清单一致，任一不匹配
    抛 BackupIntegrityError 且目标目录不动（防篡改/损坏）；通过后还原并
    校验 SQLite integrity_check
  - 轮转：按文件名时间戳排序保留最近 N 份，删除最旧

纯函数、不依赖 docker——宿主编排（scripts/backup.sh）只做容器调用与取回。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 备份内容：除排除项外的 data_dir 顶层成员
_BACKUP_MEMBERS = ("rag.db", "chroma", "uploads", "secrets")
_EXCLUDE_PATTERNS = (re.compile(r"^logs(/|$)"), re.compile(r"^rag\.db\.pre-v\d+.*\.bak$"))
_TIMESTAMP_RE = re.compile(r"^rag-backup-(\d{8}T\d{6}Z)\.tar\.gz$")

MANIFEST_NAME = "manifest.json"


class BackupIntegrityError(RuntimeError):
    """备份产物损坏/被篡改（manifest 缺失、sha256 不匹配、时间戳无法解析）。"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(src_db: Path, dst_db: Path) -> None:
    """在线备份 SQLite（备份 API，运行中写入也一致）。"""
    src = sqlite3.connect(str(src_db))
    try:
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _copy_backup_members(data_dir: Path, staging: Path) -> list[Path]:
    """拷贝备份成员到 staging，返回（相对 data_dir）路径列表；跳过排除项。"""
    members: list[Path] = []
    for name in sorted(_BACKUP_MEMBERS):
        src = data_dir / name
        if not src.exists():
            continue
        rel = src.relative_to(data_dir)
        dst = staging / rel
        if src.is_dir():
            shutil.copytree(src, dst)
            members.extend(p.relative_to(staging) for p in dst.rglob("*") if p.is_file())
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            members.append(rel)
    return members


def build_backup(data_dir: Path, out_path: Path) -> dict:
    """备份数据目录到 out_path（tar.gz + manifest）。返回 manifest 内容。"""
    data_dir = Path(data_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    staging = out_path.parent / f".backup-staging-{_now_utc()}"
    staging.mkdir(parents=True)

    try:
        _sqlite_backup(data_dir / "rag.db", staging / "rag.db")
        members = _copy_backup_members(data_dir, staging)

        files = [
            {"path": str(rel.as_posix()), "sha256": _sha256(staging / rel)}
            for rel in members
        ]
        manifest = {
            "created_at": _now_utc(),
            "source_dir": str(data_dir.resolve()),
            "files": files,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with tarfile.open(out_path, "w:gz") as tf:
            for item in sorted(staging.iterdir()):
                tf.add(item, arcname=item.name)
        logger.info("备份完成：%s（%d 个文件）", out_path, len(files))
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _load_manifest(backup_path: Path, extract_dir: Path) -> dict:
    """解包并读取 manifest；缺失/损坏抛 BackupIntegrityError。"""
    with tarfile.open(backup_path, "r:gz") as tf:
        names = tf.getnames()
        if MANIFEST_NAME not in names:
            raise BackupIntegrityError(f"备份包缺少 {MANIFEST_NAME}，非本工具产物")
        # 防路径穿越：包内路径必须是相对路径（无绝对路径与 ..）
        for name in names:
            p = Path(name)
            if p.is_absolute() or ".." in p.parts:
                raise BackupIntegrityError(f"备份包含非法路径：{name}")
        tf.extractall(extract_dir)
    try:
        return json.loads((extract_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(f"{MANIFEST_NAME} 损坏：{exc}") from exc


def restore_backup(backup_path: Path, target_dir: Path) -> dict:
    """校验并还原备份包到 target_dir（须为空/不存在）。返回 manifest。"""
    backup_path = Path(backup_path)
    target_dir = Path(target_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupIntegrityError(f"还原目标非空：{target_dir}")

    extract_dir = target_dir.parent / f".restore-staging-{_now_utc()}"
    extract_dir.mkdir(parents=True)
    try:
        manifest = _load_manifest(backup_path, extract_dir)
        # 全部文件 sha256 校验，任一不匹配即拒绝
        for entry in manifest.get("files", []):
            rel = Path(entry["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise BackupIntegrityError(f"manifest 含非法路径：{rel}")
            actual = _sha256(extract_dir / rel)
            if actual != entry.get("sha256"):
                raise BackupIntegrityError(
                    f"sha256 不匹配（可能损坏/被篡改）：{rel} 期望 {entry['sha256']} 实际 {actual}"
                )
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(extract_dir.iterdir()):
            if item.name == MANIFEST_NAME:
                continue
            shutil.move(str(item), str(target_dir / item.name))
        # SQLite 完整性兜底（恢复后立即校验，损坏即拒绝）
        con = sqlite3.connect(str(target_dir / "rag.db"))
        try:
            status = con.execute("pragma integrity_check").fetchone()[0]
        finally:
            con.close()
        if status != "ok":
            raise BackupIntegrityError(f"还原后 SQLite 完整性检查失败：{status}")
        logger.info("还原完成：%s → %s（%d 个文件）", backup_path, target_dir, len(manifest["files"]))
        return manifest
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def rotate_backups(backup_dir: Path, keep: int = 5) -> list[Path]:
    """轮转：按时间戳排序保留最近 keep 份，返回被删除的文件列表。"""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    candidates = [p for p in backup_dir.iterdir() if _TIMESTAMP_RE.match(p.name)]
    candidates.sort(key=lambda p: _TIMESTAMP_RE.match(p.name).group(1))  # type: ignore[union-attr]
    deleted: list[Path] = []
    for old in candidates[:-keep] if keep > 0 else candidates:
        old.unlink()
        deleted.append(old)
    if deleted:
        logger.info("轮转删除 %d 份最旧备份（保留 %d 份）", len(deleted), keep)
    return deleted
