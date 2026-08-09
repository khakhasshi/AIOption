"""Per-user persistent AI chat sessions and messages.

Conversations used to live only in browser localStorage, which meant every user
on the same browser shared the same threads. This store moves them server-side,
scoped strictly by ``owner_id`` (the authenticated username), so each user only
ever sees their own sessions.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from .account_store import normalize_owner_id
from .db import connect, ensure_column, run_db_init_once


MAX_MESSAGES_PER_SESSION = 400


def init_chat_db() -> None:
    run_db_init_once("chat_store", _init_chat_db)


def _init_chat_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新会话',
                provider TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '',
                tools_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                traces_json TEXT,
                tables_json TEXT,
                instance_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "chat_messages", "instance_id", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner ON chat_sessions(owner_id, updated_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(owner_id, session_id, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_owner_created ON chat_messages(owner_id, created_at)")


def list_sessions(owner_id: str | None) -> list[dict[str, Any]]:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute(
            """
            SELECT s.id, s.title, s.provider, s.account, s.tools_json, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id AND m.owner_id = s.owner_id) AS message_count
            FROM chat_sessions s
            WHERE s.owner_id = ?
            ORDER BY s.updated_at DESC, s.created_at DESC
            """,
            (owner,),
        ).fetchall()
    return [_session_dict(row) for row in rows]


def get_session(owner_id: str | None, session_id: str) -> dict[str, Any] | None:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute(
            "SELECT id, title, provider, account, tools_json, created_at, updated_at, 0 AS message_count FROM chat_sessions WHERE owner_id = ? AND id = ?",
            (owner, str(session_id or "")),
        ).fetchone()
    return _session_dict(row) if row else None


def create_session(
    owner_id: str | None,
    *,
    title: str | None = None,
    provider: str | None = None,
    account: str | None = None,
    tools: list[str] | None = None,
) -> dict[str, Any]:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    session_id = _new_id()
    now = _utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO chat_sessions (id, owner_id, title, provider, account, tools_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                owner,
                _clean_text(title, 80) or "新会话",
                _clean_text(provider, 64),
                _clean_text(account, 128),
                _dump_tools(tools),
                now,
                now,
            ),
        )
    return {
        "id": session_id,
        "title": _clean_text(title, 80) or "新会话",
        "provider": _clean_text(provider, 64),
        "account": _clean_text(account, 128),
        "tools": tools,
        "message_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def ensure_session(owner_id: str | None, session_id: str | None, **defaults: Any) -> str:
    """Return a valid session id owned by ``owner_id``, creating one if needed."""
    owner = normalize_owner_id(owner_id)
    sid = str(session_id or "").strip()
    if sid:
        existing = get_session(owner, sid)
        if existing:
            return sid
        # Caller supplied an id (client-generated) that does not exist yet: adopt it.
        init_chat_db()
        now = _utc_now()
        with connect() as db:
            db.execute(
                """
                INSERT INTO chat_sessions (id, owner_id, title, provider, account, tools_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    sid,
                    owner,
                    _clean_text(defaults.get("title"), 80) or "新会话",
                    _clean_text(defaults.get("provider"), 64),
                    _clean_text(defaults.get("account"), 128),
                    _dump_tools(defaults.get("tools")),
                    now,
                    now,
                ),
            )
        return sid
    return create_session(owner, **defaults)["id"]


def update_session(owner_id: str | None, session_id: str, **patch: Any) -> dict[str, Any] | None:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    assignments: list[str] = ["updated_at = ?"]
    params: list[Any] = [_utc_now()]
    if "title" in patch:
        assignments.append("title = ?")
        params.append(_clean_text(patch.get("title"), 80) or "新会话")
    if "provider" in patch:
        assignments.append("provider = ?")
        params.append(_clean_text(patch.get("provider"), 64))
    if "account" in patch:
        assignments.append("account = ?")
        params.append(_clean_text(patch.get("account"), 128))
    if "tools" in patch:
        assignments.append("tools_json = ?")
        params.append(_dump_tools(patch.get("tools")))
    params.extend([owner, str(session_id or "")])
    with connect() as db:
        db.execute(
            f"UPDATE chat_sessions SET {', '.join(assignments)} WHERE owner_id = ? AND id = ?",
            params,
        )
    return get_session(owner, session_id)


def delete_session(owner_id: str | None, session_id: str) -> None:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    sid = str(session_id or "")
    with connect() as db:
        db.execute("DELETE FROM chat_messages WHERE owner_id = ? AND session_id = ?", (owner, sid))
        db.execute("DELETE FROM chat_sessions WHERE owner_id = ? AND id = ?", (owner, sid))


