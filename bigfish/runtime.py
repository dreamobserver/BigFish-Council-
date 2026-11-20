"""运行时装配：把 Settings 变成可用的 (Database, Council)。"""
from __future__ import annotations

from .backup import ensure_key_consistency
from .config import Settings
from .core import Council
from .db import Database
from .security import load_or_create_key


def open_council(settings: Settings, key: bytes | None = None) -> tuple[Database, Council]:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.init_schema()
    # B2 自愈：如果上次恢复在「换库」和「换密钥」之间被杀，
    # 这里的指纹不一致检测会把密钥重新轮换，保证旧执行票不会复活。
    try:
        ensure_key_consistency(db, settings.key_path)
    except Exception:      # 自愈失败不阻止启动，但会留在 audit / 日志里
        pass
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
