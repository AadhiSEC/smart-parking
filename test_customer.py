"""Customer account tests. Credentials and databases are disposable fixtures."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import secrets
import sqlite3
import time
import unittest
import server
import test_server

Client = test_server.Client
PASSWORD = 'Customer-test-only-4488'


class CustomerTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def guest(self):
        client = Client(self.app)
        client.state()
        return client

    def legacy_booking(self, client, space='T01', request_id=None):
        session, _ = self.app.store.session(client.cookie.split('=', 1)[1])
        return self.app.store.book(session['id'], {
            'spaceId': space, 'unit': 'hour', 'duration': 2, 'driver': 'Legacy Driver',
            'plate': f'TN 01 ZZ {int(space[1:]) + (100 if space[0] == "B" else 0):04d}', 'requestId': request_id or secrets.token_hex(16)
        })[0]

    def test_signup_required_and_account_validation(self):
        guest = self.guest()
        self.assertIsNone(guest.state()['customer'])
        self.assertEqual(guest.book()[0], 401)
        self.assertEqual(guest.signup('invalid')[0], 400)
        self.assertEqual(guest.signup('valid@example.test', password='short')[0], 400)
        self.assertEqual(guest.signup('valid@example.test', name='  ')[0], 400)
        self.assertEqual(guest.signup('valid@example.test', name='x\n')[0], 400)
        self.assertEqual(guest.signup('valid@example.test', password='x'*129)[0], 400)
        self.assertEqual(guest.request('POST', '/api/customer/signup', [])[0], 400)
        self.assertEqual(guest.signup(' FIRST@EXAMPLE.TEST ')[0], 409)
        self.assertEqual(guest.signup(' NEW@EXAMPLE.TEST ', name=' New Customer ')[0], 201)
        state = guest.state()
        self.assertEqual(state['customer']['email'], 'new@example.test')
        self.assertEqual(state['customer']['name'], 'New Customer')
        self.assertEqual(guest.book()[0], 201)
        self.assertNotIn('password', json.dumps(state))
        self.assertEqual(guest.request('GET', '/api/admin/state')[0], 401)

    def test_same_account_in_second_browser_owns_booking_and_history(self):
        booking = self.a.book()[1]['booking']
        second = self.guest()
        self.assertEqual(second.login('FIRST@example.test')[0], 200)
        self.assertEqual(second.state()['bookings'], [{**booking, 'balance': booking['total']}])
        path = f"/api/bookings/{booking['id']}/release"
        self.assertEqual(self.b.request('POST', path, {})[0], 404)
        self.assertEqual(second.request('POST', path, {})[0], 200)
        state = self.a.state()
        self.assertEqual(state['bookings'], [])
        self.assertEqual(state['history'][0]['id'], booking['id'])
        self.assertEqual(state['history'][0]['status'], 'released')
        self.assertEqual(self.b.state()['history'], [])

    def test_idempotent_retry_across_customer_sessions(self):
        request_id = secrets.token_hex(16)
        first = self.a.book(requestId=request_id)
        second = self.guest()
        self.assertEqual(second.login('first@example.test')[0], 200)
        again = second.book(requestId=request_id, plate=first[1]['booking']['plate'])
        self.assertEqual((first[0], again[0]), (201, 200))
        self.assertEqual(first[1], again[1])
        self.assertEqual(second.book(requestId=request_id, duration=3)[0], 409)
        self.assertEqual(len(second.state()['bookings']), 1)

    def test_logout_revokes_cookie_but_preserves_account(self):
        self.a.book()
        stolen = Client(self.app)
        stolen.cookie, stolen.csrf = self.a.cookie, self.a.csrf
        self.assertEqual(self.a.request('POST', '/api/customer/logout', {})[0], 200)
        self.assertNotEqual(self.a.cookie, stolen.cookie)
        self.assertNotEqual(self.a.csrf, stolen.csrf)
        self.assertEqual(stolen.book(spaceId='T02')[0], 401)
        self.assertIsNone(stolen.state()['customer'])
        self.assertEqual(stolen.state()['bookings'], [])
        self.assertIsNone(self.a.state()['customer'])
        self.assertEqual(self.a.login('first@example.test')[0], 200)
        self.assertEqual(len(self.a.state()['bookings']), 1)

    def test_signin_rotates_session_and_claims_only_current_guest_bookings(self):
        guest = self.guest()
        other = self.guest()
        mine = self.legacy_booking(guest)
        theirs = self.legacy_booking(other, 'B01')
        old_cookie, old_csrf = guest.cookie, guest.csrf
        self.assertEqual(guest.login('first@example.test')[0], 200)
        self.assertNotEqual(guest.cookie, old_cookie)
        self.assertNotEqual(guest.csrf, old_csrf)
        self.assertEqual([b['id'] for b in self.a.state()['bookings']], [mine['id']])
        self.assertEqual([b['id'] for b in other.state()['bookings']], [theirs['id']])
        replay = Client(self.app); replay.cookie = old_cookie; replay.csrf = old_csrf
        self.assertEqual(replay.request('POST', f"/api/bookings/{mine['id']}/release", {})[0], 401)
        self.assertEqual(replay.state()['bookings'], [])

    def test_claim_handles_duplicate_request_ids_and_signup(self):
        request_id = secrets.token_hex(16)
        self.a.book(requestId=request_id)
        guest = self.guest()
        self.legacy_booking(guest, 'T02', request_id)
        self.assertEqual(guest.login('first@example.test')[0], 200)
        self.assertEqual(len(guest.state()['bookings']), 2)
        new_guest = self.guest()
        legacy = self.legacy_booking(new_guest, 'B01')
        self.assertEqual(new_guest.signup('new@example.test')[0], 201)
        self.assertEqual(new_guest.state()['bookings'][0]['id'], legacy['id'])

    def test_password_hash_limits_and_local_recovery(self):
        guest = self.guest()
        unknown = guest.login('unknown@example.test', 'wrong')[1]
        wrong = guest.login('first@example.test', 'wrong')[1]
        self.assertEqual(unknown, wrong)
        for _ in range(4):
            self.assertEqual(guest.login('first@example.test', 'wrong')[0], 401)
        self.assertEqual(guest.login('first@example.test')[0], 429)
        self.assertEqual(self.b.state()['customer']['email'], 'second@example.test')
        with self.app.store.connect() as db:
            row = db.execute('SELECT * FROM customers WHERE email=?', ('first@example.test',)).fetchone()
            self.assertNotEqual(row['password_hash'], PASSWORD)
            expected = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), bytes.fromhex(row['salt']), server.PASSWORD_ROUNDS).hex()
            self.assertEqual(row['password_hash'], expected)
            db.execute('UPDATE customers SET locked_until=? WHERE id=?', (int(time.time())-1, row['id']))
        self.assertEqual(guest.login('first@example.test')[0], 200)
        self.a.book()
        self.app.store.reset_customer_password('first@example.test', 'Replacement-test-8899')
        self.assertEqual(self.a.book(spaceId='T02')[0], 401)
        self.assertEqual(guest.book(spaceId='T02')[0], 401)
        fresh = self.guest()
        self.assertEqual(fresh.login('first@example.test')[0], 401)
        self.assertEqual(fresh.login('first@example.test', 'Replacement-test-8899')[0], 200)
        self.assertEqual(len(fresh.state()['bookings']), 1)
        self.assertIsNotNone(self.b.state()['customer'])

    def test_auth_csrf_origin_and_expiry(self):
        guest = self.guest()
        data = {'email': 'first@example.test', 'password': PASSWORD}
        self.assertEqual(guest.request('POST', '/api/customer/login', data, extra={'X-CSRF-Token': 'wrong'})[0], 403)
        self.assertEqual(guest.request('POST', '/api/customer/login', data, extra={'Origin': 'https://elsewhere.test'})[0], 403)
        self.assertEqual(self.a.request('POST', '/api/customer/logout', {}, extra={'X-CSRF-Token': 'wrong'})[0], 403)
        self.assertEqual(self.a.login('second@example.test')[0], 409)
        with self.app.store.connect() as db:
            db.execute('UPDATE sessions SET expires=? WHERE customer_id IS NOT NULL', (int(time.time())-1,))
        self.assertEqual(self.a.book()[0], 401)
        self.assertIsNone(self.a.state()['customer'])
        self.assertEqual(self.a.login('first@example.test')[0], 200)

    def test_history_expiry_and_admin_departure(self):
        first = self.a.book()[1]['booking']
        now = int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000, now-1, first['id']))
        self.assertEqual(self.a.state()['history'][0]['status'], 'expired')
        second = self.a.book()[1]['booking']
        self.app.store.admin_action(second['id'], 'check-in')
        self.assertEqual(self.a.request('POST', f"/api/bookings/{second['id']}/release", {})[0], 409)
        self.app.store.admin_action(second['id'], 'check-out', test_server.checkout_confirmation(self.app.store, second['id']))
        history = self.a.state()['history']
        self.assertEqual({b['status'] for b in history}, {'expired', 'departed'})
        self.assertEqual(self.a.state()['historyTotal'], 2)

    def test_account_migration_preserves_legacy_and_admin(self):
        guest = self.guest()
        booking = self.legacy_booking(guest)
        self.app.store.set_admin_password('Admin-migration-test-7788')
        legacy = Path(self.temp.name) / 'pre-accounts.sqlite3'
        with self.app.store.connect() as db, closing(sqlite3.connect(legacy)) as old:
            db.backup(old)
            old.execute('DROP INDEX bookings_customer')
            old.execute('DROP INDEX customer_requests')
            old.execute('ALTER TABLE bookings DROP COLUMN customer_id')
            old.execute('ALTER TABLE sessions DROP COLUMN customer_id')
            old.execute('DROP TABLE customers')
        upgraded = server.Store(legacy)
        session, _ = upgraded.session(guest.cookie.split('=', 1)[1])
        self.assertEqual(upgraded.state(session)['bookings'][0]['id'], booking['id'])
        self.assertTrue(upgraded.admin_configured())
        self.assertTrue(legacy.with_name('pre-accounts.before-accounts.sqlite3').exists())
        result, token = upgraded.customer_auth(session, {'name': 'Migrated', 'email': 'migrated@example.test', 'password': PASSWORD}, signup=True)
        restarted = server.Store(legacy)
        session, _ = restarted.session(token)
        self.assertEqual(restarted.state(session)['customer']['id'], result['customer']['id'])
        self.assertEqual(restarted.state(session)['bookings'][0]['id'], booking['id'])


if __name__ == '__main__':
    unittest.main()
