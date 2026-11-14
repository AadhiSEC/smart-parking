"""Consistent SQLite backups and offline restore; standard library only."""
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile

VERSION = 3
# Parents precede children when importing into the application's current schema.
TABLES = ('customers', 'spaces', 'sessions', 'bookings', 'space_blocks',
          'admin_credentials', 'admin_sessions', 'admin_events', 'price_settings', 'booking_extensions', 'payments', 'waivers')


class BackupError(Exception):
    pass


class DatabaseLock:
    """One server or restore at a time, including across local processes."""
    def __init__(self, database):
        path = Path(database).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path.with_suffix(path.suffix + '.lock'), 'a+b')
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise BackupError('This database is in use. Stop the parking server with Ctrl+C before continuing.') from error

    def close(self):
        if not self.file.closed:
            self.file.close()  # Closing releases the operating-system lock.

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def readonly(path):
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def verify_database(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise BackupError('The database is damaged. No data was restored.')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise BackupError('The database contains broken record links. No data was restored.')


def unique_name(prefix='parking-backup'):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-UTC')
    return f'{prefix}-{stamp}-{secrets.token_hex(3)}.sqlite3'


def create_backup(database, destination):
    """Use SQLite's online backup API so committed WAL data is included."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an existing backup, even if its name was reused.
    with destination.open('xb'):
        pass
    try:
        with closing(readonly(database)) as source, closing(sqlite3.connect(destination)) as backup:
            source.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
            backup.execute('PRAGMA trusted_schema=OFF')
            verify_database(backup)
            backup.execute('CREATE TABLE IF NOT EXISTS parking_backup_meta(version INTEGER NOT NULL, created_at TEXT NOT NULL)')
            backup.execute('DELETE FROM parking_backup_meta')
            table_names={r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version=VERSION if 'waivers' in table_names else 2 if 'payments' in table_names else 1
            backup.execute('INSERT INTO parking_backup_meta VALUES (?,?)',
                           (version, datetime.now(timezone.utc).isoformat(timespec='seconds')))
            backup.commit()
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def download_backup(database):
    with tempfile.TemporaryDirectory(prefix='backup-download-', dir=Path(database).parent) as folder:
        output = create_backup(database, Path(folder) / 'parking.sqlite3')
        return output.read_bytes()


def prepare_restore(source_path, stage_path, store_factory):
    """Import values into a fresh trusted schema, never install uploaded SQL."""
    stage_store = store_factory(stage_path)
    try:
        with closing(readonly(source_path)) as source, stage_store.connect() as stage:
            source.execute('BEGIN')
            verify_database(source)
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            supported={1:set(TABLES)-{'payments','waivers'},2:set(TABLES)-{'waivers'},3:set(TABLES)}
            if tables not in [names | {'parking_backup_meta'} for names in supported.values()]:
                raise BackupError('Choose a backup made with Download database backup in this version of the parking app.')
            if source.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
                raise BackupError('This backup has unsupported database objects. No data was restored.')
            metadata = source.execute('SELECT version,created_at FROM parking_backup_meta').fetchall()
            if len(metadata) != 1 or metadata[0][0] not in supported:
                raise BackupError('This backup version is not supported.')
            if tables != supported[metadata[0][0]] | {'parking_backup_meta'}:
                raise BackupError('The backup version and tables do not match.')
            created = datetime.fromisoformat(metadata[0][1])
            if created.tzinfo is None:
                raise BackupError('The backup date is invalid.')
            stage.execute('BEGIN IMMEDIATE')
            for table in reversed(TABLES):
                stage.execute(f'DELETE FROM "{table}"')
            for table in TABLES:
                if table in ('payments','waivers') and table not in tables:
                    continue  # Older backups have no entries for the newer features.
                expected = {r[1]: r[2].upper() for r in stage.execute(f'PRAGMA table_info("{table}")')}
                actual = {r[1]: r[2].upper() for r in source.execute(f'PRAGMA table_info("{table}")')}
                if expected != actual:
                    raise BackupError(f'The backup has an incompatible {table} table.')
                if table == 'admin_sessions':
                    continue  # Restoring must not reactivate old admin sessions.
                columns = ','.join(f'"{c}"' for c in expected)
                marks = ','.join('?' for _ in expected)
                cursor = source.execute(f'SELECT {columns} FROM "{table}"')
                while rows := cursor.fetchmany(500):
                    stage.executemany(f'INSERT INTO "{table}" ({columns}) VALUES ({marks})', rows)
            if stage.execute('''SELECT 1 FROM bookings b WHERE
                COALESCE((SELECT SUM(amount) FROM payments WHERE booking_id=b.id AND voided_at IS NULL),0)+
                COALESCE((SELECT SUM(amount) FROM waivers WHERE booking_id=b.id AND voided_at IS NULL),0)>b.total''').fetchone():
                raise BackupError('The backup contains payments and waivers exceeding booking charges.')
            stage.execute('UPDATE sessions SET expires=0')
            spaces = {tuple(row) for row in stage.execute('SELECT id,type FROM spaces')}
            expected_spaces = {(f'{prefix}{i:02d}', kind) for prefix,kind in [('T','truck'),('B','bus')] for i in range(1,6)}
            if spaces != expected_spaces or stage.execute('SELECT count(*) FROM price_settings').fetchone()[0] != 1:
                raise BackupError('The backup is missing parking spaces or prices.')
            stage.commit()
            # Store connections use sqlite3.Row; use tuples for integrity results.
            stage.row_factory = None
            verify_database(stage)
            return {'createdAt': created.isoformat(),
                    'bookings': stage.execute('SELECT count(*) FROM bookings').fetchone()[0],
                    'customers': stage.execute('SELECT count(*) FROM customers').fetchone()[0]}
    except (sqlite3.Error, ValueError, TypeError, OSError) as error:
        raise BackupError('The backup could not be read or validated. No data was restored.') from error


def restore_database(source, database, store_factory, confirm):
    """Validate, confirm, save current data, then replace it while offline."""
    source, database = Path(source).resolve(), Path(database).resolve()
    if source == database:
        raise BackupError('Choose a separate backup file, not the live parking database.')
    with DatabaseLock(database):
        with tempfile.TemporaryDirectory(prefix='restore-check-', dir=database.parent) as folder:
            stage_path = Path(folder) / 'validated.sqlite3'
            summary = prepare_restore(source, stage_path, store_factory)
            if not confirm(summary):
                return {'restored': False}
            safety = None
            if database.exists():
                safety = create_backup(database, database.parent / 'backups' / unique_name('before-restore'))
            # SQLite installs the complete snapshot transactionally, including WAL handling.
            with closing(readonly(stage_path)) as stage, closing(sqlite3.connect(database, timeout=10)) as target:
                stage.backup(target)
            return {'restored': True, 'safetyBackup': str(safety) if safety else None, **summary}
