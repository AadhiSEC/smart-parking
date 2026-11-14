"""Booking search checks use disposable customer accounts and databases."""
import json
import unittest
from urllib.parse import quote
import test_server


class SearchTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown

    def search(self, query, client=None):
        return (client or self.a).request('GET', '/api/state?search=' + quote(query))[1]

    def test_partial_registration_case_spaces_and_hyphens(self):
        first = self.a.book(plate='TN01AB1234')[1]['booking']
        self.a.book(spaceId='T02', plate='TN01XY5678')
        for query in ['tn01ab1234', 'TN-01-AB-1234', '  tn 01 ab 1234  ', 'AB12', '1234']:
            with self.subTest(query=query):
                result = self.search(query)
                self.assertEqual([b['id'] for b in result['bookings']], [first['id']])
                self.assertEqual(result['bookingsTotal'], 2)
                self.assertEqual(sum(s['occupied'] for s in result['spaces']), 2)

    def test_space_booking_id_and_clear(self):
        first = self.a.book()[1]['booking']
        self.a.book(spaceId='B01')
        for query in ['t01', first['id'].lower(), first['id'].replace('-', ''), first['id'][-8:]]:
            self.assertEqual([b['id'] for b in self.search(query)['bookings']], [first['id']])
        self.assertEqual(len(self.search('')['bookings']), 2)
        self.assertEqual(len(self.search('   ')['bookings']), 2)

    def test_history_search_and_separate_counts(self):
        first = self.a.book(plate='TN01AB1234')[1]['booking']
        self.a.request('POST', f"/api/bookings/{first['id']}/release", {})
        self.a.book(spaceId='B01', plate='TN01AB5678')
        result = self.search('T01')
        self.assertEqual(result['bookings'], [])
        self.assertEqual(result['bookingsTotal'], 1)
        self.assertEqual(result['historyTotal'], 1)
        self.assertEqual(result['historyMatches'], 1)
        self.assertEqual(result['history'][0]['id'], first['id'])
        self.assertEqual(result['history'][0]['status'], 'released')

    def test_search_finds_records_older_than_latest_100(self):
        first = self.a.book(plate='TN01AB1234')[1]['booking']
        self.a.request('POST', f"/api/bookings/{first['id']}/release", {})
        with self.app.store.connect() as db:
            for i in range(105):
                db.execute('''INSERT INTO bookings(id,owner,space_id,driver,plate,start,end,total,released,request_id,request_body,customer_id)
                    SELECT ?,owner,space_id,driver,'TN 01 ZZ 9999',start+?,end+?,total,1,?,request_body,customer_id
                    FROM bookings WHERE id=?''', (f'PK-{i:016X}', (i+1)*10, (i+1)*10, f'search-history-{i:016d}', first['id']))
        recent = self.search('')
        self.assertEqual(len(recent['history']), 100)
        self.assertNotIn(first['id'], [b['id'] for b in recent['history']])
        result = self.search('TN01AB1234')
        self.assertEqual(result['historyTotal'], 106)
        self.assertEqual(result['historyMatches'], 1)
        self.assertEqual([b['id'] for b in result['history']], [first['id']])
        self.assertEqual(self.search('ZZ9999')['historyMatches'], 105)

    def test_other_customers_and_signed_out_visitors_cannot_search_private_records(self):
        first = self.a.book(driver='Private Driver', plate='TN01AB1234')[1]['booking']
        guest = test_server.Client(self.app); guest.state()
        for client in [self.b, guest]:
            for query in ['TN01AB1234', first['id'], 'T01']:
                result = self.search(query, client)
                self.assertEqual(result['bookings'], [])
                self.assertEqual(result['history'], [])
                self.assertEqual(result['bookingsTotal'], 0)
                self.assertNotIn('Private Driver', json.dumps(result))
        same_account = test_server.Client(self.app); same_account.state()
        self.assertEqual(same_account.login('first@example.test')[0], 200)
        self.assertEqual(self.search('TN01AB1234', same_account)['bookings'][0]['id'], first['id'])

    def test_no_results_and_special_characters_are_literal(self):
        self.a.book()
        for query in ['NOTFOUND', '%', '_', "' OR 1=1 --", '<script>']:
            result = self.search(query)
            self.assertEqual(result['bookings'], [])
            self.assertEqual(result['historyMatches'], 0)
            self.assertEqual(result['bookingsTotal'], 1)
        self.assertEqual(self.a.request('GET', '/api/state?search=' + 'x'*81)[0], 400)


if __name__ == '__main__':
    unittest.main()
