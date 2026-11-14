"""Admin regression checks. Passwords here are fixtures for disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time
import unittest
import server
import test_server
Client = test_server.Client

TEST_PASSWORD = 'Local-test-only-4488'


class AdminTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def login(self):
        if not self.app.store.admin_configured():
            self.app.store.set_admin_password(TEST_PASSWORD)
        admin = Client(self.app)
        self.assertEqual(admin.request('GET', '/api/admin/session')[0], 200)
        self.assertEqual(admin.request('POST', '/api/admin/login', {'password': TEST_PASSWORD})[0], 200)
        return admin

    def test_setup_and_authentication_required(self):
        anon = Client(self.app)
        status, session = anon.request('GET', '/api/admin/session')
        self.assertEqual(status, 200)
        self.assertFalse(session['configured'])
        self.assertFalse(session['authenticated'])
        self.assertEqual(anon.request('GET', '/api/admin/state')[0], 401)
        self.assertEqual(anon.request('POST', '/api/admin/login', {'password': TEST_PASSWORD})[0], 503)
        _, booking = self.a.book()
        path = f"/api/admin/bookings/{booking['booking']['id']}/check-in"
        self.assertEqual(self.a.request('GET', '/api/admin/state')[0], 401)
        self.assertEqual(anon.request('POST', path, {})[0], 401)
        self.assertEqual(anon.request('POST', '/api/admin/logout', {})[0], 401)

    def test_admin_sees_all_customers_and_logout_revokes_access(self):
        self.a.book()
        self.b.book(spaceId='B01', driver='Second Driver')
        admin = self.login()
        state = admin.request('GET', '/api/admin/state')[1]
        self.assertEqual(len(state['bookings']), 2)
        self.assertEqual({b['driver'] for b in state['bookings']}, {'Test Driver', 'Second Driver'})
        self.assertEqual({b['status'] for b in state['bookings']}, {'reserved'})
        old_cookie, old_csrf = admin.cookie, admin.csrf
        self.assertEqual(admin.request('POST', '/api/admin/logout', {})[0], 200)
        admin.cookie, admin.csrf = old_cookie, old_csrf
        self.assertEqual(admin.request('GET', '/api/admin/state')[0], 401)
        self.assertEqual(len(self.a.state()['bookings']), 1)

    def test_checkin_overdue_checkout_and_customer_protection(self):
        _, booking = self.a.book()
        identifier = booking['booking']['id']
        admin = self.login()
        path = f'/api/admin/bookings/{identifier}'
        self.assertEqual(admin.request('POST',path+'/check-in',{})[0],200)
        self.assertEqual(admin.request('POST',path+'/check-in',{})[0],200)
        self.assertIsNotNone(self.a.state()['bookings'][0]['checkedInAt'])
        self.assertEqual(self.a.request('POST',f'/api/bookings/{identifier}/release',{})[0],409)
        self.assertEqual(self.a.request('POST','/api/bookings/release-all',{})[0],200)
        self.assertEqual(len(self.a.state()['bookings']),1)
        now=int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?',(now-7200000,now-1,identifier))
        self.assertEqual(admin.request('GET','/api/admin/state')[1]['bookings'][0]['status'],'overdue')
        self.assertEqual(sum(s['occupied'] for s in self.b.state()['spaces']),1)
        self.assertEqual(self.b.book()[0],409)
        self.assertEqual(admin.request('POST',path+'/cancel',{})[0],409)
        self.assertEqual(admin.request('POST',path+'/check-out',test_server.checkout_confirmation(self.app.store, identifier))[0],200)
        self.assertEqual(admin.request('POST',path+'/check-out',test_server.checkout_confirmation(self.app.store, identifier))[0],200)
        self.assertEqual(admin.request('GET','/api/admin/state')[1]['bookings'][0]['status'],'departed')
        self.assertEqual(sum(s['occupied'] for s in self.b.state()['spaces']),0)
        self.assertEqual(self.b.book()[0],201)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_events WHERE booking_id=?',(identifier,)).fetchone()[0],2)

    def test_cancellation_and_invalid_transitions(self):
        _, result=self.a.book()
        admin=self.login()
        path=f"/api/admin/bookings/{result['booking']['id']}"
        self.assertEqual(admin.request('POST',path+'/check-out',{})[0],409)
        self.assertEqual(admin.request('POST',path+'/cancel',{})[0],200)
        self.assertEqual(admin.request('POST',path+'/cancel',{})[0],200)
        self.assertEqual(admin.request('POST',path+'/check-in',{})[0],409)
        self.assertEqual(admin.request('GET','/api/admin/state')[1]['bookings'][0]['status'],'cancelled')
        self.assertEqual(self.a.state()['bookings'],[])
        self.assertEqual(self.b.book()[0],201)
        self.assertEqual(admin.request('POST','/api/admin/bookings/PK-0000000000000000/check-in',{})[0],404)

    def test_expired_reservation_cannot_check_in(self):
        _, result=self.a.book()
        now=int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=?',(now-7200000,now-1))
        admin=self.login()
        self.assertEqual(admin.request('POST',f"/api/admin/bookings/{result['booking']['id']}/check-in",{})[0],409)
        self.assertEqual(admin.request('GET','/api/admin/state')[1]['bookings'][0]['status'],'expired')
        self.assertEqual(self.b.book()[0],201)

    def test_login_limits_hashing_and_password_reset(self):
        admin=self.login()
        anon=Client(self.app);anon.request('GET','/api/admin/session')
        for _ in range(5):
            self.assertEqual(anon.request('POST','/api/admin/login',{'password':'wrong-password'})[0],401)
        self.assertEqual(anon.request('POST','/api/admin/login',{'password':TEST_PASSWORD})[0],429)
        with self.app.store.connect() as db:
            row=db.execute('SELECT * FROM admin_credentials').fetchone()
            self.assertNotEqual(row['password_hash'],TEST_PASSWORD)
            db.execute('UPDATE admin_credentials SET locked_until=?',(int(time.time())-1,))
        self.assertEqual(anon.request('POST','/api/admin/login',{'password':TEST_PASSWORD})[0],200)
        self.app.store.set_admin_password('Replacement-test-5522')
        self.assertEqual(admin.request('GET','/api/admin/state')[0],401)
        self.assertEqual(anon.request('GET','/api/admin/state')[0],401)

    def test_session_expiry_and_csrf_origin_checks(self):
        admin=self.login()
        self.assertEqual(admin.request('POST','/api/admin/logout',{},extra={'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(admin.request('POST','/api/admin/logout',{},extra={'Origin':'https://other.example'})[0],403)
        with self.app.store.connect() as db:
            db.execute('UPDATE admin_sessions SET expires=?',(int(time.time())-1,))
        self.assertEqual(admin.request('GET','/api/admin/state')[0],401)
        self.assertEqual(admin.request('POST','/api/admin/logout',{})[0],401)

    def test_checkin_and_customer_release_are_atomic(self):
        _,result=self.a.book()
        identifier=result['booking']['id'];admin=self.login();barrier=threading.Barrier(2)
        def arrive():
            barrier.wait();return admin.request('POST',f'/api/admin/bookings/{identifier}/check-in',{})[0]
        def release():
            barrier.wait();return self.a.request('POST',f'/api/bookings/{identifier}/release',{})[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(arrive);second=pool.submit(release);results=(first.result(),second.result())
        self.assertIn(results,[(200,409),(409,200)])

    def test_legacy_database_migration_preserves_bookings(self):
        self.a.book()
        legacy=Path(self.temp.name)/'legacy.sqlite3'
        with self.app.store.connect() as db, closing(sqlite3.connect(legacy)) as old:
            db.backup(old)
            for column in ['checked_in_at','checked_out_at','cancelled_at']:
                old.execute(f'ALTER TABLE bookings DROP COLUMN {column}')
        upgraded=server.Store(legacy)
        token=self.a.cookie.split('=',1)[1]
        session,_=upgraded.session(token)
        self.assertEqual(len(upgraded.state(session)['bookings']),1)
        self.assertTrue(legacy.with_name('legacy.before-admin.sqlite3').exists())
        server.Store(legacy)  # Repeated startup must be safe.
        self.assertEqual(len(upgraded.state(session)['bookings']),1)

    def test_admin_assets_available_but_password_database_private(self):
        for path in ['/admin','/admin.html','/admin.js?v=test','/admin.css?v=test']:
            self.assertEqual(self.a.request('GET',path)[0],200)
        for path in ['/data/parking.sqlite3','/data/parking.before-admin.sqlite3','/test_admin.py']:
            self.assertEqual(self.a.request('GET',path)[0],404)


if __name__=='__main__':
    unittest.main()
