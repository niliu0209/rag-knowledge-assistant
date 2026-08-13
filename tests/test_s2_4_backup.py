"""S2-4 备份/恢复：产物可核验（manifest + sha256）、往返一致、篡改拒绝、轮转。

覆盖验收锚点「备份产物清单可核验（时间戳/哈希）」「恢复后数据一致」：
- 备份产物含 manifest.json（文件列表 + sha256 + 生成时间）
- 恢复往返后文件与 SQLite 数据一致（含 Key 可用性前提：secrets 在包内）
- 篡改/损坏产物（任一文件字节变更）恢复必须拒绝且目标目录不动
- 轮转保留最近 N 份删最旧
- 排除项（logs/ 可重建、历史迁移 *.bak）不进包
"""

from __future__ import annotations

import sqlite3
import tarfile
import zlib
from pathlib import Path

import pytest


@pytest.fixture
def backup_service():
    """导入服务模块（延迟到 fixture，避免收集期依赖）。"""
    from app.services import backup

    return backup


def _make_data_dir(root: Path) -> Path:
    """构造真实形态数据目录：rag.db + chroma/ + uploads/ + secrets/ + 排除项。"""
    data = root / "data"
    (data / "chroma" / "col1").mkdir(parents=True)
    (data / "uploads").mkdir()
    (data / "secrets").mkdir()
    (data / "logs").mkdir()

    con = sqlite3.connect(str(data / "rag.db"))
    con.execute("create table docs (id text, title text)")
    con.execute("insert into docs values ('d1', '备份测试文档')")
    con.execute("insert into docs values ('d2', '恢复后仍存在')")
    con.commit()
    con.close()

    (data / "chroma" / "col1" / "data_level0.bin").write_bytes(b"\x00\x01\x02chroma-data")
    (data / "uploads" / "doc1.pdf").write_bytes(b"%PDF-1.4 fake content")
    (data / "secrets" / "rag_key.bin").write_bytes(b"\x13\x37secret-key-material")
    (data / "logs" / "access.log").write_text("2026-08-14 access")
    (data / "rag.db.pre-v003-20260811063922.bak").write_bytes(b"old migration backup")
    return data


def test_backup_creates_manifest_with_hashes(backup_service, tmp_path):
    """备份产物：tar.gz 内含 manifest.json，覆盖应备份项、排除 logs 与 *.bak。"""
    data = _make_data_dir(tmp_path)
    out = tmp_path / "backups"
    out.mkdir()

    manifest = backup_service.build_backup(data, out / "rag-backup-test.tar.gz")

    assert manifest["created_at"]
    assert {f["path"] for f in manifest["files"]} == {
        "rag.db",
        "chroma/col1/data_level0.bin",
        "uploads/doc1.pdf",
        "secrets/rag_key.bin",
    }
    with tarfile.open(out / "rag-backup-test.tar.gz", "r:gz") as tf:
        names = tf.getnames()
    assert "manifest.json" in names
    assert not any("logs" in n or "pre-v003" in n for n in names)


def test_restore_roundtrip_data_consistent(backup_service, tmp_path):
    """恢复往返：文件字节一致 + SQLite 数据一致 + integrity_check 通过。"""
    data = _make_data_dir(tmp_path)
    out = tmp_path / "backups"
    out.mkdir()
    backup_service.build_backup(data, out / "rag-backup-roundtrip.tar.gz")

    target = tmp_path / "restored"
    backup_service.restore_backup(out / "rag-backup-roundtrip.tar.gz", target)

    assert (target / "secrets" / "rag_key.bin").read_bytes() == b"\x13\x37secret-key-material"
    assert (target / "uploads" / "doc1.pdf").read_bytes() == b"%PDF-1.4 fake content"
    con = sqlite3.connect(str(target / "rag.db"))
    rows = con.execute("select title from docs order by id").fetchall()
    assert rows == [("备份测试文档",), ("恢复后仍存在",)]
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    con.close()


def test_restore_rejects_tampered_backup(backup_service, tmp_path):
    """篡改产物（备份后改一个字节）：恢复必须拒绝且目标目录不产生文件。"""
    data = _make_data_dir(tmp_path)
    out = tmp_path / "backups"
    out.mkdir()
    pkg = out / "rag-backup-tampered.tar.gz"
    backup_service.build_backup(data, pkg)

    # 篡改：解包改 uploads/doc1.pdf 一个字节，重新打包
    staging = tmp_path / "tamper-staging"
    with tarfile.open(pkg, "r:gz") as tf:
        tf.extractall(staging)
    (staging / "uploads" / "doc1.pdf").write_bytes(b"%PDF-1.4 TAMPERED!!!")
    with tarfile.open(pkg, "w:gz") as tf:
        for item in sorted(staging.iterdir()):
            tf.add(item, arcname=item.name)

    target = tmp_path / "restored"
    with pytest.raises(backup_service.BackupIntegrityError, match="sha256"):
        backup_service.restore_backup(pkg, target)
    assert not target.exists() or not any(target.iterdir())


def test_rotate_keeps_newest_n(backup_service, tmp_path):
    """轮转：保留最近 N 份，删除最旧（按文件名 UTC 时间戳排序）。"""
    out = tmp_path / "backups"
    out.mkdir()
    for i in range(1, 7):  # 6 份：010000..060000，06 最新
        (out / f"rag-backup-20260814T{i:02d}0000Z.tar.gz").write_bytes(b"x")

    deleted = backup_service.rotate_backups(out, keep=5)

    assert deleted == [out / "rag-backup-20260814T010000Z.tar.gz"]
    remaining = sorted(p.name for p in out.iterdir())
    assert remaining == [f"rag-backup-20260814T{i:02d}0000Z.tar.gz" for i in range(2, 7)]


def test_restore_missing_manifest_rejected(backup_service, tmp_path):
    """无 manifest 的包（非本工具产物）恢复拒绝。"""
    bad = tmp_path / "not-ours.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        payload = tmp_path / "payload.txt"
        payload.write_text("随便一个文件")
        tf.add(payload, arcname="payload.txt")

    with pytest.raises(backup_service.BackupIntegrityError):
        backup_service.restore_backup(bad, tmp_path / "restored")
