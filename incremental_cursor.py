"""Versioned, opaque high-water marks. No keys, paths, or message bodies."""
import base64
import binascii
import hashlib
import hmac
import json
import re

VERSION = 1
MAX_CURSOR_SIZE = 4 * 1024 * 1024
MAX_LOCAL_ID = 2**63 - 1
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_DB = re.compile(r'message/message_[0-9]+\.db\Z')
_TABLE = re.compile(r'Msg_[a-zA-Z0-9_]+\Z')


class CursorError(ValueError):
    """Invalid cursor or a source that cannot safely continue this cursor."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def encode(state: dict) -> str:
    envelope = dict(state, checksum=hashlib.sha256(_canonical(state)).hexdigest())
    encoded = base64.urlsafe_b64encode(_canonical(envelope)).decode('ascii').rstrip('=')
    if len(encoded) > MAX_CURSOR_SIZE:
        raise CursorError('Cursor exceeds the supported size')
    return encoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CursorError('Duplicate cursor field')
        result[key] = value
    return result


def decode(cursor: str) -> dict:
    try:
        if not isinstance(cursor, str) or not 0 < len(cursor) <= MAX_CURSOR_SIZE:
            raise CursorError('Cursor must be a nonempty bounded string')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
            raise CursorError('Invalid URL-safe cursor encoding')
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        state = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(state, dict) or set(state) != {'version', 'account', 'databases', 'checksum'}:
            raise CursorError('Invalid cursor fields')
        checksum = state.pop('checksum')
        if not isinstance(checksum, str) or not hmac.compare_digest(
                checksum, hashlib.sha256(_canonical(state)).hexdigest()):
            raise CursorError('Cursor checksum mismatch')
        if type(state['version']) is not int or state['version'] != VERSION:
            raise CursorError('Unsupported cursor version')
        if not isinstance(state['account'], str) or not _HEX.fullmatch(state['account']):
            raise CursorError('Invalid account fingerprint')
        databases = state['databases']
        if not isinstance(databases, dict) or not databases:
            raise CursorError('Invalid cursor databases')
        for source, database in databases.items():
            if not _DB.fullmatch(source) or not isinstance(database, dict):
                raise CursorError('Invalid cursor database')
            if set(database) != {'generation', 'tables'}:
                raise CursorError('Invalid cursor database fields')
            generation = database['generation']
            if not isinstance(generation, str) or not _HEX.fullmatch(generation):
                raise CursorError('Invalid database generation')
            if not isinstance(database['tables'], dict):
                raise CursorError('Invalid table watermarks')
            for table, mark in database['tables'].items():
                if not _TABLE.fullmatch(table) or type(mark) is not int or not 0 <= mark <= MAX_LOCAL_ID:
                    raise CursorError('Invalid table watermark')
        if encode(state) != cursor:
            raise CursorError('Noncanonical cursor encoding')
        return state
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError, binascii.Error) as exc:
        if isinstance(exc, CursorError):
            raise
        raise CursorError('Malformed cursor') from None
