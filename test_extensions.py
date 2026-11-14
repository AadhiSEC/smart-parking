"""Booking extension regression tests. All databases are disposable."""
from concurrent.futures import ThreadPoolExecutor
import secrets
import threading
import time
import unittest
import server
import test_server


class ExtensionTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def booking(self, **kwargs):
        status, data = self.a.book(**kwargs)
        self.assertEqual(status, 201)
        return data['booking']

    def quote(self, booking, unit='hour', duration=2, client=None):
        status, data = (client or self.a).request('POST', f"/api/bookings/{booking['id']}/extension-quote", {'unit': unit, 'duration': duration})
        self.assertEqual(status, 200, data)
        q = data['quote']
        return {'unit': unit, 'duration': duration, 'expectedEnd': q['previousEnd'],
                'expectedTotal': q['previousTotal'], 'expectedRate': q['rate'], 'requestId': secrets.token_hex(16)}

    def extend(self, booking, data, client=None):
        return (client or self.a).request('POST', f"/api/bookings/{booking['id']}/extend", data)

    def test_mixed_extensions_preserve_original_price_and_receipt_across_restart(self):
        booking = self.booking()
        draft = self.quote(booking)
        self.assertEqual(self.a.state()['bookings'][0], {**booking, 'balance': booking['total']})
        self.assertEqual(self.extend(booking, draft)[0], 200)
        with self.app.store.connect() as db:
            db.execute('UPDATE price_settings SET truck_day=900,truck_hour=125')
        draft = self.quote(booking, 'day', 1)
        status, result = self.extend(booking, draft)
        self.assertEqual(status, 200)
        updated = result['booking']
        self.assertEqual(updated['end'], booking['end'] + 2*3600000 + 86400000)
        self.assertEqual(updated['total'], 160+160+900)
        self.assertEqual(updated['start'], booking['start'])
        self.assertEqual(sum(s['occupied'] for s in self.b.state()['spaces']), 1)
        reopened = server.Store(self.app.store.path)
        session, _ = reopened.session(self.a.cookie.split('=', 1)[1])
        receipt = reopened.receipt(session, booking['id'])['booking']
        self.assertEqual([c['amount'] for c in receipt['charges']], [160, 160, 900])
        self.assertEqual([c['rate'] for c in receipt['charges']], [80, 80, 900])
        self.assertEqual(sum(c['amount'] for c in receipt['charges']), receipt['total'])
        self.assertEqual(receipt['rate'], 80)
        self.assertEqual(receipt['end'], updated['end'])

    def test_retry_same_account_second_browser_is_idempotent(self):
        booking = self.booking()
        draft = self.quote(booking)
        first = self.extend(booking, draft)
        second = test_server.Client(self.app)
        second.state(); second.login('first@example.test')
        self.assertEqual(self.extend(booking, draft, second), first)
        modified = {**draft, 'duration': 3}
        self.assertEqual(self.extend(booking, modified)[0], 409)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM booking_extensions').fetchone()[0], 1)

    def test_stale_price_or_booking_requires_new_preview(self):
        booking = self.booking()
        draft = self.quote(booking)
        with self.app.store.connect() as db:
            db.execute('UPDATE price_settings SET truck_hour=150')
        self.assertEqual(self.extend(booking, draft)[0], 409)
        fresh = self.quote(booking)
        self.assertEqual(self.extend(booking, fresh)[0], 200)
        stale = {**fresh, 'requestId': secrets.token_hex(16)}
        self.assertEqual(self.extend(booking, stale)[0], 409)
        self.assertEqual(self.a.state()['bookings'][0]['total'], 460)

    def test_ownership_guest_expiry_csrf_and_origin(self):
        booking = self.booking()
        draft = self.quote(booking)
        guest = test_server.Client(self.app); guest.state()
        for action in ('extension-quote', 'extend'):
            path = f"/api/bookings/{booking['id']}/{action}"
            self.assertEqual(self.b.request('POST', path, draft)[0], 404)
            self.assertEqual(guest.request('POST', path, draft)[0], 401)
            self.assertEqual(self.a.request('POST', path, draft, extra={'X-CSRF-Token': 'wrong'})[0], 403)
            self.assertEqual(self.a.request('POST', path, draft, extra={'Origin': 'https://other.example'})[0], 403)
        with self.app.store.connect() as db:
            db.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.extend(booking, draft)[0], 401)

    def test_terminal_bookings_and_expired_space_reuse_reject_extension(self):
        for action in ('release', 'cancel', 'checkout', 'expire'):
            booking = self.booking()
            draft = self.quote(booking)
            if action == 'release':
                self.a.request('POST', f"/api/bookings/{booking['id']}/release", {})
            elif action == 'cancel':
                self.app.store.admin_action(booking['id'], 'cancel')
            elif action == 'checkout':
                self.app.store.admin_action(booking['id'], 'check-in')
                self.app.store.admin_action(booking['id'], 'check-out', test_server.checkout_confirmation(self.app.store, booking['id']))
            else:
                now = int(time.time()*1000)
                with self.app.store.connect() as db:
                    db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000,now-1,booking['id']))
            self.assertEqual(self.extend(booking, draft)[0], 409)
            self.assertEqual(self.a.request('POST', f"/api/bookings/{booking['id']}/extension-quote", draft)[0], 409)
        self.assertEqual(self.b.book()[0], 201)
        self.assertEqual(self.extend(booking, draft)[0], 409)
        self.assertEqual(sum(s['occupied'] for s in self.a.state()['spaces']), 1)

    def test_overdue_checked_in_requires_future_end_and_keeps_space(self):
        booking = self.booking()
        self.app.store.admin_action(booking['id'], 'check-in')
        now = int(time.time()*1000)
        old_end = now-2*3600000
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (old_end-7200000,old_end,booking['id']))
        path = f"/api/bookings/{booking['id']}/extension-quote"
        self.assertEqual(self.a.request('POST', path, {'unit':'hour','duration':1})[0], 409)
        draft = self.quote(booking, duration=3)
        status, result = self.extend(booking, draft)
        self.assertEqual(status, 200)
        self.assertEqual(result['booking']['end'], old_end+3*3600000)
        self.assertEqual(result['booking']['total'], 400)
        self.assertIsNotNone(result['booking']['checkedInAt'])
        self.assertEqual(self.b.book()[0], 409)

    def test_competing_extensions_only_apply_once(self):
        booking = self.booking()
        a, b = self.quote(booking), self.quote(booking, 'day', 1)
        barrier = threading.Barrier(2)
        def save(draft):
            barrier.wait(); return self.extend(booking, draft)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, (a, b)))
        self.assertEqual(sorted(outcomes), [200, 409])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM booking_extensions').fetchone()[0], 1)

    def test_input_validation_and_client_total_cannot_override_price(self):
        booking = self.booking()
        path = f"/api/bookings/{booking['id']}/extension-quote"
        for unit, duration in [('week',1),('hour',0),('hour',25),('day',31),('hour',True),('hour',1.5),('day','2')]:
            self.assertEqual(self.a.request('POST', path, {'unit':unit,'duration':duration})[0], 400)
        self.assertEqual(self.extend(booking, {'unit':'hour','duration':2})[0], 400)
        draft = self.quote(booking)
        self.assertEqual(self.extend(booking, {**draft,'expectedTotal':0})[0], 409)
        status, result = self.extend(booking, {**draft,'newEnd':1,'total':1,'amount':0})
        self.assertEqual(status, 200)
        self.assertEqual(result['booking']['total'], 320)


if __name__ == '__main__':
    unittest.main()