def clear_messages(owner_id: str | None, session_id: str) -> None:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    sid = str(session_id or "")
    with connect() as db:
        db.execute("DELETE FROM chat_messages WHERE owner_id = ? AND session_id = ?", (owner, sid))
        db.execute(
            "UPDATE chat_sessions SET title = '新会话', updated_at = ? WHERE owner_id = ? AND id = ?",
            (_utc_now(), owner, sid),
        )


def list_messages(owner_id: str | None, session_id: str) -> list[dict[str, Any]]:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute(
            """
            SELECT id, role, text, traces_json, tables_json, instance_id, created_at
            FROM chat_messages
            WHERE owner_id = ? AND session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (owner, str(session_id or "")),
        ).fetchall()
    return [_message_dict(row) for row in rows]


def append_message(
    owner_id: str | None,
    session_id: str,
    role: str,
    text: str,
    *,
    traces: Any = None,
    tables: Any = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    sid = ensure_session(owner, session_id)
    message_id = _new_id()
    now = _utc_now()
    safe_role = "user" if str(role) == "user" else "assistant"
    body = str(text or "")
    with connect() as db:
        db.execute(
            """
            INSERT INTO chat_messages (id, session_id, owner_id, role, text, traces_json, tables_json, instance_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sid,
                owner,
                safe_role,
                body,
                _dump_json(traces),
                _dump_json(tables),
                instance_id,
                now,
            ),
        )
        # Auto-title the session from the first user message.
        if safe_role == "user":
            current = db.execute(
                "SELECT title FROM chat_sessions WHERE owner_id = ? AND id = ?",
                (owner, sid),
            ).fetchone()
            current_title = str(_row_get(current, "title") or "").strip()
            if not current_title or current_title == "新会话":
                db.execute(
                    "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
                    (_title_from_text(body), now, owner, sid),
                )
            else:
                db.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE owner_id = ? AND id = ?",
                    (now, owner, sid),
                )
        else:
            db.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE owner_id = ? AND id = ?",
                (now, owner, sid),
            )
        _trim_session_messages(db, owner, sid)
    return {"id": message_id, "session_id": sid, "role": safe_role, "created_at": now}


def count_user_messages_today(owner_id: str | None) -> int:
    init_chat_db()
    owner = normalize_owner_id(owner_id)
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        with connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM chat_messages WHERE owner_id = ? AND role = 'user' AND created_at >= ?",
                (owner, day_start),
            ).fetchone()
    except Exception:
        return 0
    return int(_row_get(row, "count") or 0)


def _trim_session_messages(db: Any, owner: str, session_id: str) -> None:
    try:
        row = db.execute(
            "SELECT COUNT(*) AS count FROM chat_messages WHERE owner_id = ? AND session_id = ?",
            (owner, session_id),
        ).fetchone()
        total = int(_row_get(row, "count") or 0)
        if total <= MAX_MESSAGES_PER_SESSION:
            return
        db.execute(
            """
            DELETE FROM chat_messages
            WHERE owner_id = ? AND session_id = ? AND id IN (
                SELECT id FROM chat_messages
                WHERE owner_id = ? AND session_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            )
            """,
            (owner, session_id, owner, session_id, total - MAX_MESSAGES_PER_SESSION),
        )
    except Exception:
        pass


def _session_dict(row: Any) -> dict[str, Any]:
    return {
        "id": _row_get(row, "id"),
        "title": _row_get(row, "title") or "新会话",
        "provider": _row_get(row, "provider") or "",
        "account": _row_get(row, "account") or "",
        "tools": _load_tools(_row_get(row, "tools_json")),
        "message_count": int(_row_get(row, "message_count") or 0),
        "created_at": _row_get(row, "created_at"),
        "updated_at": _row_get(row, "updated_at"),
    }


def _message_dict(row: Any) -> dict[str, Any]:
    return {
        "id": _row_get(row, "id"),
        "role": _row_get(row, "role") or "assistant",
        "text": _row_get(row, "text") or "",
        "traces": _load_json(_row_get(row, "traces_json"), default=[]),
        "tables": _load_json(_row_get(row, "tables_json"), default=[]),
        "instance_id": _row_get(row, "instance_id") or "",
        "created_at": _row_get(row, "created_at"),
    }


def _title_from_text(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return "新会话"
    return cleaned[:22] + ("…" if len(cleaned) > 22 else "")


def _dump_tools(tools: Any) -> str | None:
    if tools is None:
        return None
    if isinstance(tools, list):
        return json.dumps([str(item) for item in tools], ensure_ascii=False)
    return None


def _load_tools(value: Any) -> list[str] | None:
    if value is None:
        return None
    try:
        data = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list):
        return [str(item) for item in data]
    return None


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _load_json(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return default


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _new_id() -> str:
    return f"c_{secrets.token_urlsafe(16)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)
