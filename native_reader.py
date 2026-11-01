"""Native SQLCipher reader. Connections belong to one query, never to a pool.

Only derived keys are cached (in memory). SQLite handles live WAL snapshots and
locking; no immutable/nolock flags, checkpoint, or journal mode changes are used.
Read-only WAL access can still create/update SQLite's SHM coordination file.
"""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
from pathlib import Path

import accounts
import crypto


def _driver():
    try:
        from sqlcipher3 import dbapi2
    except ImportError as exc:
        raise RuntimeError(
            'Native SQLCipher is unavailable; install requirements-windows.txt '
            '(sqlcipher3==0.6.4). For an offline mirror select WECHAT_DB_BACKEND=sqlite3.'
        ) from exc
    return dbapi2


@lru_cache(maxsize=128)
def _derive(raw_key: bytes, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac('sha512', raw_key, salt, accounts.KDF_ITER, 32).hex()


@contextmanager
def connection(db_path: str, key: str | None = None):
    """Open one bounded, read-only session; close even on initialization failure.

    Each SELECT gets an engine snapshot. A streaming SELECT keeps that snapshot
    until consumed or explicitly closed. No retry is performed after rows escape.
    """
    driver = _driver()
    path = Path(db_path).resolve()
    raw = bytes.fromhex(crypto.load_key() if key is None else key)
    if len(raw) != 32:
        raise ValueError('Account key must contain 32 bytes')
    with path.open('rb') as handle:
        salt = handle.read(accounts.SALT_SIZE)
    if len(salt) != accounts.SALT_SIZE:
        raise ValueError('Encrypted database header is truncated')
    derived = _derive(raw, salt)
    con = driver.connect(path.as_uri() + '?mode=ro', uri=True,
                         isolation_level=None, timeout=1.0)
    try:
        if not con.execute('PRAGMA cipher_version').fetchone():
            raise RuntimeError('SQLCipher codec is missing from the native driver')
        con.execute(f'PRAGMA key="x\'{derived}\'"')
        con.execute('PRAGMA cipher_compatibility=4')
        con.execute('PRAGMA cipher_page_size=4096')
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA temp_store=MEMORY')
        con.row_factory = driver.Row
        yield con
    finally:
        con.close()


def query(db_path: str, sql: str, key: str | None = None) -> list[dict]:
    with connection(db_path, key) as con:
        return [dict(row) for row in con.execute(sql).fetchall()]


def iter_query(db_path: str, sql: str, batch_size: int = 500):
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('batch_size must be a positive integer')
    with connection(db_path) as con:
        cursor = con.execute(sql)
        try:
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield dict(row)
        finally:
            cursor.close()
