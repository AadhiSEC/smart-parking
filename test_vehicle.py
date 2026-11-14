"""Registration format and one-active-booking-per-vehicle tests."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest
import server
import test_server


class RegistrationFormatTests(unittest.TestCase):
    def test_supported_formats_and_equivalent_spellings(self):
        for raw, expected in [
            ('tn01ab1234', 'TN 01 AB 1234'), ('TN-01-AB-1234', 'TN 01 AB 1234'),
            (' TN 1 AB 7 ', 'TN 01 AB 0007'), ('DL1PC1234', 'DL 01 PC 1234'),
            ('DL01CAA1234', 'DL 01 CAA 1234'), ('WB55AB5577', 'WB 55 AB 5577'),
            ('TS09AB1234', 'TS 09 AB 1234'), ('TG09AB1234', 'TG 09 AB 1234'),
            ('OR02AB1234', 'OR 02 AB 1234'), ('OD02AB1234', 'OD 02 AB 1234'),
        ]:
            with self.subTest(raw=raw): self.assertEqual(server.normalize_plate(raw), expected)

    def test_invalid_or_unsupported_numbers(self):
        for value in ['', None, [], 'TEST 01', '12345', 'ZZ01AB1234', 'TN00AB1234',
                      'TN01AB0000', 'TN01AB12345', 'TN01ABCD1234', 'TN01AB12.34',
                      'TN01AB12/34', 'TN01AB1234\n', 'ＴＮ01AB1234', 'TN01Aß1234',
                      'TN\t01AB1234', '26BH1234AA', '123456789012345678901',
                      'WB 55 5577', 'WB555577', 'wb-55-5577', 'KA011234']:
            with self.subTest(value=value), self.assertRaises(server.ApiError) as error:
                server.normalize_plate(value)
            self.assertEqual(error.exception.status, 400)


class VehicleBookingTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def test_format_validation_is_enforced_on_the_server(self):
        for plate in ['WB 55 5577', 'WB555577', 'wb-55-5577']:
            self.assertEqual(self.a.book(spaceId='B04', plate=plate)[0], 400)
        self.assertFalse(next(s['occupied'] for s in self.a.state()['spaces'] if s['id'] == 'B04'))
        self.assertEqual(self.a.book(plate='NOT A PLATE')[0], 400)
        self.assertEqual(self.a.book(plate='ZZ01AB1234')[0], 400)
        self.assertEqual(self.a.book(plate='26BH1234AA')[0], 400)
        status, result = self.a.book(plate='tn-01-ab-1234')
        self.assertEqual(status, 201)
        self.assertEqual(result['booking']['plate'], 'TN 01 AB 1234')

    def test_preexisting_no_series_booking_can_still_be_viewed_and_released(self):
        booking = self.a.book(spaceId='B04', plate='WB55AB5577')[1]['booking']
        with self.app.store.connect() as db:
            row = db.execute('SELECT request_body FROM bookings WHERE id=?', (booking['id'],)).fetchone()
            original = json.loads(row[0]); original[4] = 'WB 55 5577'
            db.execute('UPDATE bookings SET plate=?,request_body=? WHERE id=?', ('WB 55 5577', json.dumps(original), booking['id']))
        self.assertEqual(self.a.state()['bookings'][0]['plate'], 'WB 55 5577')
        self.assertEqual(self.a.request('GET', f"/api/bookings/{booking['id']}/receipt")[0], 200)
        self.assertEqual(self.a.request('POST', f"/api/bookings/{booking['id']}/release", {})[0], 200)

    def test_duplicate_across_accounts_and_vehicle_types_is_private(self):
        self.assertEqual(self.a.book(plate='TN01AB0007', driver='Private Name')[0], 201)
        status, result = self.b.book(spaceId='B01', plate='tn-1-ab-7')
        self.assertEqual(status, 409)
        self.assertIn('vehicle already has an active booking', result['error'])
        self.assertNotIn('Private Name', json.dumps(result))
        self.assertNotIn('T01', json.dumps(result))
        self.assertEqual(self.a.book(spaceId='T02', plate='TN01AB0007')[0], 409)

    def test_race_for_different_spaces_only_one_succeeds(self):
        barrier = threading.Barrier(2)
        def book(args):
            client, space = args
            barrier.wait()
            return client.book(spaceId=space, plate='TN01AB1234')[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(book, [(self.a, 'T01'), (self.b, 'B01')]))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(sum(s['occupied'] for s in self.a.state()['spaces']), 1)

    def test_retry_normalizes_plate_and_does_not_duplicate(self):
        first = self.a.book(plate='tn-1-ab-7', requestId='vehicle-retry-test-0001')
        second = self.a.book(plate='TN 01 AB 0007', requestId='vehicle-retry-test-0001')
        self.assertEqual((first[0], second[0]), (201, 200))
        self.assertEqual(first[1], second[1])

    def test_release_expiry_and_checkout_allow_the_next_booking(self):
        booking = self.a.book(plate='TN01AB1234')[1]['booking']
        self.a.request('POST', f"/api/bookings/{booking['id']}/release", {})
        status, result = self.b.book(spaceId='B01', plate='TN01AB1234')
        self.assertEqual(status, 201)
        now = int(time.time()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000, now-1, result['booking']['id']))
        status, result = self.a.book(plate='TN01AB1234')
        self.assertEqual(status, 201)
        identifier = result['booking']['id']
        self.app.store.admin_action(identifier, 'check-in')
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?', (now-7200000, now-1, identifier))
        self.assertEqual(self.b.book(spaceId='B01', plate='TN01AB1234')[0], 409)
        self.app.store.admin_action(identifier, 'check-out', test_server.checkout_confirmation(self.app.store, identifier))
        self.assertEqual(self.b.book(spaceId='B01', plate='TN01AB1234')[0], 201)

    def test_existing_unformatted_record_still_blocks_duplicates(self):
        booking = self.a.book(plate='TN01AB1234')[1]['booking']
        with self.app.store.connect() as db:
            row = db.execute('SELECT request_body FROM bookings WHERE id=?', (booking['id'],)).fetchone()
            original = json.loads(row[0]); original[4] = 'tn-01-ab-1234'
            db.execute('UPDATE bookings SET plate=?,request_body=? WHERE id=?', ('tn-01-ab-1234', json.dumps(original), booking['id']))
        self.assertEqual(self.b.book(spaceId='B01', plate='TN01AB1234')[0], 409)
        receipt = self.a.request('GET', f"/api/bookings/{booking['id']}/receipt")
        self.assertEqual(receipt[0], 200)
        self.assertEqual(receipt[1]['booking']['plate'], 'tn-01-ab-1234')


if __name__ == '__main__':
    unittest.main()
