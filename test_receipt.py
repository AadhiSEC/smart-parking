"""Receipt ownership and original-price regression tests; disposable databases only."""
import json
import secrets
import test_extensions
import time
import unittest
import server
import test_server


class ReceiptTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def book_receipt(self, **overrides):
        status, result = self.a.book(**overrides)
        self.assertEqual(status, 201)
        return f"/api/bookings/{result['booking']['id']}/receipt", result['booking']

    def test_owner_receipt_preserves_original_price_and_terms(self):
        path, booking = self.book_receipt(spaceId='B01', unit='day', duration=2)
        with self.app.store.connect() as db:
            db.execute('UPDATE price_settings SET bus_day=999 WHERE id=1')
        status, result = self.a.request('GET', path)
        self.assertEqual(status, 200)
        receipt = result['booking']
        for field, value in booking.items():
            self.assertEqual(receipt[field], value)
        self.assertEqual((receipt['unit'], receipt['duration'], receipt['rate'], receipt['total']), ('day', 2, 800, 1600))
        self.assertEqual(receipt['type'], 'bus')
        self.assertEqual(receipt['status'], 'reserved')
        self.assertIsNotNone(result['generatedAt'])
        for secret in ('owner', 'customer_id', 'password', 'request_body', 'request_id'):
            self.assertNotIn(secret, json.dumps(result))

    def test_other_customer_guest_and_expired_session_cannot_read_receipt(self):
        path, _ = self.book_receipt()
        status, result = self.b.request('GET', path)
        self.assertEqual(status, 404)
        self.assertNotIn('Test Driver', json.dumps(result))
        guest = test_server.Client(self.app)
        self.assertEqual(guest.request('GET', path)[0], 401)
        guest.state()
        self.assertEqual(guest.request('GET', path)[0], 404)
        self.a.request('POST', '/api/customer/logout', {})
        self.assertEqual(self.a.request('GET', path)[0], 404)

    def test_account_receipt_available_in_second_browser_and_history(self):
        path, booking = self.book_receipt()
        second = test_server.Client(self.app)
        second.state(); self.assertEqual(second.login('first@example.test')[0], 200)
        self.assertEqual(second.request('GET', path)[0], 200)
        self.assertEqual(second.request('POST', f"/api/bookings/{booking['id']}/release", {})[0], 200)
        self.assertEqual(self.a.request('GET', path)[1]['booking']['status'], 'released')
        self.assertEqual(self.a.request('GET', path)[1]['booking']['total'], 160)

    def test_receipt_tracks_arrival_departure_cancel_and_expiry(self):
        path, booking = self.book_receipt()
        self.app.store.admin_action(booking['id'], 'check-in')
        receipt = self.a.request('GET', path)[1]['booking']
        self.assertEqual(receipt['status'], 'parked')
        self.assertIsNotNone(receipt['checkedInAt'])
        self.app.store.admin_action(booking['id'], 'check-out', test_server.checkout_confirmation(self.app.store, booking['id']))
        receipt = self.a.request('GET', path)[1]['booking']
        self.assertEqual(receipt['status'], 'departed')
        self.assertIsNotNone(receipt['checkedOutAt'])
        path, booking = self.book_receipt()
        self.app.store.admin_action(booking['id'], 'cancel')
        self.assertEqual(self.a.request('GET', path)[1]['booking']['status'], 'cancelled')
        path, booking = self.book_receipt()
        now = int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000, now-1, booking['id']))
        self.assertEqual(self.a.request('GET', path)[1]['booking']['status'], 'expired')

    def test_receipt_payment_totals_corrections_extension_and_privacy(self):
        path, booking = self.book_receipt()
        def payment():
            status, result = self.a.request('GET', path)
            self.assertEqual(status, 200)
            return result['payment']
        def record(amount, method, paid):
            return self.app.store.record_payment(booking['id'], dict(amount=amount, method=method,
                expectedPaid=paid, expectedTotal=160, requestId=secrets.token_hex(16)))['paymentId']
        self.assertEqual(payment(), dict(waived=0, paid=0, balance=160, cash=0, upi=0, status='Unpaid'))
        cash = record(60, 'cash', 0)
        self.assertEqual(payment(), dict(waived=0, paid=60, balance=100, cash=60, upi=0, status='Part paid'))
        upi = record(100, 'upi', 60)
        self.assertEqual(payment(), dict(waived=0, paid=160, balance=0, cash=60, upi=100, status='Paid'))
        status, other = self.b.request('GET', path)
        self.assertEqual(status, 404)
        self.assertNotIn('payment', other)
        self.app.store.void_payment(booking['id'], cash, {'reason': 'Private staff correction note'})
        self.assertEqual(payment(), dict(waived=0, paid=100, balance=60, cash=0, upi=100, status='Part paid'))
        self.assertNotIn('Private staff correction note', json.dumps(self.a.request('GET', path)[1]))
        quote = test_extensions.ExtensionTests.quote(self, booking, duration=1)
        self.assertEqual(self.a.request('POST', f"/api/bookings/{booking['id']}/extend", quote)[0], 200)
        self.assertEqual(payment(), dict(waived=0, paid=100, balance=140, cash=0, upi=100, status='Part paid'))
        self.app.store = server.Store(self.app.store.path)
        self.app.store.admin_action(booking['id'], 'cancel')
        self.assertEqual(payment()['paid'], 100)
        self.app.store.void_payment(booking['id'], upi, {'reason': 'Wrong booking'})
        self.assertEqual(payment(), dict(waived=0, paid=0, balance=240, cash=0, upi=0, status='Unpaid'))

    def test_receipt_assets_and_nonexistent_ids(self):
        for path in ['/receipt', '/receipt.html', '/receipt.js?v=1', '/receipt.css?v=1']:
            self.assertEqual(self.a.request('GET', path)[0], 200)
        self.assertEqual(self.a.request('GET', '/api/bookings/PK-0000000000000000/receipt')[0], 404)
        self.assertEqual(self.a.request('GET', '/api/bookings/invalid/receipt')[0], 404)


if __name__ == '__main__':
    unittest.main()
