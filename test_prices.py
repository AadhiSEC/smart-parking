"""Price settings, authorization, persistence and booking-price regression tests."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import unittest
import server
import test_admin
import test_server


class PriceTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown
    login = test_admin.AdminTests.login

    def draft(self, admin):
        state = admin.request('GET', '/api/admin/state')[1]
        return {'rates': state['rates'], 'priceRevision': state['priceRevision']}

    def test_persist_all_four_rates_and_preserve_existing_receipt(self):
        _, old = self.a.book(requestId='original-price-request')
        admin = self.login()
        data = self.draft(admin)
        self.assertEqual(data['rates'], server.RATES)
        data['rates'] = {'truck': {'hour': 120, 'day': 900}, 'bus': {'hour': 150, 'day': 1100}}
        self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 200)
        reopened = server.Store(self.app.store.path)
        with reopened.connect() as db:
            self.assertEqual(reopened.prices(db)['rates'], data['rates'])
        self.assertEqual(self.a.state()['rates'], data['rates'])
        for space, unit, expected in [('T02', 'hour', 240), ('T03', 'day', 1800), ('B01', 'hour', 300), ('B02', 'day', 2200)]:
            status, result = self.a.book(spaceId=space, unit=unit)
            self.assertEqual(status, 201)
            self.assertEqual(result['booking']['total'], expected)
        status, retry = self.a.book(requestId='original-price-request', expectedRate=80)
        self.assertEqual(status, 200)
        self.assertEqual(retry, old)
        receipt = self.a.request('GET', f"/api/bookings/{old['booking']['id']}/receipt")[1]['booking']
        self.assertEqual((receipt['rate'], receipt['total']), (80, 160))

    def test_invalid_prices_do_not_partially_save(self):
        admin = self.login()
        original = self.draft(admin)
        for invalid in [0, -1, 100001, 1.5, True, '120', None]:
            data = copy.deepcopy(original)
            data['rates']['truck']['hour'] = 321
            data['rates']['bus']['day'] = invalid
            self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 400)
            self.assertEqual(self.draft(admin), original)
        self.assertEqual(admin.request('POST', '/api/admin/prices', raw=b'null')[0], 400)
        for data in [[], {}, {'rates': {}}, {'rates': [], 'priceRevision': 1},
                     {'rates': {'truck': {'hour': 2}, 'bus': {}}, 'priceRevision': 1}]:
            self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 400)
        valid = copy.deepcopy(original)
        valid['rates'] = {'truck': {'hour': 1, 'day': 100000}, 'bus': {'hour': 100000, 'day': 1}}
        self.assertEqual(admin.request('POST', '/api/admin/prices', valid)[0], 200)

    def test_admin_authentication_csrf_origin_and_expiry(self):
        admin = self.login()
        data = self.draft(admin)
        self.assertEqual(self.a.request('POST', '/api/admin/prices', data)[0], 401)
        guest = test_server.Client(self.app)
        guest.request('GET', '/api/admin/session')
        self.assertEqual(guest.request('POST', '/api/admin/prices', data)[0], 401)
        for headers in [{'X-CSRF-Token': 'wrong'}, {'Origin': 'https://other.example'}]:
            self.assertEqual(admin.request('POST', '/api/admin/prices', data, extra=headers)[0], 403)
        with self.app.store.connect() as db:
            db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 401)
        self.assertEqual(self.a.state()['rates'], server.RATES)

    def test_simultaneous_admin_edits_cannot_overwrite_each_other(self):
        first, second = self.login(), self.login()
        a, b = self.draft(first), self.draft(second)
        a['rates']['truck']['hour'] = 111
        b['rates']['truck']['hour'] = 222
        barrier = threading.Barrier(2)
        def save(client, data):
            barrier.wait()
            return client.request('POST', '/api/admin/prices', data)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            x = pool.submit(save, first, a)
            y = pool.submit(save, second, b)
            results = [x.result(), y.result()]
        self.assertEqual(sorted(results), [200, 409])
        winning = a if results[0] == 200 else b
        self.assertEqual(self.a.state()['rates'], winning['rates'])

    def test_stale_customer_quote_requires_review_before_booking(self):
        admin = self.login()
        data = self.draft(admin)
        data['rates']['truck']['hour'] = 125
        self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 200)
        status, result = self.a.book(expectedRate=80)
        self.assertEqual(status, 409)
        self.assertIn('Review', result['error'])
        self.assertEqual(self.a.state()['bookings'], [])
        self.assertEqual(self.a.book(expectedRate=125)[1]['booking']['total'], 250)

    def test_existing_database_initializes_prices_only_once(self):
        _, original = self.a.book()
        with self.app.store.connect() as db:
            db.execute('DROP TABLE price_settings')
        upgraded = server.Store(self.app.store.path)
        self.assertEqual(upgraded.admin_state()['rates'], server.RATES)
        self.assertEqual(self.a.state()['bookings'][0]['total'], original['booking']['total'])
        admin = self.login()
        data = self.draft(admin)
        data['rates']['bus']['day'] = 999
        self.assertEqual(admin.request('POST', '/api/admin/prices', data)[0], 200)
        self.assertEqual(server.Store(self.app.store.path).admin_state()['rates']['bus']['day'], 999)


if __name__ == '__main__':
    unittest.main()
