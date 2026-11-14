"""Backups and restores only use disposable databases, never real bookings."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import backups
import server
import test_admin
import test_server
import test_extensions


class BackupTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown
    login = test_admin.AdminTests.login

    def snapshot(self, name='backup.sqlite3'):
        return backups.create_backup(self.app.store.path, Path(self.temp.name) / name)

    def restart(self):
        path = self.app.store.path
        self.app = server.make_server(0, path)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.a.app = self.b.app = self.app

    def stop(self):
        self.app.shutdown(); self.app.server_close(); self.thread.join()

    def test_download_auth_private_files_and_live_wal_snapshot(self):
        self.assertEqual(self.a.request('GET','/api/admin/backup')[0],401)
        admin = self.login()
        self.assertEqual(self.a.book()[0],201)
        # Keep WAL connection open to ensure online snapshots include uncheckpointed writes.
        with self.app.store.connect() as db:
            db.execute("UPDATE price_settings SET truck_hour=99")
            status, content = admin.request('GET','/api/admin/backup')
            self.assertEqual(status,200)
            self.assertTrue(content.startswith(b'SQLite format 3\x00'))
            path=Path(self.temp.name)/'download.sqlite3';path.write_bytes(content)
            with closing(sqlite3.connect(path)) as saved:
                self.assertEqual(saved.execute('SELECT count(*) FROM bookings').fetchone()[0],1)
                self.assertEqual(saved.execute('SELECT truck_hour FROM price_settings').fetchone()[0],99)
                self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        for url in ['/backups.py','/data/backups/test.sqlite3','/api/backups','/api/admin/restore']:
            self.assertEqual(self.a.request('GET',url)[0],404)
        with self.app.store.connect() as db:db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(admin.request('GET','/api/admin/backup')[0],401)

    def test_restore_preserves_data_creates_safety_copy_and_signs_everyone_out(self):
        admin = self.login()
        status,result=self.a.book();self.assertEqual(status,201)
        booking=result['booking']
        draft=test_extensions.ExtensionTests.quote(self,booking)
        self.assertEqual(self.a.request('POST',f"/api/bookings/{booking['id']}/extend",draft)[0],200)
        self.assertEqual(admin.request('POST',f"/api/admin/bookings/{booking['id']}/check-in",{})[0],200)
        with self.app.store.connect() as db:
            db.execute('UPDATE price_settings SET truck_hour=99')
        self.app.store.set_space_block('B05',dict(blocked=True,reason='Ground repair',blockRevision=0))
        with self.app.store.connect() as db:
            expected={table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
                      for table in backups.TABLES if table not in ('sessions','admin_sessions')}
        original=self.snapshot()
        self.assertEqual(self.b.book(spaceId='T02')[0],201)
        path=self.app.store.path
        with self.assertRaises(backups.BackupError):
            backups.restore_database(original,path,server.Store,lambda _:True)
        self.stop()
        try:
            result=backups.restore_database(original,path,server.Store,lambda info:info['bookings']==1 and info['customers']==2)
            self.assertTrue(result['restored'])
            with closing(sqlite3.connect(result['safetyBackup'])) as safety:
                self.assertEqual(safety.execute('SELECT count(*) FROM bookings').fetchone()[0],2)
            with closing(sqlite3.connect(path)) as restored:
                for table,rows in expected.items():
                    self.assertEqual(restored.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall(),rows,table)
                self.assertEqual(restored.execute('SELECT count(*) FROM bookings').fetchone()[0],1)
                self.assertEqual(restored.execute('SELECT count(*) FROM admin_sessions').fetchone()[0],0)
                self.assertEqual(restored.execute('SELECT max(expires) FROM sessions').fetchone()[0],0)
                self.assertEqual(restored.execute("SELECT reason FROM space_blocks WHERE space_id='B05'").fetchone()[0],'Ground repair')
        finally:self.restart()
        admin.app=self.app
        self.assertEqual(admin.request('GET','/api/admin/state')[0],401)
        self.assertEqual(self.a.state()['bookings'],[])
        self.assertEqual(self.a.login('first@example.test')[0],200)
        self.assertEqual(len(self.a.state()['bookings']),1)
        self.assertEqual(self.login().request('GET','/api/admin/state')[1]['totalRecords'],1)

    def test_cancel_invalid_and_safety_backup_failure_leave_data_unchanged(self):
        self.assertEqual(self.a.book()[0],201)
        original=self.snapshot();path=self.app.store.path
        self.stop()
        try:
            result=backups.restore_database(original,path,server.Store,lambda _:False)
            self.assertFalse(result['restored'])
            bad=Path(self.temp.name)/'bad.sqlite3';bad.write_bytes(b'not a database')
            with self.assertRaises(backups.BackupError):backups.restore_database(bad,path,server.Store,lambda _:True)
            with patch('backups.create_backup',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):backups.restore_database(original,path,server.Store,lambda _:True)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM bookings').fetchone()[0],1)
            self.assertFalse(list(path.parent.glob('restore-check-*')))
        finally:self.restart()

    def test_invalid_schema_and_constraints_rejected_before_confirmation(self):
        original=self.snapshot(); path=self.app.store.path
        self.stop()
        try:
            with closing(sqlite3.connect(original)) as db:
                db.execute('CREATE TRIGGER unwanted AFTER INSERT ON spaces BEGIN DELETE FROM bookings; END')
            called=[]
            with self.assertRaises(backups.BackupError):backups.restore_database(original,path,server.Store,lambda _:called.append(True))
            self.assertEqual(called,[])
            with closing(sqlite3.connect(original)) as db:
                db.execute('DROP TRIGGER unwanted')
                db.execute('DELETE FROM price_settings');db.commit()
            with self.assertRaises(backups.BackupError):backups.restore_database(original,path,server.Store,lambda _:True)
        finally:self.restart()

    def test_no_overwrite_no_accidental_empty_source_and_single_server(self):
        original=self.snapshot();before=original.read_bytes()
        with self.assertRaises(FileExistsError):backups.create_backup(self.app.store.path,original)
        self.assertEqual(original.read_bytes(),before)
        missing=Path(self.temp.name)/'missing.sqlite3';output=Path(self.temp.name)/'failed.sqlite3'
        with self.assertRaises(FileNotFoundError):backups.create_backup(missing,output)
        self.assertFalse(missing.exists());self.assertFalse(output.exists())
        with self.assertRaises(backups.BackupError):server.make_server(0,self.app.store.path)
        # The lock is enforced across processes, not just within Python objects.
        code='import backups,sys; backups.DatabaseLock(sys.argv[1])'
        result=subprocess.run([sys.executable,'-c',code,str(self.app.store.path)],cwd=server.ROOT,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Stop the parking server',result.stderr)

    def test_cli_restore_to_new_project_cancel_then_confirm_and_backup(self):
        self.assertEqual(self.a.book()[0],201)
        original=self.snapshot()
        project=Path(self.temp.name)/'isolated app';project.mkdir()
        for name in ['server.py','backups.py','excel_report.py']:
            shutil.copy2(server.ROOT/name,project/name)
        command=[sys.executable,str(project/'server.py'),'--restore',str(original)]
        cancelled=subprocess.run(command,input='\n',capture_output=True,text=True)
        self.assertEqual(cancelled.returncode,0,cancelled.stderr)
        self.assertIn('cancelled',cancelled.stdout)
        self.assertFalse((project/'data/parking.sqlite3').exists())
        confirmed=subprocess.run(command,input='RESTORE\n',capture_output=True,text=True)
        self.assertEqual(confirmed.returncode,0,confirmed.stderr)
        self.assertIn('Restore complete',confirmed.stdout)
        with closing(sqlite3.connect(project/'data/parking.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bookings').fetchone()[0],1)
        saved=subprocess.run([sys.executable,str(project/'server.py'),'--backup'],capture_output=True,text=True)
        self.assertEqual(saved.returncode,0,saved.stderr)
        self.assertEqual(len(list((project/'data/backups').glob('*.sqlite3'))),1)


if __name__=='__main__':unittest.main()
