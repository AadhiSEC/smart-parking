"""Run with: python -m unittest -v test_server. Uses a disposable database."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import itertools
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
import server


def checkout_confirmation(store, booking_id):
    payment = store.payment_details(booking_id)
    return dict(expectedTotal=payment['total'], expectedPaid=payment['paid'], allowUnpaid=True)


class Client:
    vehicle_numbers = itertools.count(100, 20)

    def __init__(self, app):
        self.app, self.cookie, self.csrf = app, '', ''
        self.vehicle_base = next(self.vehicle_numbers)

    def request(self, method, path, data=None, extra=None, raw=None):
        port = self.app.server_port
        headers = {'Cookie': self.cookie, 'Host': f'localhost:{port}', 'Origin': f'http://localhost:{port}',
                   'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}
        headers.update(extra or {})
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        if response.getheader('Set-Cookie'):
            self.cookie = response.getheader('Set-Cookie').split(';')[0]
        content = response.read()
        status = response.status
        try: result = json.loads(content)
        except ValueError: result = content
        connection.close()
        if isinstance(result, dict) and 'csrfToken' in result:
            self.csrf = result['csrfToken']
        return status, result

    def state(self):
        status, data = self.request('GET', '/api/state')
        assert status == 200
        return data

    def book(self, **overrides):
        space = overrides.get('spaceId', 'T01')
        offset = {f'{prefix}{i:02d}': start+i for prefix, start in [('T', 0), ('B', 5)] for i in range(1, 6)}.get(space, 0)
        data = {'spaceId': 'T01', 'unit': 'hour', 'duration': 2, 'driver': 'Test Driver',
                'plate': f'TN 01 AA {self.vehicle_base+offset:04d}', 'requestId': secrets.token_hex(16)}
        data.update(overrides)
        return self.request('POST', '/api/bookings', data)

    def signup(self, email, name='Test Customer', password='Customer-test-only-4488'):
        return self.request('POST', '/api/customer/signup', {'name': name, 'email': email, 'password': password})

    def login(self, email, password='Customer-test-only-4488'):
        return self.request('POST', '/api/customer/login', {'email': email, 'password': password})


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PARKING_TEST_DIR'))
        self.app = server.make_server(0, Path(self.temp.name) / 'parking.sqlite3')
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.a, self.b = Client(self.app), Client(self.app)
        self.a.state(); self.b.state()
        self.assertEqual(self.a.signup('first@example.test')[0], 201)
        self.assertEqual(self.b.signup('second@example.test')[0], 201)

    def tearDown(self):
        self.app.shutdown(); self.app.server_close(); self.thread.join()
        self.temp.cleanup()

    def test_shared_availability_and_private_details(self):
        initial = self.a.state()
        self.assertEqual(len(initial['spaces']), 10)
        self.assertEqual(sum(s['type'] == 'truck' for s in initial['spaces']), 5)
        status, result = self.a.book()
        self.assertEqual(status, 201)
        self.assertEqual(result['booking']['total'], 160)
        self.assertEqual(result['booking']['end'] - result['booking']['start'], 7200000)
        other = self.b.state()
        self.assertEqual(sum(s['occupied'] for s in other['spaces']), 1)
        self.assertEqual(other['bookings'], [])
        self.assertNotIn('Test Driver', json.dumps(other))
        self.assertEqual(len(self.a.state()['bookings']), 1)

    def test_two_simultaneous_customers_only_one_wins(self):
        barrier = threading.Barrier(2)
        def reserve(client):
            barrier.wait()
            return client.book()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [self.a, self.b]))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(sum(s['occupied'] for s in self.a.state()['spaces']), 1)

    def test_server_rates_and_daily_duration(self):
        for space, unit, duration, expected in [('T01','day',2,1200), ('B01','hour',3,300), ('B02','day',2,1600)]:
            status, result = self.a.book(spaceId=space, unit=unit, duration=duration, total=1)
            self.assertEqual(status, 201)
            self.assertEqual(result['booking']['total'], expected)
            self.assertEqual(result['booking']['end']-result['booking']['start'], duration*(3600000 if unit=='hour' else 86400000))

    def test_input_validation(self):
        for overrides in [{'duration':0},{'duration':25},{'duration':1.5},{'duration':True},{'duration':'2'},
                          {'unit':'week'},{'unit':'day','duration':31},{'spaceId':'T99'},
                          {'driver':' '},{'plate':'X'*21},{'driver':'<script>'*10},{'requestId':'short'}]:
            with self.subTest(overrides=overrides): self.assertEqual(self.a.book(**overrides)[0],400)
        self.assertEqual(self.a.request('POST','/api/bookings',[])[0],400)
        self.assertEqual(sum(s['occupied'] for s in self.a.state()['spaces']),0)

    def test_retry_is_idempotent(self):
        request_id=secrets.token_hex(16)
        first=self.a.book(requestId=request_id)
        again=self.a.book(requestId=request_id)
        self.assertEqual((first[0],again[0]),(201,200))
        self.assertEqual(first[1],again[1])
        self.assertEqual(self.a.book(requestId=request_id,duration=3)[0],409)
        self.assertEqual(len(self.a.state()['bookings']),1)

    def test_release_ownership_and_release_all(self):
        _, result=self.a.book()
        identifier=result['booking']['id']
        self.assertEqual(self.b.request('POST',f'/api/bookings/{identifier}/release',{})[0],404)
        self.b.book(spaceId='B01')
        self.assertEqual(self.a.request('POST','/api/bookings/release-all',{})[0],200)
        self.assertEqual(len(self.a.state()['bookings']),0)
        self.assertEqual(len(self.b.state()['bookings']),1)
        identifier=self.b.state()['bookings'][0]['id']
        self.assertEqual(self.b.request('POST',f'/api/bookings/{identifier}/release',{})[0],200)
        self.assertEqual(sum(s['occupied'] for s in self.a.state()['spaces']),0)

    def test_expiry_uses_server_time(self):
        self.a.book()
        now=int(time.time()*1000)
        # Only this test's temporary database is edited to simulate an elapsed stay.
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?, end=?',(now-7200000,now-1))
        self.assertEqual(self.a.state()['bookings'],[])
        self.assertEqual(sum(s['occupied'] for s in self.b.state()['spaces']),0)
        self.assertEqual(self.b.book()[0],201)

    def test_database_survives_server_restart(self):
        self.a.book()
        path=self.app.store.path
        self.app.shutdown(); self.app.server_close(); self.thread.join()
        self.app=server.make_server(0,path)
        self.thread=threading.Thread(target=self.app.serve_forever,daemon=True); self.thread.start()
        self.a.app=self.app
        self.assertEqual(len(self.a.state()['bookings']),1)
        self.assertEqual(self.a.state()['bookings'][0]['total'],160)

    def test_request_and_file_protection(self):
        self.assertEqual(self.a.request('GET','/')[0],200)
        for path in ['/data/parking.sqlite3','/server.py','/../server.py','/README.md']:
            self.assertEqual(self.a.request('GET',path)[0],404)
        self.assertEqual(self.a.request('GET','/api/state',extra={'Host':'example.com'})[0],403)
        self.assertEqual(self.a.request('POST','/api/bookings',{},extra={'Origin':'https://other.example'})[0],403)
        self.assertEqual(self.a.request('POST','/api/bookings',{},extra={'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(Client(self.app).request('POST','/api/bookings',{})[0],401)
        self.assertEqual(self.a.request('POST','/api/bookings',raw=b'{bad')[0],400)
        self.assertEqual(self.a.request('POST','/api/bookings',raw=b'x'*5000)[0],413)


if __name__ == '__main__':
    unittest.main()
