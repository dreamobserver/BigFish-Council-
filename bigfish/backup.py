"""备份与恢复：一致性快照 + 校验 + 原子替换 + 密钥轮换.

两类包（v0.1.1-rc3 P1-4.3 明确区分）：

A. 完整操作备份（export_bundle / kind=operational）
   - 含 council.db 全量快照，用于真出事后**恢复运行**；
   - **不含** secrets/ticket.key（密钥文件独立存放，不进包）；
   - 属于敏感文件：里面有执行器 token 哈希、执行票哈希与幂等响应，
     只能留在本机或加密后存放，不得外发。

B. 脱敏审计导出包（export_audit_bundle / kind=audit）
   - 只含任务/版本/审查/审批/事件/审计记录，全部经过脱敏；
   - **不含** executors.token_hash、tickets 表的任何字段、idempotency 响应、
     也不含 prompt 正文之外的任何凭据材料；
   - 可以安全外发给外部审计。

恢复（restore_bundle）顺序保证「要么成功，要么原库不动」：
  1) 只读校验（zip → manifest → sha256 → integrity → 必需表 → 关联）
  2) 解包到**同目录**临时文件（保证 os.replace 是同一文件系统内原子替换）
  3) 在临时副本上做 schema 迁移（失败不影响原库）
  4) 关闭原连接 → 删 -wal/-shm → os.replace 原子替换
  5) 重开连接 → 版本/世代 +1 → 轮换执行票密钥 → 在途任务全部 BLOCKED
  任何一步失败都把 pre-restore 快照放回原位。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .db import (BACKUP_FORMAT_VERSION, REQUIRED_TABLES, SCHEMA_VERSION, Database,
                 file_sha256, now)
from .locks import LeaseError, exclusive_access
from .security import (KEY_BYTES, SCRUB_KINDS, rotate_key, scan_secrets, scrub,
                       secure_file, write_key_atomic)

MANIFEST_NAME = "manifest.json"
DB_NAME = "council.db"
KIND_OPERATIONAL = "operational"
KIND_AUDIT = "audit"

# 审计包里允许出现的表（显式白名单，不靠注释声称）
AUDIT_TABLES = ("tasks", "task_versions", "reviews", "approvals", "events", "audit")

# R6：审计包改用**列白名单**，不再用黑名单。
# 黑名单的问题是「漏一个列就等于漏一次凭据」——以后有人给 tasks 加一列
# executor_token，黑名单不会自动覆盖。白名单的方向相反：漏列只会少导出数据
# （安全方向的失败），而且真正被跳过哪些列会写进 manifest.redaction。
AUDIT_COLUMNS: dict[str, tuple[str, ...]] = {
    "tasks": ("id", "title", "goal", "scope", "risk", "status", "current_version",
              "control", "executor_id", "blocked_reason", "cancel_requested_at",
              "created_at", "updated_at"),
    "task_versions": ("task_id", "version", "prompt", "note", "created_at"),
    "reviews": ("id", "task_id", "version", "model", "kind", "verdict", "content",
                "source", "actor", "revoked_at", "created_at"),
    "approvals": ("id", "task_id", "version", "decision", "note", "actor",
                  "created_at"),
    "events": ("id", "task_id", "kind", "detail", "created_at"),
    "audit": ("id", "actor", "action", "target", "detail", "created_at"),
}
# 兼容旧引用：这些列名必须永远落在白名单之外
AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")


DISABLED_REASON_RESTORE = "数据库已从备份恢复，恢复前的凭据全部作废，需人工重新授权"

class RestoreError(RuntimeError):
    """恢复失败；原数据库未被修改。"""


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in REQUIRED_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.DatabaseError:
            out[t] = -1
    return out


def _schema_version_of(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return 0


def export_bundle(db: Database, path: str | Path) -> Path:
    """生成完整操作备份包（A）。返回最终路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="bfc-snap-"))
    try:
        snap = db.snapshot(tmpdir / DB_NAME)
        digest = file_sha256(snap)
        conn = sqlite3.connect(str(snap))
        try:
            integrity = (conn.execute("PRAGMA integrity_check").fetchone() or ["?"])[0]
            counts = _counts(conn)
            schema_version = _schema_version_of(conn)
            epoch_row = conn.execute(
                "SELECT value FROM meta WHERE key='restore_epoch'").fetchone()
            restore_epoch = int(epoch_row[0]) if epoch_row else 0
        finally:
            conn.close()

        missing = [t for t, n in counts.items() if n < 0]
        if missing or str(integrity).lower() != "ok":
            raise RuntimeError(
                f"快照校验失败：integrity={integrity} missing_tables={missing}")

        manifest = {
            "backup_kind": KIND_OPERATIONAL,
            "backup_format": BACKUP_FORMAT_VERSION,
            "app_version": __version__,
            "schema_version": schema_version,
            "restore_epoch": restore_epoch,
            "created_at": now(),
            "created_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_file": DB_NAME,
            "db_sha256": digest,
            "db_size": snap.stat().st_size,
            "table_counts": counts,
            "includes_key_file": False,
            "contains_plaintext_credentials": False,
            "contains_credential_hashes": True,
            "handling": "敏感：含 token 哈希与票哈希，只在本机保存或加密存放，不得外发。",
            "note": "执行票由本地密钥派生，密钥文件不在包内；恢复后密钥轮换，旧票全部失效。",
        }
        tmpzip = tmpdir / "bundle.zip"
        with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            z.write(snap, DB_NAME)
        tmpzip.replace(p)
        return p
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def export_audit_bundle(db: Database, path: str | Path) -> Path:
    """生成可安全外发的脱敏审计包（B）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="bfc-audit-"))
    try:
        snap = db.snapshot(tmpdir / DB_NAME)
        payload: dict[str, list[dict]] = {}
        conn = sqlite3.connect(str(snap))
        conn.row_factory = sqlite3.Row
        try:
            integrity = (conn.execute("PRAGMA integrity_check").fetchone() or ["?"])[0]
            if str(integrity).lower() != "ok":
                raise RuntimeError(f"快照校验失败：integrity={integrity}")
            dropped: dict[str, list[str]] = {}
            for table in AUDIT_TABLES:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                allow = set(AUDIT_COLUMNS.get(table, ()))
                skipped: set[str] = set()
                clean = []
                for row in rows:
                    item = {}
                    for k, v in row.items():
                        if k not in allow:
                            skipped.add(k)
                            continue
                        item[k] = scrub(v) if isinstance(v, str) else v
                    clean.append(item)
                if skipped:
                    dropped[table] = sorted(skipped)
                payload[table] = clean
            counts = {t: len(v) for t, v in payload.items()}
            # 逐字段回扫：脱敏之后还剩什么形态的凭据（预期为空，但要说出来）
            hits: dict[str, int] = {}
            for table, rows in payload.items():
                for row in rows:
                    for v in row.values():
                        if isinstance(v, str):
                            for kind in scan_secrets(v):
                                hits[kind] = hits.get(kind, 0) + 1
        finally:
            conn.close()

        manifest = {
            "backup_kind": KIND_AUDIT,
            "backup_format": BACKUP_FORMAT_VERSION,
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "created_at": now(),
            "created_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tables": list(payload),
            "table_counts": counts,
            "redaction": {
                "policy": "allowlist",
                "allowed_columns": {t: list(c) for t, c in AUDIT_COLUMNS.items()},
                "dropped_tables": ["executors", "tickets", "idempotency", "runs",
                                   "artifacts", "meta"],
                "dropped_columns": dropped,
                "string_scrub": "bfc_*/Bearer/sk-*/gh*_*/AKIA*/private-key",
            },
            # F-07：不能把「没命中我们写的那几条正则」说成「保证没有凭据」。
            # 这里如实声明扫描范围，并强制标 manual_review_required。
            "contains_plaintext_credentials": False,
            "contains_credential_hashes": False,
            "credential_scan": {
                "mode": "pattern-based",
                "patterns": list(SCRUB_KINDS),
                "fields_scanned": "全部导出字符串列（含 task_versions.prompt 正文）",
                "coverage": "只覆盖上述已知形态；换行拆分、编码变形、"
                            "私有格式的密钥无法保证命中",
                "hits": hits,
                "manual_review_required": True,
            },
            "handling": ("可外发（需人工复核）：无 token、无 token 哈希、无执行票、"
                         "无幂等响应；自由文本已按已知模式脱敏，"
                         "但脱敏不等于证明，外发前请人工过一遍 manifest.credential_scan。"),
        }
        tmpzip = tmpdir / "audit.zip"
        with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, rows in payload.items():
                z.writestr(name + ".json", json.dumps(rows, ensure_ascii=False))
        tmpzip.replace(p)
        return p
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _verify_snapshot(conn: sqlite3.Connection, manifest: dict) -> tuple[bool, str]:
    verdict = (conn.execute("PRAGMA integrity_check").fetchone() or ["?"])[0]
    if str(verdict).lower() != "ok":
        return False, f"integrity_check 失败：{verdict}"
    counts = _counts(conn)
    missing = [t for t, n in counts.items() if n < 0]
    if missing:
        return False, "缺少必需数据表：" + ", ".join(missing)
    expected = manifest.get("table_counts") or {}
    for t, want in expected.items():
        if counts.get(t) != want:
            return False, f"表 {t} 行数不一致：备份 {want} / 实际 {counts.get(t)}"
    return True, "ok"


def _verify_version(conn: sqlite3.Connection) -> tuple[bool, str]:
    ver = _schema_version_of(conn)
    if ver > SCHEMA_VERSION:
        return False, (f"备份 schema 版本 {ver} 高于当前程序支持的 {SCHEMA_VERSION}，"
                       "请先升级程序，禁止把高版本库恢复进低版本程序")
    return True, f"schema_version={ver}"


def _verify_relational(conn: sqlite3.Connection) -> tuple[bool, str]:
    """关键关联检查：版本、审批、审查都要落在存在且已记录的版本上。"""
    orphan_versions = conn.execute(
        "SELECT COUNT(*) FROM tasks t WHERE NOT EXISTS("
        " SELECT 1 FROM task_versions v WHERE v.task_id=t.id AND v.version=t.current_version)"
    ).fetchone()[0]
    if orphan_versions:
        return False, f"{orphan_versions} 个任务的当前版本在 task_versions 中不存在"
    orphan_approvals = conn.execute(
        "SELECT COUNT(*) FROM approvals a WHERE NOT EXISTS("
        " SELECT 1 FROM task_versions v WHERE v.task_id=a.task_id AND v.version=a.version)"
    ).fetchone()[0]
    if orphan_approvals:
        return False, f"{orphan_approvals} 条审批记录指向不存在的版本"
    orphan_reviews = conn.execute(
        "SELECT COUNT(*) FROM reviews r WHERE NOT EXISTS("
        " SELECT 1 FROM task_versions v WHERE v.task_id=r.task_id AND v.version=r.version)"
    ).fetchone()[0]
    if orphan_reviews:
        return False, f"{orphan_reviews} 条审查记录指向不存在的版本"
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        return False, f"外键检查失败：{len(bad)} 处"
    return True, "ok"


def inspect_bundle(path: str | Path) -> dict:
    """只读检查备份包，不改动任何数据库。"""
    p = Path(path)
    if not p.exists():
        raise RestoreError("备份文件不存在")
    try:
        z = zipfile.ZipFile(p)
    except zipfile.BadZipFile as exc:
        raise RestoreError(f"备份包不是合法 ZIP：{exc}") from None
    with z:
        try:
            names = z.namelist()
        except zipfile.BadZipFile as exc:
            raise RestoreError(f"备份包损坏：{exc}") from None
        if MANIFEST_NAME not in names:
            raise RestoreError("备份包缺少 manifest.json，无法校验")
        try:
            manifest = json.loads(z.read(MANIFEST_NAME).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RestoreError(f"manifest.json 无法解析：{exc}") from None
        kind = manifest.get("backup_kind", KIND_OPERATIONAL)
        if kind == KIND_AUDIT:
            return {"manifest": manifest, "backup_kind": kind,
                    "table_counts": manifest.get("table_counts") or {}}
        if DB_NAME not in names:
            raise RestoreError("备份包缺少 council.db")
        tmp = Path(tempfile.mkdtemp(prefix="bfc-verify-"))
        try:
            z.extract(DB_NAME, tmp)
            dbp = tmp / DB_NAME
            if manifest.get("db_sha256"):
                if file_sha256(dbp) != manifest["db_sha256"]:
                    raise RestoreError("快照 sha256 与 manifest 不一致（文件损坏）")
            conn = sqlite3.connect(str(dbp))
            try:
                ok, msg = _verify_snapshot(conn, manifest)
                if not ok:
                    raise RestoreError(msg)
                ok, msg = _verify_version(conn)
                if not ok:
                    raise RestoreError(msg)
                ok, msg = _verify_relational(conn)
                if not ok:
                    raise RestoreError(msg)
                counts = _counts(conn)
                schema_version = _schema_version_of(conn)
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"manifest": manifest, "backup_kind": kind, "table_counts": counts,
                "schema_version": schema_version,
                "created_at_iso": manifest.get("created_at_iso")}


def _block_inflight(conn: sqlite3.Connection, epoch: int, t: float) -> dict:
    """恢复后把「快照里躺着的授权」全部作废（R1 + P1-4.2）。

    只把 tickets.revoked_at 置位是不够的：本系统里执行票是 HMAC 确定性派生的，
    真正让它失效的是「密钥轮换 + 世代不一致」两道锁。但**恢复让整库回到过去的
    时刻**，光靠票是不够的——快照里可能存着：

      1) 在途任务（DISPATCHED/RUNNING/CANCELLING）：外部进程可能还在跑副作用，
         必须 BLOCKED 交人工确认，绝不能凭旧快照就认为它没在动；
      2) 待派工的已批准任务（APPROVED）：这是「随时可被领取」的授权。恢复把它
         原样搬回来，等于复活了撤销之前的派工许可，必须冻结；
      3) 所有远端身份（executors）：凭据在恢复前就已经发到外面了。恢复后依然
         能用旧 token 登进来（token 哈希随快照一起回来了），所以必须一律禁用；
      4) 历史执行票 + claim 幂等响应：重放路径会把快照里的历史票重新算出来。

    这里一次做全，且不删任何一行：任务、票、身份都保留原记录（审计要用），
    只翻状态位 + 写 disabled_reason。
    """
    in_flight = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('DISPATCHED','RUNNING','CANCELLING')"
    ).fetchall()
    for r in in_flight:
        conn.execute(
            "UPDATE tasks SET status='BLOCKED', blocked_reason=?, updated_at=? WHERE id=?",
            ("数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批", t, r[0]))
    # R1-a：APPROVED 是「可领取」状态，快照里的它等于一张还没发出去的通行证
    frozen = conn.execute(
        "SELECT id FROM tasks WHERE status='APPROVED'").fetchall()
    for r in frozen:
        conn.execute(
            "UPDATE tasks SET status='BLOCKED', blocked_reason=?, updated_at=? WHERE id=?",
            ("数据库已从备份恢复，该任务的派工许可已冻结；"
             "人工核对后需重新提交审批才可再次派工", t, r[0]))
    tickets_revoked = conn.execute(
        "UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL", (t,)).rowcount
    # R1-b：所有远端身份一律禁用（凭据已外发，恢复不能把它们一起复活）
    disabled = conn.execute(
        "UPDATE executors SET enabled=0, disabled_reason=?"
        " WHERE enabled=1", (DISABLED_REASON_RESTORE,)).rowcount
    purged = conn.execute(
        "DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')").rowcount
    return {"blocked": len(in_flight) + len(frozen),
            "frozen_approved": len(frozen),
            "executors_disabled": disabled,
            "tickets_revoked": tickets_revoked,
            "idempotency_purged": purged}


def _prepare_staging(staging: Path, captured_epoch: int, key_sha: str | None,
                     info: dict, actor: str) -> tuple[int, dict]:
    """在临时库上一次性做完：世代 +1、阻断在途、吊销票、清幂等、写审计、记 key 指纹（B2）。

    全部在一个事务里，避免「库已换好但半个月后才想起来写世代」这种半完成状态。
    """
    conn = sqlite3.connect(str(staging))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='restore_epoch'").fetchone()
        staged_epoch = int(row[0]) if row else 0
        new_epoch = max(captured_epoch, staged_epoch) + 1
        t = now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('restore_epoch', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_epoch),))
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_restore_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(t),))
            if key_sha:
                # 启动时用它做自愈：库换代了但密钥没换（两次 replace 之间被杀）能被发现
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('key_sha256', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key_sha,))
            cleanup = _block_inflight(conn, new_epoch, t)
            conn.execute(
                "INSERT INTO audit(actor, action, target, detail, created_at)"
                " VALUES(?,?,?,?,?)",
                (actor, "db.restored", str(info.get("manifest", {}).get("created_at_iso", "")),
                 f"epoch={new_epoch} blocked={cleanup['blocked']} "
                 f"idem_purged={cleanup['idempotency_purged']} "
                 f"source={info.get('backup_kind')}", t))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        return new_epoch, cleanup
    finally:
        conn.close()


def restore_bundle(db: Database, path: str | Path, key_path: str | Path | None = None,
                   before_replace=None, actor: str = "human_owner") -> dict:
    """从备份恢复（B2：真正的原子恢复）。

    顺序（每一步失败都能成对回退，绝不留下「库换了密钥没换」的半成品）：
      1) 只读校验：zip → manifest → 版本 → integrity → 关联 → 必需表
      2) 解包到同目录 staging，在 staging 上迁移并一次性写世代/阻断/审计/key 指纹
      3) 给当前库留 .prerestore 快照，并给当前密钥留 .prerestore 副本
      4) 停服务（before_replace）→ 关连接 → 删 -wal/-shm → 原子替换库 → 原子替换密钥
      5) 重开连接 → 复核完整性 → 返回

    before_replace：在关连接之前调用（用于停 API 线程与看门狗）。

    R8：整个恢复期数据库处于维护模式（db.maintenance）。进入时分两步排空：
      1) 先调 before_replace 让上层停掉 API 线程 / 看门狗（不再有新请求进来）；
      2) 再抢 Database 的写锁（enter_maintenance 内部完成屏障），确认没有
         在途写事务，之后所有普通读写 fail-fast 成 MaintenanceError。
    这样「执行器收到成功应答、恢复又回滚」这种时序就不会发生：在维护模式下
    任何一次提交都直接被拒（503），执行器会走不确定分支去对账，不会误以为成功。
    """
    p = Path(path)
    info = inspect_bundle(p)
    if info.get("backup_kind") == KIND_AUDIT:
        raise RestoreError("这是脱敏审计包，不能用于恢复运行数据库")

    target = db.path
    original_dir = target.parent
    original_dir.mkdir(parents=True, exist_ok=True)
    staging = original_dir / (target.name + ".restore-new")
    key_new: Path | None = None
    key_backup: Path | None = None
    key_installed = False
    db_closed = False
    swapped = False
    had_prerestore = False
    # 回滚也失败时保留维护模式：宁可拒绝服务，也不让一个状态不明的库继续接写
    keep_maintenance = False

    db.enter_maintenance("正在从备份恢复数据库")
    try:
      # F-06：恢复锁只管「另一个 restore」；跨进程的普通写者靠 Database 打开时
      # 登记的写者租约来拦。先放开自己那份，再抢排他，否则自己会把自己挡在外面。
      db.pause_lease()
      with _translate_lease(exclusive_access(target)):
       with db._allow_during_maintenance():
        # 必须在维护旁路内读取真实活库世代；不能把 MaintenanceError 吞成 0。
        captured_epoch = db.restore_epoch()
        try:
            with zipfile.ZipFile(p) as z:
                with z.open(DB_NAME) as src, open(staging, "wb") as dst:
                    shutil.copyfileobj(src, dst)

            # ---- 1) 版本与结构校验（staging 上做，原库一行不动）----
            probe = sqlite3.connect(str(staging))
            try:
                ok, msg = _verify_version(probe)
            finally:
                probe.close()
            if not ok:
                raise RestoreError(msg)

            staged = Database(staging)
            try:
                staged.init_schema()      # 旧版本备份在这里有序迁移到当前版本
            finally:
                staged.close()

            conn = sqlite3.connect(str(staging))
            try:
                ok, msg = _verify_snapshot(conn, info["manifest"])
                if not ok:
                    raise RestoreError(msg)
                ok, msg = _verify_relational(conn)
                if not ok:
                    raise RestoreError(msg)
            finally:
                conn.close()

            # ---- 2) 密钥先在内存里算出新值，指纹写进 staging ----
            new_key_bytes = secrets.token_bytes(KEY_BYTES) if key_path is not None else None
            key_sha = hashlib.sha256(new_key_bytes).hexdigest() if new_key_bytes else None
            if key_path is not None:
                key_new = Path(str(key_path) + ".restore-new")
                write_key_atomic(key_new, new_key_bytes)

            new_epoch, cleanup = _prepare_staging(
                staging, captured_epoch, key_sha, info, actor)

            # ---- 3) 排空上层 → 原库与原密钥各留一份回滚副本 ----
            if before_replace is not None:
                before_replace()
            if target.exists():
                db.snapshot(original_dir / (target.name + ".prerestore"))
                had_prerestore = True
            if key_path is not None and Path(key_path).exists():
                key_backup = Path(str(key_path) + ".prerestore")
                shutil.copyfile(str(key_path), str(key_backup))
                try:
                    secure_file(key_backup)
                except Exception:          # pragma: no cover
                    pass

            # ---- 4) 关连接 → 成对替换 ----
            db.close()
            db_closed = True
            for suffix in ("-wal", "-shm"):
                side = Path(str(target) + suffix)
                if side.exists():
                    side.unlink()

            try:
                os.replace(staging, target)
                swapped = True
            except OSError as exc:
                raise RestoreError(f"数据库原子替换失败：{exc}") from None

            if key_new is not None:
                try:
                    os.replace(key_new, Path(key_path))
                    key_installed = True
                except OSError as exc:
                    raise RestoreError(f"密钥原子替换失败：{exc}") from None

            # ---- 5) 重开连接并复核 ----
            db._reopen()
            db_closed = False
            verdict = db.one("PRAGMA integrity_check")
            if verdict is None or str(verdict[0]).lower() != "ok":
                raise RestoreError(f"恢复后完整性校验失败：{verdict and verdict[0]}")

            return {
                "restored": True,
                "restore_epoch": new_epoch,
                "key_rotated": bool(key_installed),
                "blocked_inflight": _count_blocked(db),
                "tickets_revoked": cleanup.get("tickets_revoked", 0),
                "idempotency_purged": cleanup["idempotency_purged"],
                "table_counts": info["table_counts"],
                "manifest": info["manifest"],
                "created_at_iso": info["manifest"].get("created_at_iso"),
                "executors_disabled": cleanup.get("executors_disabled", 0),
                "frozen_approved": cleanup.get("frozen_approved", 0),
                "prerestore_path": (str(original_dir / (target.name + ".prerestore"))
                                    if had_prerestore else None),
                "prerestore_copy": (str(original_dir / (target.name + ".prerestore"))
                                    if had_prerestore else None),
                "key_backup_path": str(key_backup) if key_backup else None,
            }
        except BaseException as exc:
            # 回滚必须成对：库文件与密钥文件要么一起回到恢复前，要么一起前进。
            # 先确保连接关掉，否则 Windows 上 os.replace 会撞文件占用。
            rollback_errors: list[str] = []
            if db_closed or swapped:
                try:
                    db.close()
                except Exception:            # pragma: no cover
                    pass
                if swapped and had_prerestore:
                    src = original_dir / (target.name + ".prerestore")
                    if src.exists():
                        try:
                            rollback = original_dir / (target.name + ".rollback")
                            shutil.copyfile(src, rollback)
                            for suffix in ("-wal", "-shm"):
                                side = Path(str(target) + suffix)
                                if side.exists():
                                    side.unlink()
                            os.replace(rollback, target)
                        except Exception as rbe:      # pragma: no cover
                            rollback_errors.append(f"库回滚失败：{rbe}")
                if key_installed and key_backup and key_backup.exists():
                    try:
                        os.replace(key_backup, Path(key_path))
                    except Exception as rbe:          # pragma: no cover
                        rollback_errors.append(f"密钥回滚失败：{rbe}")
                try:
                    db._reopen()
                except Exception:            # pragma: no cover
                    pass
            if key_new is not None and key_new.exists():
                try:
                    key_new.unlink()
                except OSError:              # pragma: no cover
                    pass
            if rollback_errors:
                # 回滚也失败：明确报双错，并**保留维护模式**——库处于未知状态时
                # 宁可让所有写请求继续 503，也不能让它带着半套凭据继续接单。
                keep_maintenance = True
                raise RestoreError(
                    "恢复失败且回滚未完全成功：" + "；".join(rollback_errors)
                    + f"（原始错误：{exc}）") from exc
            raise
        finally:
            if staging.exists():
                try:
                    staging.unlink()
                except OSError:              # pragma: no cover
                    pass
    finally:
        # 无论成功失败都要把写者租约抢回来：否则本实例退出「在用」名单，
        # 另一个进程就能在我们眼皮底下把库换掉（F-06）。
        try:
            db.resume_lease()
        except Exception:            # pragma: no cover - 进程正在退出时可能失败
            pass
        if not keep_maintenance:
            db.exit_maintenance()


@contextlib.contextmanager
def _translate_lease(cm):
    """把 LeaseError 翻译成 RestoreError。

    恢复的调用方（GUI / CLI / core）只认识 RestoreError；锁模块不知道
    上层有「恢复」这个业务概念，所以翻译放在这一层做。
    """
    try:
        with cm:
            yield
    except LeaseError as exc:
        raise RestoreError(str(exc)) from None


def _count_blocked(db: Database) -> int:
    row = db.one("SELECT COUNT(*) FROM tasks WHERE status='BLOCKED'")
    return int(row[0]) if row else 0


def ensure_key_consistency(db: Database, key_path: Path) -> dict:
    """启动自愈（B2 折中方案）：库里的 key 指纹与磁盘密钥不一致就重新轮换。

    场景：两次 os.replace 之间被强杀 → 库已经是新世代，磁盘还是旧密钥。
    此时旧票本来就该全部作废，重新生成密钥并把新指纹写回 meta 即可，
    不需要跨文件事务。
    """
    key_path = Path(key_path)
    if not key_path.exists():
        # 密钥文件都没了：现在生成的密钥一定不是签发过票的那把，
        # 所以这里也要把旧票一并作废，否则「密钥换了、票还能用」。
        return _heal_key(db, key_path, action="new-key")
    actual = hashlib.sha256(key_path.read_bytes()).hexdigest()
    row = db.one("SELECT value FROM meta WHERE key='key_sha256'")
    if row is None:
        # 全新库（或 rc5 之前的库）还没有指纹：补一次，之后才有得比。
        return _heal_key(db, key_path, action="recorded", fingerprint=actual,
                         rotate=False)
    if actual == row[0]:
        return {"ok": True, "action": "consistent"}
    return _heal_key(db, key_path, action="rotated", fingerprint=actual)


def _heal_key(db: Database, key_path: Path, action: str,
              fingerprint: str | None = None, rotate: bool = True) -> dict:
    """把「库里的指纹」和「磁盘上的密钥」重新对齐，并作废一切旧执行票。

    审计项 R9 指出两件事，一起修：
    1) 自愈分支没有任何单测（原来靠人读代码）；
    2) 原文案写着「恢复前的执行票全部失效」，但函数里根本没碰 tickets 表——
       密钥换了，tickets 表里的 token_hash 还是旧值，旧票照样能通过认证。
       现在补上：轮换密钥的同时吊销全部未撤销的票、清 claim 幂等、写审计。
    不删任何行，只翻状态位。
    """
    if rotate or not key_path.exists():
        key = rotate_key(key_path)          # 内部已 mkdir + 原子替换 + 0600
        fingerprint = hashlib.sha256(key).hexdigest()
    token_hash = fingerprint or ""
    t = now()
    tickets = 0
    idem = 0
    with db.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES('key_sha256', ?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token_hash,))
        if rotate:
            # 只有真的换了密钥才动票。仅仅补记指纹（老库首次升级到 rc5）时
            # 票还是用同一把密钥签的，作废它们等于无故中断在途执行。
            cur = c.execute("UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL",
                            (t,))
            tickets = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            idem = c.execute(
                "DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')"
            ).rowcount
        c.execute("INSERT INTO audit(actor, action, target, detail, created_at)"
                  " VALUES(?,?,?,?,?)",
                  ("human_owner", "db.key_healed", key_path.name,
                   (f"库内 key 指纹与磁盘密钥不一致（{action}），已重新对齐密钥并"
                    f"吊销 {tickets} 张执行票、清理 {max(idem, 0)} 条幂等记录")
                   if rotate else
                   f"库内尚无 key 指纹（{action}），已补记、未改动任何执行票",
                   t))
    return {"ok": True, "action": action, "tickets_revoked": tickets,
            "idempotency_purged": max(idem, 0)}


