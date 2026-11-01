"""Incremental API/CLI/MCP tests on synthetic SQLCipher databases only."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import contacts
import db
import incremental_cursor as cursors
import query
from test_native_reader import KEY, live, sc, writer  # noqa: F401 (shared codec fixtures)

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self, root, driver, account="wxid_testchanges_device"):
        self.root = root
        self.driver = driver
        self.storage = root / account / 'db_storage'
        self.writers = {}
        (self.storage / 'message').mkdir(parents=True)
        (self.storage / 'contact').mkdir()
        self.contact = writer(driver, self.storage / 'contact' / 'contact.db')
        self.contact.execute('CREATE TABLE contact(username, alias, nick_name, remark, local_type)')
        self.contact.execute("INSERT INTO contact VALUES ('wxid_friendchanges', '', 'Synthetic Friend', '', 3)")
        self.table = self.table_in(0, 'wxid_friendchanges')

    def table_in(self, shard, user):
        if shard not in self.writers:
            con = writer(self.driver, self.storage / 'message' / f'message_{shard}.db')
            con.execute('CREATE TABLE Name2Id(user_name TEXT UNIQUE)')
            self.writers[shard] = con
        con = self.writers[shard]
        con.execute('INSERT OR IGNORE INTO Name2Id VALUES (?)', (user,))
        table = 'Msg_' + hashlib.md5(user.encode()).hexdigest()
        con.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY, server_id INTEGER, '
                    'create_time INTEGER, local_type INTEGER, real_sender_id INTEGER, message_content BLOB)')
        return table

    def insert(self, local_id, shard=0, table=None, ts=None, server_id=0, body=None, kind=1):
        self.writers[shard].execute(
            f'INSERT INTO {table or self.table} VALUES (?, ?, ?, ?, 1, ?)',
            (local_id, server_id, int(time.time())-10 if ts is None else ts, kind,
             f'synthetic-{local_id}' if body is None else body))

    def close(self):
        self.contact.close()
        for con in self.writers.values():
            con.close()
        self.writers.clear()


@pytest.fixture
def store(live, sc):
    store = Store(live, sc)
    yield store
    store.close()


def mark(cursor, shard, table):
    return cursors.decode(cursor)['databases'][f'message/message_{shard}.db']['tables'][table]


def test_initialization_skips_history_and_does_not_read_bodies(store, monkeypatch):
    store.insert(40, body='historical synthetic body')
    monkeypatch.setattr(query, '_fmt_msg', lambda _: pytest.fail('baseline must not format history'))
    result = query.read_new_messages()
    assert result['messages'] == [] and result['count'] == 0 and result['has_more'] is False
    assert mark(result['next_cursor'], 0, store.table) == 40
    decoded = base64.urlsafe_b64decode(result['next_cursor'] + '=' * (-len(result['next_cursor']) % 4)).decode()
    assert KEY not in decoded and str(store.root) not in decoded
    assert 'historical synthetic body' not in decoded and 'wxid_testchanges' not in decoded
    assert 'salt' not in decoded


def test_new_same_second_null_zero_replay_and_no_duplicates(store):
    cursor = query.read_new_messages()['next_cursor']
    for i, server_id in [(1, None), (2, 0), (3, 99)]:
        store.insert(i, ts=100, server_id=server_id)
    result = query.read_new_messages(cursor)
    assert [m['local_id'] for m in result['messages']] == [1, 2, 3]
    assert [m['server_id'] for m in result['messages']] == [None, 0, 99]
    assert result['messages'][0]['source_db'] == 'message/message_0.db'
    assert result['messages'][0]['source_table'] == store.table
    assert result['messages'][0]['contact'] == 'Synthetic Friend'
    assert result['messages'][0]['direction'] == '[对方]'
    assert query.read_new_messages(cursor) == result  # caller retries without acknowledging
    empty = query.read_new_messages(result['next_cursor'])
    assert empty['messages'] == [] and empty['has_more'] is False
    assert empty['next_cursor'] == result['next_cursor']


def test_global_paging_preserves_all_table_prefixes_despite_timestamp_disorder(store):
    other = store.table_in(0, 'wxid_otherchanges')
    remote = store.table_in(12, 'wxid_friendchanges')
    cursor = query.read_new_messages()['next_cursor']
    expected = set()
    for shard, table in [(0, store.table), (0, other), (12, remote)]:
        for i in range(1, 5):
            store.insert(i, shard, table, ts=1000-i)
            expected.add((f'message/message_{shard}.db', table, i))
    found = []
    first = query.read_new_messages(cursor, 2)
    assert first['count'] == 2 and first['has_more'] is True
    # Candidates not returned keep their exact prior marks.
    decoded = cursors.decode(first['next_cursor'])
    returned = {(m['source_db'], m['source_table']): m['local_id'] for m in first['messages']}
    for source, state in decoded['databases'].items():
        for table, high in state['tables'].items():
            assert high == returned.get((source, table), 0)
    for _ in range(10):
        result = query.read_new_messages(cursor, 2)
        assert [m['_ts'] for m in result['messages']] == sorted(m['_ts'] for m in result['messages'])
        found.extend((m['source_db'], m['source_table'], m['local_id']) for m in result['messages'])
        cursor = result['next_cursor']
        if not result['has_more']:
            break
    assert len(found) == len(expected) and set(found) == expected
    assert query.read_new_messages(cursor)['count'] == 0


def test_new_tables_and_shards_start_at_zero_and_keep_unreturned_marks(store):
    store.insert(500)
    cursor = query.read_new_messages()['next_cursor']
    table = store.table_in(0, 'wxid_othernew')
    remote = store.table_in(10, 'wxid_friendchanges')
    store.insert(1, table=table)
    store.insert(1, shard=10, table=remote)
    result = query.read_new_messages(cursor, 1)
    assert result['count'] == 1 and result['has_more']
    assert mark(result['next_cursor'], 0, store.table) == 500
    new_marks = [mark(result['next_cursor'], 0, table), mark(result['next_cursor'], 10, remote)]
    assert sorted(new_marks) == [0, 1]
    tail = query.read_new_messages(result['next_cursor'], 1)
    assert tail['count'] == 1 and not tail['has_more']


def test_uncommitted_hidden_and_formatting_failure_does_not_advance(store, monkeypatch):
    cursor = query.read_new_messages()['next_cursor']
    con = store.writers[0]
    con.execute('BEGIN')
    store.insert(1)
    assert query.read_new_messages(cursor)['count'] == 0
    con.execute('COMMIT')
    fmt = query._fmt_msg
    monkeypatch.setattr(query, '_fmt_msg', lambda _: (_ for _ in ()).throw(RuntimeError('synthetic formatting failure')))
    with pytest.raises(RuntimeError):
        query.read_new_messages(cursor)
    monkeypatch.setattr(query, '_fmt_msg', fmt)
    assert query.read_new_messages(cursor)['messages'][0]['local_id'] == 1


def test_preserves_existing_compressed_rich_media_system_formatting(store):
    import zstandard
    cursor = query.read_new_messages()['next_cursor']
    cases = [
        (1, zstandard.ZstdCompressor().compress('synthetic text\nfull body'.encode())),
        (49, '<msg><appmsg><title>Synthetic card</title><type>5</type><url>https://example.com</url></appmsg></msg>'),
        (3, '<msg><img length="123" /></msg>'),
        (10000, 'synthetic system text'),
    ]
    for i, (kind, body) in enumerate(cases, 1):
        store.insert(i, body=body, kind=kind, ts=100)
    result = query.read_new_messages(cursor)
    for item, (kind, body) in zip(result['messages'], cases):
        expected = query._fmt_msg({'create_time': 100, 'local_type': kind,
                                   'message_body': body, 'sender_wxid': 'wxid_friendchanges'})
        assert all(item[key] == value for key, value in expected.items())
    assert result['messages'][0]['content'] == 'synthetic text\nfull body'
    assert result['messages'][1]['app'] and result['messages'][2]['xml']
    assert result['messages'][3]['is_system']


def test_lower_ids_and_old_row_updates_are_outside_append_semantics(store):
    store.insert(10)
    cursor = query.read_new_messages()['next_cursor']
    store.insert(2)
    store.writers[0].execute(f"UPDATE {store.table} SET message_content='changed' WHERE local_id=10")
    result = query.read_new_messages(cursor)
    assert result['count'] == 0 and result['next_cursor'] == cursor


@pytest.mark.parametrize('schema', ['duplicate_ids', 'nonpositive'])
def test_unsupported_local_id_schema_is_rejected(store, schema):
    if schema == 'duplicate_ids':
        store.writers[0].execute('CREATE TABLE Msg_duplicate(local_id INTEGER, message_content)')
    else:
        store.insert(0)
    with pytest.raises(cursors.CursorError, match='local_id'):
        query.read_new_messages()


def test_no_checkpoint_writes_or_leaked_read_handles(store, monkeypatch):
    import native_reader
    from contextlib import contextmanager
    cursor = query.read_new_messages()['next_cursor']
    store.insert(1)
    path = store.storage / 'message' / 'message_0.db'
    wal = Path(str(path) + '-wal')
    before = path.read_bytes(), wal.read_bytes()
    original = native_reader.connection
    opened = []
    traces = []
    @contextmanager
    def tracked(*args, **kwargs):
        with original(*args, **kwargs) as con:
            opened.append(con)
            con.set_trace_callback(traces.append)
            yield con
    monkeypatch.setattr(native_reader, 'connection', tracked)
    assert query.read_new_messages(cursor)['count'] == 1
    assert (path.read_bytes(), wal.read_bytes()) == before
    assert not any('checkpoint' in sql.lower() for sql in traces)
    assert opened
    for con in opened:
        with pytest.raises(store.driver.ProgrammingError, match='closed'):
            con.execute('SELECT 1')


@pytest.mark.parametrize('bad', ['', '*bad*', 'e30', 'a', None, 123, pytest.param('A' * (cursors.MAX_CURSOR_SIZE+1), id='oversized')])
def test_bad_cursor_rejected_without_accessing_source(monkeypatch, bad):
    if bad is None:
        bad = base64.urlsafe_b64encode(b'null').decode().rstrip('=')
    monkeypatch.setattr(db, 'find_data_dir', lambda: pytest.fail('must reject cursor before reading'))
    with pytest.raises(cursors.CursorError):
        query.read_new_messages(bad)


@pytest.mark.parametrize('change', ['version', 'account', 'negative', 'boolean', 'path', 'table', 'checksum', 'extra'])
def test_cursor_fields_and_account_checked(store, change):
    original = query.read_new_messages()['next_cursor']
    state = cursors.decode(original)
    database = state['databases']['message/message_0.db']
    if change == 'version':
        state['version'] = 100
    elif change == 'account':
        state['account'] = '0' * 64
    elif change in ('negative', 'boolean'):
        database['tables'][store.table] = -1 if change == 'negative' else True
    elif change == 'path':
        state['databases']['D:/private/path.db'] = state['databases'].pop('message/message_0.db')
    elif change == 'table':
        database['tables']['../private'] = 0
    elif change == 'extra':
        state['raw_key'] = 'not allowed'
    encoded = cursors.encode(state)
    if change == 'checksum':
        raw = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        raw['account'] = '0' * 64
        encoded = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip('=')
    with pytest.raises(cursors.CursorError):
        query.read_new_messages(encoded)


def test_actual_account_switch_and_salt_replacement_fail(store, monkeypatch):
    import config
    cursor = query.read_new_messages()['next_cursor']
    # A different account directory using the same synthetic key must still fail.
    alternate = Store(store.root / 'alternate', store.driver, 'wxid_otheraccount_device')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, 'WECHAT_DATA_GLOB', str(alternate.root / '*' / 'db_storage'))
            db.reset_caches()
            with pytest.raises(cursors.CursorError, match='account'):
                query.read_new_messages(cursor)
    finally:
        alternate.close()
        db.reset_caches()
    replacement = store.root / 'replacement.db'
    w = writer(store.driver, replacement)
    w.execute('CREATE TABLE Name2Id(user_name)')
    w.close()
    store.writers.pop(0).close()
    os.replace(replacement, store.storage / 'message' / 'message_0.db')
    with pytest.raises(cursors.CursorError, match='generation'):
        query.read_new_messages(cursor)


@pytest.mark.parametrize('mutation', ['drop_table', 'delete_max', 'drop_shard'])
def test_disappeared_sources_and_id_regression_fail(store, mutation):
    store.insert(10)
    remote = store.table_in(1, 'wxid_friendchanges')
    cursor = query.read_new_messages()['next_cursor']
    if mutation == 'drop_table':
        store.writers[0].execute(f'DROP TABLE {store.table}')
    elif mutation == 'delete_max':
        store.writers[0].execute(f'DELETE FROM {store.table}')
    else:
        store.writers.pop(1).close()
        (store.storage / 'message' / 'message_1.db').unlink()
    with pytest.raises(cursors.CursorError):
        query.read_new_messages(cursor)


@pytest.mark.parametrize('limit', [0, -1, True, '2', 2001])
def test_invalid_limit_before_database_access(monkeypatch, limit):
    monkeypatch.setattr(db, 'find_data_dir', lambda: pytest.fail('must validate limit first'))
    with pytest.raises(ValueError, match='limit'):
        query.read_new_messages(limit=limit)


def subprocess_environment(store):
    """Redirect the real CLI and all its subprocesses to a synthetic account."""
    key = store.root / 'synthetic.key'
    key.write_text(KEY, encoding='ascii')
    bootstrap = store.root / 'bootstrap'
    bootstrap.mkdir(exist_ok=True)
    (bootstrap / 'sitecustomize.py').write_text(
        'import config\n'
        "config.DB_BACKEND = 'sqlcipher_native'\n"
        f'config.KEY_FILE = {str(key)!r}\n'
        f'config.CONTACTS_FILE = {str(store.root / "overrides.json")!r}\n'
        f'config.WECHAT_DATA_GLOB = {str(store.root / "*" / "db_storage")!r}\n'
        f'config.DECRYPTED_DIR = {str(store.root / "empty-mirror")!r}\n', encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(bootstrap), str(ROOT)]),
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', WECHAT_DB_BACKEND='sqlcipher_native')
    env.pop('WECHAT_ACCOUNT', None)
    return env


def run_cli(store, *args):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/common/query.py'), *args],
                            env=subprocess_environment(store), capture_output=True, text=True,
                            encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_restart_from_saved_cursor_and_mcp_registration(store):
    store.insert(1)
    first = run_cli(store, 'changes', '--json')
    assert first['count'] == 0
    saved = store.root / 'caller.cursor'
    saved.write_text(first['next_cursor'], encoding='ascii')
    store.insert(2)
    second = run_cli(store, 'changes', '--cursor', saved.read_text(), '--limit', '1', '--json')
    assert [m['local_id'] for m in second['messages']] == [2]
    store.insert(3)
    third = run_cli(store, 'changes', '--cursor', second['next_cursor'], '--json')
    assert [m['local_id'] for m in third['messages']] == [3]
    import server
    tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert tools == {'wechat_list_accounts', 'wechat_list_chats', 'wechat_read_chat',
                     'wechat_recent_messages', 'wechat_search_messages', 'wechat_chat_summary',
                     'wechat_system_events', 'wechat_read_new_messages'}
    assert server.wechat_read_new_messages(third['next_cursor'])['count'] == 0
    async def call():
        return await server.mcp.call_tool('wechat_read_new_messages', {'cursor': second['next_cursor'], 'limit': 1})
    response = asyncio.run(call())
    # FastMCP returns content blocks and a structured result for dict tools.
    text = str(response)
    assert 'source_db' in text and 'local_id' in text


def test_existing_e2e_on_synthetic_account(store):
    store.insert(1, body='合成的消息')
    result = subprocess.run([sys.executable, str(ROOT / 'e2e/test_e2e.py')],
                            env=subprocess_environment(store), capture_output=True, text=True,
                            encoding='utf-8', timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '13/13 PASS' in result.stdout
