"""Real codec tests: every database and key here is synthetic."""
import hashlib
import multiprocessing
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import config
import contacts
import crypto
import db
import native_reader

KEY = '42' * 32


@pytest.fixture
def sc():
    if sys.platform == 'win32':
        from sqlcipher3 import dbapi2 as driver
    else:
        driver = pytest.importorskip('sqlcipher3.dbapi2')
    return driver


def writer(driver, path, key=KEY):
    path = Path(path)
    salt = path.read_bytes()[:16] if path.exists() else os.urandom(16)
    derived = hashlib.pbkdf2_hmac('sha512', bytes.fromhex(key), salt, 256000, 32)
    con = driver.connect(str(path), isolation_level=None, timeout=.2)
    con.execute(f'PRAGMA key="x\'{derived.hex() + salt.hex()}\'"')
    con.execute('PRAGMA cipher_compatibility=4')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA wal_autocheckpoint=0')
    return con


@pytest.fixture
def live(monkeypatch, tmp_path, sc):
    monkeypatch.setattr(config, 'DB_BACKEND', 'sqlcipher_native')
    monkeypatch.setattr(crypto, 'load_key', lambda: KEY)
    monkeypatch.setattr(config, 'CONTACTS_FILE', str(tmp_path / 'contacts.json'))
    monkeypatch.setattr(config, 'WECHAT_DATA_GLOB', str(tmp_path / '*' / 'db_storage'))
    monkeypatch.delenv('WECHAT_ACCOUNT', raising=False)
    db.reset_caches()
    contacts.invalidate_cache()
    native_reader._derive.cache_clear()
    yield tmp_path
    contacts.invalidate_cache()
    db.reset_caches()
    native_reader._derive.cache_clear()


def count(path):
    return db.query(str(path), 'SELECT count(*) AS n FROM sample')[0]['n']


def test_committed_wal_rollback_snapshot_and_readonly(live, sc):
    path = live / 'synthetic # %.db'
    w = writer(sc, path)
    try:
        w.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, body BLOB)')
        w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = path.read_bytes()
        w.execute('BEGIN IMMEDIATE')
        w.executemany('INSERT INTO sample VALUES (?, ?)', [(i, b'x' * 9000) for i in range(100)])
        assert count(path) == 0
        w.execute('COMMIT')
        assert count(path) == 100
        assert path.read_bytes() == before  # New rows exist only in WAL.
        stream = db.iter_query(str(path), 'SELECT * FROM sample ORDER BY id', 1)
        assert next(stream)['id'] == 0
        w.execute("UPDATE sample SET body=X'00' WHERE id=99")
        assert list(stream)[-1]['body'] == b'x' * 9000
        assert db.query(str(path), 'SELECT body FROM sample WHERE id=99') == [{'body': b'\0'}]
        w.execute('BEGIN')
        w.execute('DELETE FROM sample')
        assert count(path) == 100
        w.execute('ROLLBACK')
        assert count(path) == 100
        wal = Path(str(path) + '-wal')
        hashes = (path.read_bytes(), wal.read_bytes())
        with pytest.raises(sc.OperationalError, match='readonly|read-only'):
            db.query(str(path), 'DELETE FROM sample')
        with native_reader.connection(str(path)) as r:
            r.execute('PRAGMA query_only=OFF')
            with pytest.raises(sc.OperationalError, match='readonly|read-only'):
                r.execute('DELETE FROM sample')
            assert r.execute('PRAGMA temp_store').fetchone()[0] == 2
        assert (path.read_bytes(), wal.read_bytes()) == hashes
        assert db.query_raw(str(path), 'SELECT 1, NULL, 2') == ['1||2']
        assert native_reader._derive.cache_info().hits > 0
    finally:
        w.close()


@pytest.mark.parametrize('checkpoint', ['PASSIVE', 'RESTART', 'TRUNCATE'])
def test_checkpoint_reset_writer_and_reader_restart(live, sc, checkpoint):
    path = live / 'restart.db'
    w = writer(sc, path)
    try:
        w.execute('CREATE TABLE sample(body TEXT)')
        w.execute("INSERT INTO sample VALUES ('first')")
        assert count(path) == 1
        assert w.execute(f'PRAGMA wal_checkpoint({checkpoint})').fetchone()[0] == 0
        w.execute("INSERT INTO sample VALUES ('second')")
        assert count(path) == 2
    finally:
        w.close()
    db.reset_caches()
    assert count(path) == 2
    w = writer(sc, path)
    try:
        w.execute("INSERT INTO sample VALUES ('restart')")
        assert count(path) == 3
    finally:
        w.close()


