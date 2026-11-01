"""Contact name resolution — merges contact.db (auto) with contacts.json (override)."""
import json
import os
from contextvars import ContextVar
from functools import wraps

import config
import db

_merged_cache: dict[str, dict] | None = None
_query_contacts = ContextVar('query_contacts', default=None)


def query_snapshot(function):
    """Reuse contacts within one request, then discard the live snapshot."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if config.DB_BACKEND != 'sqlcipher_native' or _query_contacts.get() is not None:
            return function(*args, **kwargs)
        token = _query_contacts.set([None])
        try:
            return function(*args, **kwargs)
        finally:
            _query_contacts.reset(token)
    return wrapped


def _build_merged() -> dict[str, dict]:
    """Build merged contact map: contact.db as base, contacts.json overrides.

    Each value: {nickname, remark, alias, local_type}
    - nickname/remark from contacts.json take priority over contact.db
    - alias comes from contact.db (the current 微信号)
    """
    global _merged_cache
    scope = _query_contacts.get() if config.DB_BACKEND == 'sqlcipher_native' else None
    if scope is not None and scope[0] is not None:
        return scope[0]
    if _merged_cache is not None and config.DB_BACKEND != 'sqlcipher_native':
        return _merged_cache

    # Base layer: contact.db
    merged = {}
    try:
        merged = db.load_contacts_from_db()
    except FileNotFoundError:
        if config.DB_BACKEND == 'sqlcipher_native':
            raise
        pass

    # Override layer: contacts.json
    if os.path.exists(config.CONTACTS_FILE):
        try:
            with open(config.CONTACTS_FILE, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            for wxid, info in overrides.items():
                if wxid not in merged:
                    merged[wxid] = {
                        "alias": "", "nick_name": "", "remark": "", "local_type": "0"
                    }
                if info.get("nickname"):
                    merged[wxid]["nick_name"] = info["nickname"]
                if info.get("remark"):
                    merged[wxid]["remark"] = info["remark"]
        except (json.JSONDecodeError, OSError):
            pass

    if config.DB_BACKEND == 'sqlcipher_native':
        if scope is not None:
            scope[0] = merged
    else:
        _merged_cache = merged
    return merged


def invalidate_cache():
    """Clear the offline merged cache (live snapshots last only one request)."""
    global _merged_cache
    _merged_cache = None
    scope = _query_contacts.get()
    if scope is not None:
        scope[0] = None


def resolve_contact_name(wxid: str) -> str:
    """Get human-readable display name for a wxid.

    Priority: remark (contacts.json) > remark (contact.db) >
              nick_name (contacts.json) > nick_name (contact.db) >
              alias (current 微信号) > raw wxid.

    For group chats, falls back to truncated chatroom ID.
    """
    merged = _build_merged()

    if "@chatroom" in wxid:
        info = merged.get(wxid, {})
        if info.get("remark"):
            return info["remark"]
        if info.get("nick_name"):
            return info["nick_name"]
        return f"群聊({wxid.split('@')[0]})"

    info = merged.get(wxid, {})
    remark = info.get("remark", "")
    if remark:
        return remark
    nick = info.get("nick_name", "")
    if nick:
        return nick
    alias = info.get("alias", "")
    if alias:
        return alias
    return wxid


def resolve_nickname(wxid: str) -> str:
    """Like resolve_contact_name but prefers the person's own nick_name
    over my remark — used for chat export speaker labels.

    Priority: nick_name > remark > alias > raw wxid.
    """
    info = _build_merged().get(wxid, {})
    return (info.get("nick_name") or info.get("remark")
            or info.get("alias") or wxid)


def find_contact(contact: str, name2id: dict[str, str]) -> list[tuple[str, str, str]]:
    """Find matching contacts by wxid, alias, nickname, or remark.

    Returns list of (table_name, wxid, display_name) tuples.
    Searches contact.db fields + contacts.json overrides.
    """
    contact_lower = contact.lower()
    merged = _build_merged()

    # Phase 1: Exact wxid match
    exact = []
    for table, wxid in name2id.items():
        if wxid.lower() == contact_lower:
            exact.append((table, wxid, resolve_contact_name(wxid)))
    if exact:
        return exact

    # Phase 2: Partial wxid match
    partial_wxid = []
    for table, wxid in name2id.items():
        if contact_lower in wxid.lower():
            partial_wxid.append((table, wxid, resolve_contact_name(wxid)))

    # Phase 3: Match against contact.db fields (alias, nick_name, remark)
    field_matches = []
    for table, wxid in name2id.items():
        info = merged.get(wxid, {})
        alias = info.get("alias", "").lower()
        nick = info.get("nick_name", "").lower()
        remark = info.get("remark", "").lower()
        if (contact_lower in alias or contact_lower in nick
                or contact_lower in remark):
            field_matches.append((table, wxid, resolve_contact_name(wxid)))

    # Combine, deduplicate
    seen = set()
    results = []
    for match_list in [partial_wxid, field_matches]:
        for item in match_list:
            if item[1] not in seen:
                seen.add(item[1])
                results.append(item)

    return results
