"""Maintenance blocks, booking races, access control and persistence."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
import server
import test_admin
import test_server


class SpaceBlockTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown
    login = test_admin.AdminTests.login

    def block(self, admin, space='T01', blocked=True, revision=0, reason='Ground repair', **extra):
        return admin.request('POST', f'/api/admin/spaces/{space}/block',
                             dict(blocked=blocked, blockRevision=revision, reason=reason), **extra)

    def test_block_public_reason_reopen_and_restart(self):
        admin = self.login()
        self.assertEqual(self.block(admin)[0], 200)
        self.assertEqual(self.a.book()[0], 409)
        for client in [self.a, self.b]:
            spaces = client.state()['spaces']
            self.assertEqual(sum(s['type']=='truck' and not s['blocked'] and not s['occupied'] for s in spaces), 4)
            space = next(s for s in spaces if s['id']=='T01')
            self.assertEqual(space['blockReason'], 'Ground repair')
            self.assertFalse(space['occupied'])
        self.app.store = server.Store(self.app.store.path)
        self.assertEqual(self.a.book()[0], 409)
        self.assertEqual(self.block(admin, blocked=False, revision=1)[0], 200)
        self.assertEqual(next(s for s in self.b.state()['spaces'] if s['id']=='T01')['blockReason'], '')
        self.assertEqual(self.a.book()[0], 201)

    def test_reserved_and_overdue_vehicles_protected_then_checkout(self):
        admin = self.login()
        _, result = self.a.book()
        identifier = result['booking']['id']
        self.assertEqual(self.block(admin)[0], 409)
        self.assertEqual(admin.request('POST', f'/api/admin/bookings/{identifier}/check-in', {})[0], 200)
        now = int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000, now-1, identifier))
        self.assertEqual(self.block(admin)[0], 409)
        self.assertEqual(admin.request('GET', '/api/admin/state')[1]['bookings'][0]['status'], 'overdue')
        self.assertEqual(admin.request('POST', f'/api/admin/bookings/{identifier}/check-out', test_server.checkout_confirmation(self.app.store, identifier))[0], 200)
        self.assertEqual(self.block(admin)[0], 200)
        self.assertEqual(self.b.book()[0], 409)
        self.assertEqual(admin.request('GET', '/api/admin/state')[1]['bookings'][0]['status'], 'departed')

    def test_expired_unarrived_booking_can_be_blocked_without_deleting_history(self):
        admin = self.login()
        self.assertEqual(self.a.book()[0], 201)
        now = int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=?', (now-7200000, now-1))
        self.assertEqual(self.block(admin)[0], 200)
        self.assertEqual(len(self.a.state()['history']), 1)

    def test_admin_auth_csrf_origin_validation_and_stale_edits(self):
        admin = self.login()
        guest = test_server.Client(self.app); guest.request('GET', '/api/admin/session')
        self.assertEqual(self.block(guest)[0], 401)
        self.assertEqual(self.block(self.a)[0], 401)
        self.assertEqual(self.block(admin, extra={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.block(admin, extra={'Origin': 'https://example.test'})[0], 403)
        self.assertEqual(self.block(admin, space='T99')[0], 404)
        for reason in ['', '  ', 'a'*161, 'line\nbreak', 42, '\ud800']:
            self.assertEqual(self.block(admin, reason=reason)[0], 400)
        for data in [[], {}, {'blocked': 1, 'blockRevision': 0}, {'blocked': True, 'blockRevision': True}]:
            self.assertEqual(admin.request('POST', '/api/admin/spaces/T01/block', data)[0], 400)
        self.assertEqual(self.block(admin, reason='<b>Repair</b> & பராமரிப்பு')[0], 200)
        self.assertEqual(self.block(admin, blocked=False, revision=0)[0], 409)
        state = admin.request('GET', '/api/admin/state')[1]
        self.assertEqual(next(s for s in state['spaces'] if s['id']=='T01')['blockRevision'], 1)
        self.assertEqual(admin.request('POST', '/api/admin/logout', {})[0], 200)
        self.assertNotEqual(self.block(admin, blocked=False, revision=1)[0], 200)

    def test_simultaneous_booking_and_block_only_one_wins(self):
        admin = self.login()
        barrier = threading.Barrier(2)
        def block():
            barrier.wait()
            return self.block(admin)[0]
        def book():
            barrier.wait()
            return self.a.book()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(block), pool.submit(book)
            statuses = first.result(), second.result()
        self.assertIn(statuses, [(200,409), (409,201)])
        space = next(s for s in self.a.state()['spaces'] if s['id']=='T01')
        self.assertEqual(int(space['blocked']) + int(space['occupied']), 1)

    def test_upgrade_preserves_existing_bookings(self):
        self.assertEqual(self.a.book()[0], 201)
        with self.app.store.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM bookings')]
            db.execute('DROP TABLE space_blocks')
        self.app.store = server.Store(self.app.store.path)
        with self.app.store.connect() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM bookings')])
        self.assertTrue(all(not s['blocked'] for s in self.a.state()['spaces']))
        self.assertEqual(self.block(self.login(), space='B01')[0], 200)


if __name__ == '__main__':
    unittest.main()
