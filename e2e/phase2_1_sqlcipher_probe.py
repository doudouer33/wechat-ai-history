"""SQLCipher read-only/WAL feasibility probe using synthetic data only.

Creates synthetic databases in --output-dir (default: ignored .local/phase2_1).
Requires the optional sqlcipher3 package; does not access WeChat or its key.
This is a bounded Phase 2.1 experiment, not an incremental-sync module.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--module-dir',type=Path,default=ROOT/'.local/phase2_1/vendor')
parser.add_argument('--output-dir',type=Path,default=ROOT/'.local/phase2_1')
args=parser.parse_args()
OUT=args.output_dir.resolve()
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(args.module_dir.resolve()))
from sqlcipher3 import dbapi2 as sc

CASE=OUT/'synthetic'/uuid.uuid4().hex
CASE.mkdir(parents=True)
DB=CASE/'sample.db'
KEY='42'*32  # public synthetic encryption key, not a WeChat key
def open_db(path,ro=False,immutable=False):
    uri=path.resolve().as_uri()+('?mode=ro' if ro else '?mode=rwc')+('&immutable=1' if immutable else '')
    c=sc.connect(uri,uri=True,isolation_level=None,timeout=.05)
    c.execute('PRAGMA key="x\''+KEY+'\'"')
    c.execute('PRAGMA cipher_compatibility=4')
    c.execute('PRAGMA busy_timeout=50')
    if ro:c.execute('PRAGMA query_only=ON')
    return c
def n(c):return c.execute('SELECT count(*) FROM sample').fetchone()[0]
def h(p):return hashlib.sha256(p.read_bytes()).hexdigest()

w=open_db(DB)
assert w.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
w.execute('PRAGMA wal_autocheckpoint=0')
w.execute('PRAGMA cache_size=8')
w.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, body TEXT)')
w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
r=open_db(DB,ro=True)
assert n(r)==0
initial_main=h(DB)
w.execute('BEGIN IMMEDIATE')
w.executemany('INSERT INTO sample VALUES (?,?)',[(i,'synthetic-'+str(i)+'x'*2000) for i in range(1,101)])
wal=Path(str(DB)+'-wal')
uncommitted_wal_bytes=wal.stat().st_size
uncommitted_hidden=n(r)==0
w.execute('COMMIT')
assert n(r)==100
committed_wal_visible=h(DB)==initial_main
v1=r.execute('PRAGMA data_version').fetchone()[0]
r.execute('BEGIN')
assert n(r)==100
w.execute('BEGIN IMMEDIATE')
w.execute("UPDATE sample SET body='synthetic-update' WHERE id=1")
w.execute("INSERT INTO sample VALUES (101,'synthetic-new')")
w.execute('COMMIT')
snapshot_stable=n(r)==100 and r.execute('SELECT body FROM sample WHERE id=1').fetchone()[0].startswith('synthetic-1x')
blocked=w.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
r.execute('COMMIT')
new_snapshot_visible=n(r)==101 and r.execute('SELECT body FROM sample WHERE id=1').fetchone()[0]=='synthetic-update'
v2=r.execute('PRAGMA data_version').fetchone()[0]
truncate=w.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
truncate_bytes=wal.stat().st_size
w.execute("UPDATE sample SET body='synthetic-generation-a' WHERE id=1")
old_header=wal.read_bytes()[:32];old_size=wal.stat().st_size
restart=w.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()
w.execute("UPDATE sample SET body='synthetic-generation-b' WHERE id=1")
new_header=wal.read_bytes()[:32];new_size=wal.stat().st_size
reset_visible=r.execute('SELECT body FROM sample WHERE id=1').fetchone()[0]=='synthetic-generation-b'
write_blocked=False
r.execute('PRAGMA query_only=OFF')
try:r.execute("INSERT INTO sample VALUES (102,'must-not-write')")
except sc.OperationalError as exc:write_blocked='readonly' in str(exc).lower() or 'read-only' in str(exc).lower()
r.execute('PRAGMA query_only=ON')

# Existing read-only mode can create SHM on a private copy when it is missing.
copy_dir=CASE/'copied-pair';copy_dir.mkdir()
copy_db=copy_dir/'sample.db'
shutil.copy2(DB,copy_db);shutil.copy2(wal,Path(str(copy_db)+'-wal'))
copy_main_before=h(copy_db);copy_wal_before=h(Path(str(copy_db)+'-wal'))
copied=open_db(copy_db,ro=True)
copy_count=n(copied)
sidecar_created=Path(str(copy_db)+'-shm').exists()
copied.close()
copy_data_unchanged=copy_main_before==h(copy_db) and copy_wal_before==h(Path(str(copy_db)+'-wal'))

# immutable intentionally demonstrates why it is invalid for live WAL databases.
im=open_db(DB,ro=True,immutable=True)
immutable_body=im.execute('SELECT body FROM sample WHERE id=1').fetchone()[0]
im.close()
immutable_stale=immutable_body!='synthetic-generation-b'

times=[];cpu_start=time.process_time();started=time.perf_counter()
for _ in range(1000):
    t=time.perf_counter();r.execute('PRAGMA data_version').fetchone();times.append((time.perf_counter()-t)*1000)
poll_wall=time.perf_counter()-started;poll_cpu=time.process_time()-cpu_start
r.close();r=open_db(DB,ro=True)
reopen_latest=n(r)==101 and r.execute('SELECT body FROM sample WHERE id=1').fetchone()[0]=='synthetic-generation-b'
reopen_version=r.execute('PRAGMA data_version').fetchone()[0]
result={'sqlite_version':sc.sqlite_version,'sqlcipher_version':r.execute('PRAGMA cipher_version').fetchone()[0],
        'uncommitted_wal_bytes':uncommitted_wal_bytes,'uncommitted_hidden':uncommitted_hidden,
        'committed_wal_visible_main_unchanged':committed_wal_visible,'snapshot_stable_during_commit':snapshot_stable,
        'checkpoint_with_active_reader':blocked,'new_snapshot_visible':new_snapshot_visible,
        'truncate_after_reader_release':truncate,'wal_bytes_after_truncate':truncate_bytes,
        'restart_result':restart,'wal_reset_salts_changed':old_header[16:24]!=new_header[16:24],
        'wal_size_before_reset':old_size,'wal_size_after_reset':new_size,'reset_data_visible':reset_visible,
        'uri_mode_ro_blocks_write_even_without_query_only':write_blocked,
        'readonly_private_copy_creates_shm':sidecar_created,'readonly_private_copy_main_wal_unchanged':copy_data_unchanged,
        'private_copy_count':copy_count,'immutable_is_stale':immutable_stale,
        'data_versions_same_connection':[v1,v2],'data_version_after_reopen':reopen_version,'reopen_reads_latest':reopen_latest,
        'data_version_1000_calls_wall_ms':poll_wall*1000,'data_version_1000_calls_cpu_ms':poll_cpu*1000,
        'data_version_call_median_ms':statistics.median(times)}
assert all([uncommitted_hidden,committed_wal_visible,snapshot_stable,new_snapshot_visible,blocked[0]==1,truncate[0]==0,truncate_bytes==0,old_header[16:24]!=new_header[16:24],reset_visible,write_blocked,sidecar_created,copy_data_unchanged,immutable_stale,v1!=v2,reopen_latest])
r.close();w.close()
(OUT/'synthetic-summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
