"""运行时装配：把 Settings 变成可用的 (Database, Council)。"""
from __future__ import annotations

from .backup import ensure_key_consistency
from .config import Settings
from .core import Council, CouncilError
from .db import Database
from .security import load_or_create_key


def open_council(settings: Settings, key: bytes | None = None) -> tuple[Database, Council]:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.init_schema()
    # B2 自愈是安全边界：密钥指纹不一致且无法修复时必须拒绝启动。
    # 继续使用未核验的磁盘密钥可能让旧执行票在恢复异常路径上继续有效。
    try:
        ensure_key_consistency(db, settings.key_path)
    except Exception as exc:
        db.close()
        raise CouncilError("执行票密钥一致性校验失败，拒绝启动") from exc
    if key is None:
        key = load_or_create_key(settings.key_path)
    council = Council(
        db,
        key=key,
        heartbeat_ttl=settings.heartbeat_ttl_sec,
        ticket_ttl=settings.ticket_ttl_sec,
        ticket_max_ttl=settings.ticket_max_ttl_sec,
        heartbeat_lease=settings.heartbeat_lease_sec,
        key_path=settings.key_path,
    )
    return db, council


def reload_key(settings: Settings, council: Council) -> bytes:
    """恢复备份轮换密钥后，把新密钥装载回运行中的 Council 实例。"""
    key = load_or_create_key(settings.key_path)
    council.key = key
    council.key_path = settings.key_path
    return key