def test_early_close_releases_checkpoint_and_parallel_queries(live, sc):
    path = live / 'close.db'
    w = writer(sc, path)
    try:
        w.execute('CREATE TABLE sample(id)')
        w.executemany('INSERT INTO sample VALUES (?)', [(i,) for i in range(5)])
        stream = db.iter_query(str(path), 'SELECT * FROM sample', 1)
        next(stream)
        w.execute('INSERT INTO sample VALUES (5)')
        assert w.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 1
        stream.close()
        assert w.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        with ThreadPoolExecutor(4) as pool:
            assert list(pool.map(lambda _: count(path), range(12))) == [6] * 12
        with pytest.raises(sc.OperationalError):
            list(db.iter_query(str(path), 'SELECT nonexistent FROM sample'))
        assert count(path) == 6
    finally:
        w.close()


def test_missing_wrong_key_corrupt_replaced_and_locked(live, sc, monkeypatch):
    path = live / 'failure.db'
    with pytest.raises(FileNotFoundError):
        count(path)
    assert not path.exists()
    path.write_bytes(b'truncated')
    with pytest.raises(ValueError, match='truncated'):
        count(path)
    path.unlink()
    w = writer(sc, path)
    w.execute('CREATE TABLE sample(id)')
    w.close()
    monkeypatch.setattr(crypto, 'load_key', lambda: '43' * 32)
    with pytest.raises(sc.DatabaseError):
        count(path)
    monkeypatch.setattr(crypto, 'load_key', lambda: KEY)
    assert count(path) == 0
    replacement = live / 'replacement.db'
    w = writer(sc, replacement)
    w.execute('CREATE TABLE sample(id)')
    w.execute('INSERT INTO sample VALUES (1)')
    w.close()
    os.replace(replacement, path)
    assert count(path) == 1  # new salt, same path
    w = writer(sc, path)
    try:
        w.execute('PRAGMA journal_mode=DELETE')
        w.execute('BEGIN EXCLUSIVE')
        with pytest.raises(sc.OperationalError, match='locked'):
            count(path)
        w.execute('ROLLBACK')
        assert count(path) == 1
    finally:
        w.close()
    path.write_bytes(os.urandom(4096))
    with pytest.raises(sc.DatabaseError):
        count(path)


def _process_writer(path, pipe):
    from sqlcipher3 import dbapi2
    w = writer(dbapi2, path)
    try:
        w.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, body TEXT)')
        w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        pipe.send('ready')
        for i in range(10):
            assert pipe.recv() == 'write'
            w.execute('BEGIN')
            w.execute('INSERT INTO sample VALUES (?, ?)', (i, 'text' * 3000))
            pipe.send('uncommitted')
            assert pipe.recv() == 'commit'
            w.execute('COMMIT')
            pipe.send('committed')
        assert pipe.recv() == 'checkpoint'
        pipe.send(w.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0])
    finally:
        w.close()
        pipe.close()


def test_independent_writer_process(live, sc):
    path = live / 'process.db'
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_process_writer, args=(str(path), child))
    process.start()
    child.close()
    def receive():
        assert parent.poll(20), 'writer timed out'
        return parent.recv()
    try:
        assert receive() == 'ready'
        for i in range(10):
            parent.send('write')
            assert receive() == 'uncommitted'
            assert count(path) == i
            parent.send('commit')
            assert receive() == 'committed'
            assert db.query(str(path), 'SELECT id, body FROM sample ORDER BY id') == [
                {'id': n, 'body': 'text' * 3000} for n in range(i + 1)]
        parent.send('checkpoint')
        assert receive() == 0
        process.join(10)
        assert process.exitcode == 0
        assert count(path) == 10
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_live_existing_queries_shards_contacts_and_failure(live, sc):
    import query
    from conftest import SAMPLE_TABLE, SAMPLE_WXID
    storage = live / 'wxid_testsynthetic_device' / 'db_storage'
    (storage / 'message').mkdir(parents=True)
    (storage / 'contact').mkdir()
    c = writer(sc, storage / 'contact' / 'contact.db')
    now = int(time.time()) - 20
    writers = []
    try:
        c.execute('CREATE TABLE contact(username, alias, nick_name, remark, local_type)')
        c.execute('INSERT INTO contact VALUES (?, ?, ?, ?, ?)', (SAMPLE_WXID, '', 'first', '', 3))
        for shard, times in [(0, [now-3, now-1]), (10, [now-2, now])]:
            w = writer(sc, storage / 'message' / f'message_{shard}.db')
            writers.append(w)
            w.execute('CREATE TABLE Name2Id(user_name)')
            w.execute('INSERT INTO Name2Id VALUES (?)', (SAMPLE_WXID,))
            w.execute(f'CREATE TABLE {SAMPLE_TABLE}(local_id, server_id, create_time, local_type, real_sender_id, message_content)')
            for i, ts in enumerate(times):
                w.execute(f'INSERT INTO {SAMPLE_TABLE} VALUES (?, ?, ?, 1, 1, ?)', (i, i+100, ts, f'hello-{ts}'))
            assert len(db.get_message_dbs()) == len(writers)  # discover new shard without reset
        assert query.list_chats() == [{'wxid': SAMPLE_WXID, 'display': 'first'}]
        c.execute("UPDATE contact SET nick_name='latest'")
        assert query.list_chats()[0]['display'] == 'latest'
        msgs = query.read_chat(SAMPLE_WXID, 3, 1)['chats'][0]['messages']
        assert [m['_ts'] for m in msgs] == [now-2, now-1, now]
        assert len(query.summary(1)['conversations'][0]['messages']) == 4
        assert [m['_ts'] for m in query.search('hello', 1, 3)['messages']] == [now, now-1, now-2]
        assert query.recent(1, 2)['total'] == 2
        import zstandard
        body = zstandard.ZstdCompressor().compress('compressed synthetic keyword'.encode())
        writers[-1].execute(f'INSERT INTO {SAMPLE_TABLE} VALUES (20, 200, ?, 1, 1, ?)', (now+1, body))
        result = query.search('synthetic keyword', 1, 3)
        assert result['count'] == 1
        assert result['messages'][0]['_ts'] == now+1
        writers[-1].close()
        writers.pop()
        (storage / 'message' / 'message_10.db').unlink()
        assert len(db.get_message_dbs()) == 1
        (storage / 'message' / 'message_11.db').write_bytes(b'bad')
        with pytest.raises(ValueError):
            query.list_chats()  # never silently skip a damaged shard
    finally:
        contacts.invalidate_cache()
        c.close()
        for w in writers:
            w.close()


