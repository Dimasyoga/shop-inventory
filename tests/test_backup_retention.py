"""backup.sh's retention policy.

The only test in the suite that drives a shell script, and it earns the exception:
everything else here can be wrong and be fixed, while a retention bug deletes the
thing you would have fixed it from. The rules being pinned are that age alone never
decides (a floor of recent copies always survives), that restore.sh's pre-restore
rescue copies are out of scope entirely, and that nothing is deleted on a run whose
own backup did not land.
"""
import os
import sqlite3
import subprocess
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_SH = os.path.join(ROOT, 'backup.sh')

DAY = 24 * 60 * 60


@pytest.fixture
def shop(tmp_path):
    """A scratch database for backup.sh to copy, outside the source tree."""
    path = tmp_path / 'shop.db'
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, created_at TEXT);
        CREATE TABLE order_items (id INTEGER PRIMARY KEY);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO products (name) VALUES ('Kopi');
    """)
    conn.commit()
    conn.close()
    return path


def aged(directory, name, days_old):
    """A backup file that looks `days_old` days old to find -mtime."""
    path = directory / name
    path.write_bytes(b'not really a database, never read by the pruner')
    when = time.time() - days_old * DAY
    os.utime(path, (when, when))
    return path


def run_backup(shop, out_dir, keep_days=30, keep_min=7, db_path=None):
    env = dict(os.environ,
               SHOP_DB_PATH=str(db_path or shop),
               BACKUP_KEEP_DAYS=str(keep_days),
               BACKUP_KEEP_MIN=str(keep_min))
    # cwd is the scratch dir so the `docker compose ps` probe finds no compose file
    # and takes the local branch, as it would on a machine without Docker.
    return subprocess.run(['bash', BACKUP_SH, str(out_dir)], env=env, cwd=str(out_dir),
                          capture_output=True, text=True)


def names(out_dir):
    return sorted(p.name for p in out_dir.iterdir())


def test_a_backup_is_written(shop, tmp_path):
    out = tmp_path / 'backups'
    out.mkdir()
    result = run_backup(shop, out)
    assert result.returncode == 0, result.stderr
    written = [n for n in names(out) if n.startswith('shop-')]
    assert len(written) == 1
    # ...and it is a real database, not an empty file.
    conn = sqlite3.connect(str(out / written[0]))
    assert conn.execute("SELECT name FROM products").fetchone()[0] == 'Kopi'
    conn.close()


def test_backups_past_the_cutoff_are_pruned(shop, tmp_path):
    out = tmp_path / 'backups'
    out.mkdir()
    for day in (40, 50, 60):
        aged(out, f'shop-2026{day:04d}-000000.db', day)
    # Eight recent ones, so the floor is already satisfied and age alone decides.
    for day in range(8):
        aged(out, f'shop-2026070{day}-120000.db', day)

    run_backup(shop, out, keep_days=30, keep_min=7)
    survivors = names(out)
    assert not [n for n in survivors if n.startswith('shop-2026004')]
    assert 'shop-20260050-000000.db' not in survivors
    assert 'shop-20260060-000000.db' not in survivors


def test_the_newest_survive_however_old_they_are(shop, tmp_path):
    """A shop that let the cron lapse for a year must not have the run that resumes
    it delete every backup it has."""
    out = tmp_path / 'backups'
    out.mkdir()
    for n in range(5):
        aged(out, f'shop-2025010{n}-000000.db', 300 + n)

    run_backup(shop, out, keep_days=30, keep_min=7)
    kept = [n for n in names(out) if n.startswith('shop-2025')]
    # Five old ones plus the fresh one is six, under the floor of seven: nothing goes.
    assert len(kept) == 5


def test_the_floor_keeps_the_newest_ones_specifically(shop, tmp_path):
    """Not just "three files survive" -- the three that survive must be the most
    recent three. Keeping an arbitrary trio would satisfy a count and lose the
    backups you would actually restore from."""
    out = tmp_path / 'backups'
    out.mkdir()
    for n in range(10):
        # All well past the cutoff, a day apart; n=0 is the newest of them.
        aged(out, f'shop-202601{n:02d}-000000.db', 100 + n)

    run_backup(shop, out, keep_days=30, keep_min=3)
    kept = sorted(n for n in names(out) if n.startswith('shop-202601'))
    # The run's own fresh backup takes the first of the three slots, leaving the two
    # newest of the old ones.
    assert kept == ['shop-20260100-000000.db', 'shop-20260101-000000.db']


def test_pre_restore_copies_are_never_touched(shop, tmp_path):
    """restore.sh writes these because someone was about to do something
    irreversible. Outliving a routine cleanup is the entire point of them."""
    out = tmp_path / 'backups'
    out.mkdir()
    aged(out, 'pre-restore-20240101-000000.db', 500)
    for n in range(9):
        aged(out, f'shop-202602{n:02d}-000000.db', 100 + n)

    run_backup(shop, out, keep_days=30, keep_min=1)
    assert 'pre-restore-20240101-000000.db' in names(out)


def test_nothing_is_pruned_when_the_backup_fails(shop, tmp_path):
    """Pruning is downstream of a backup that landed. A run that produced nothing
    must not also clear out the copies that would have covered for it."""
    out = tmp_path / 'backups'
    out.mkdir()
    for n in range(9):
        aged(out, f'shop-202603{n:02d}-000000.db', 100 + n)
    before = names(out)

    result = run_backup(shop, out, keep_days=30, keep_min=1,
                        db_path=tmp_path / 'does-not-exist.db')
    assert result.returncode != 0
    assert names(out) == before


def test_a_run_with_nothing_to_prune_says_nothing(shop, tmp_path):
    # A directory of recent backups and the default cutoff: nothing qualifies, and a
    # nightly cron log should stay quiet rather than reporting a no-op every day.
    out = tmp_path / 'backups'
    out.mkdir()
    for n in range(3):
        aged(out, f'shop-202607{n:02d}-000000.db', n)
    result = run_backup(shop, out)
    assert 'pruned' not in result.stdout.lower()