def test_dependency_and_codec_errors(monkeypatch, tmp_path, sc):
    import builtins
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'sqlcipher3':
            raise ImportError('synthetic missing dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    with pytest.raises(RuntimeError, match='requirements-windows'):
        native_reader._driver()
    monkeypatch.setattr(builtins, '__import__', original)
    import sqlite3
    path = tmp_path / 'no-codec.db'
    path.write_bytes(b'x' * 4096)
    monkeypatch.setattr(native_reader, '_driver', lambda: sqlite3)
    with pytest.raises(RuntimeError, match='codec'):
        native_reader.query(str(path), 'SELECT 1', KEY)


def test_equal_size_wal_reset_and_full_decryption_parity(live, sc):
    import sqlite3
    from scripts.windows import decrypt_all
    path = live / 'reset.db'
    w = writer(sc, path)
    try:
        w.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, body TEXT)')
        w.execute("INSERT INTO sample VALUES (1, 'initial')")
        w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        w.execute("UPDATE sample SET body='generation-a'")
        assert db.query(str(path), 'SELECT * FROM sample')[0]['body'] == 'generation-a'
        wal = Path(str(path) + '-wal')
        old = wal.read_bytes()
        assert w.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
        w.execute("UPDATE sample SET body='generation-b'")
        new = wal.read_bytes()
        assert len(old) == len(new)
        assert old[16:24] != new[16:24]
        rows = db.query(str(path), 'SELECT * FROM sample')
        assert rows == [{'id': 1, 'body': 'generation-b'}]
        plain = live / 'offline.db'
        assert decrypt_all.decrypt_db(bytes.fromhex(KEY), str(path), str(plain))
        with sqlite3.connect(plain) as con:
            con.row_factory = sqlite3.Row
            assert [dict(row) for row in con.execute('SELECT * FROM sample')] == rows
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        w.close()


def test_account_selection_and_unavailable_pinned_source(live, sc, monkeypatch):
    roots = []
    for account in ('wxid_testone_device', 'wxid_testtwo_device'):
        storage = live / account / 'db_storage'
        (storage / 'message').mkdir(parents=True)
        w = writer(sc, storage / 'message' / 'message_12.db')
        w.execute('CREATE TABLE Name2Id(user_name)')
        w.close()
        roots.append(storage)
    with pytest.raises(RuntimeError, match='uniquely'):
        db.find_data_dir()
    monkeypatch.setenv('WECHAT_ACCOUNT', 'wxid_testone')
    assert db.find_data_dir() == str(roots[0])
    roots[0].rename(roots[0].with_name('unavailable'))
    with pytest.raises(FileNotFoundError, match='refusing to switch'):
        db.find_data_dir()


def test_contact_request_cache_releases_source_and_refreshes(live, sc, monkeypatch):
    path = live / 'contact.db'
    def create(path, name):
        w = writer(sc, path)
        w.execute('CREATE TABLE contact(username, alias, nick_name, remark, local_type)')
        w.execute('INSERT INTO contact VALUES (?, ?, ?, ?, ?)', ('friend', '', name, '', 3))
        w.close()
    create(path, 'first')
    monkeypatch.setattr(db, 'get_contact_db_path', lambda: str(path))
    @contacts.query_snapshot
    def request():
        assert contacts.resolve_contact_name('friend') == 'first'
        replacement = live / 'new.db'
        create(replacement, 'latest')
        os.replace(replacement, path)  # no persistent observer handle blocks replacement
        assert contacts.resolve_contact_name('friend') == 'first'
    request()
    assert contacts.resolve_contact_name('friend') == 'latest'
    @contacts.query_snapshot
    def failing_request():
        assert contacts.resolve_contact_name('friend') == 'latest'
        raise RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        failing_request()
    assert contacts._query_contacts.get() is None
