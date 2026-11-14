"""Daily report dates, amounts, access and Excel-safe CSV regression tests."""
import csv
from datetime import datetime
import io
import unittest
import server
import test_admin
import test_server


class ReportTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown
    login = test_admin.AdminTests.login
    day = '2026-10-18'
    start = int(datetime(2026,10,18,tzinfo=server.REPORT_TIMEZONE).timestamp()*1000)

    def booking(self, **overrides):
        status, result = self.a.book(**overrides)
        self.assertEqual(status, 201)
        return result['booking']['id']

    def get_report(self, admin=None, day=None):
        status, report = (admin or self.login()).request('GET', '/api/admin/reports?date='+(day or self.day))
        self.assertEqual(status, 200, report)
        return report

    def test_empty_valid_dates_invalid_dates_and_authentication(self):
        admin = self.login()
        report = self.get_report(admin)
        self.assertEqual(report['events'], [])
        self.assertTrue(all(v==0 for v in report['summary'].values()))
        self.assertEqual(self.get_report(admin,'2024-02-29')['date'], '2024-02-29')
        for date in ['', '2026-02-29', '2026-13-01', '2026-10-18x', '26-10-18', '9999-12-31']:
            for path in ['/api/admin/reports?date=', '/api/admin/reports.csv?date=']:
                self.assertEqual(admin.request('GET',path+date)[0], 400)
        guest = test_server.Client(self.app); guest.request('GET','/api/admin/session')
        for client in [self.a, guest, test_server.Client(self.app)]:
            for path in ['/api/admin/reports?date=', '/api/admin/reports.csv?date=']:
                self.assertEqual(client.request('GET',path+self.day)[0], 401)
        with self.app.store.connect() as db:
            db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(admin.request('GET','/api/admin/reports.csv?date='+self.day)[0], 401)

    def test_ist_midnight_boundaries_and_movements_from_older_bookings(self):
        identifiers = [self.booking(spaceId=f'T0{i}') for i in range(1,5)]
        with self.app.store.connect() as db:
            for identifier, at in zip(identifiers, [self.start-1,self.start,self.start+86400000-1,self.start+86400000]):
                db.execute('UPDATE bookings SET start=?,end=? WHERE id=?',(at,at+7200000,identifier))
            db.execute('UPDATE bookings SET checked_in_at=?,checked_out_at=?,released=1 WHERE id=?',
                       (self.start,self.start+86400000-1,identifiers[0]))
            db.execute('UPDATE bookings SET checked_in_at=?,checked_out_at=?,released=1 WHERE id=?',
                       (self.start+86400000,self.start+86400001,identifiers[3]))
        report = self.get_report()
        self.assertEqual(report['summary']['bookings'],2)
        self.assertEqual(report['summary']['arrivals'],1)
        self.assertEqual(report['summary']['departures'],1)
        self.assertEqual(report['summary']['bookedAmount'],320)
        self.assertEqual({e['bookingId'] for e in report['events'] if e['event']=='Booking'},set(identifiers[1:3]))
        self.assertEqual(len(report['events']),4)

    def test_original_and_extension_amounts_stay_on_their_own_dates(self):
        identifier = self.booking()
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=?,total=400 WHERE id=?',(self.start+1000,self.start+100000000,identifier))
            for i,(at,amount) in enumerate([(self.start+2000,80),(self.start+86400000,160)]):
                db.execute('INSERT INTO booking_extensions(booking_id,request_id,request_body,unit,duration,rate,amount,previous_end,new_end,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (identifier,f'report-ext-{i}','[]','hour',1,amount,amount,self.start+7200000,self.start+10800000,at))
        report = self.get_report()
        self.assertEqual(report['summary']['originalAmount'],160)
        self.assertEqual(report['summary']['extensionAmount'],80)
        self.assertEqual(report['summary']['bookedAmount'],240)
        following = self.get_report(day='2026-10-19')
        self.assertEqual(following['summary']['bookings'],0)
        self.assertEqual(following['summary']['extensions'],1)
        self.assertEqual(following['summary']['bookedAmount'],160)
        self.assertEqual(self.get_report()['summary'],report['summary'])

    def test_cancelled_and_released_bookings_are_labelled_gross_not_payments(self):
        first, second = self.booking(), self.booking(spaceId='B01')
        self.app.store.admin_action(first,'cancel')
        self.a.request('POST',f'/api/bookings/{second}/release',{})
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=?',(self.start+1000,self.start+7200000))
        report = self.get_report()
        self.assertEqual(report['summary']['bookedAmount'],360)
        self.assertEqual({e['currentStatus'] for e in report['events']},{'cancelled','released'})
        self.assertIn('Not payments received',report['note'])

    def test_csv_unicode_quoting_formula_protection_and_totals(self):
        identifier = self.booking(driver='=SUM(1,2)')
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET start=?,end=?,plate=? WHERE id=?',
                       (self.start,self.start+7200000,'  @legacy',identifier))
        admin = self.login()
        status, content = admin.request('GET','/api/admin/reports.csv?date='+self.day)
        self.assertEqual(status,200)
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        self.assertIn(['Gross booked amount (INR; not payments)','160'],rows)
        event=next(row for row in rows if row and row[0]=='Booking')
        self.assertEqual(event[1],'2026-10-18 00:00:00')
        self.assertEqual(event[5],"'=SUM(1,2)")
        self.assertEqual(event[6],"'  @legacy")
        with self.app.store.connect() as db:
            db.execute('UPDATE bookings SET driver=?',('அருண், "Driver"',))
        content=admin.request('GET','/api/admin/reports.csv?date='+self.day)[1]
        rows=list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        self.assertEqual(next(row for row in rows if row and row[0]=='Booking')[5],'அருண், "Driver"')

    def test_report_includes_all_records_beyond_dashboard_limit_and_is_read_only(self):
        identifier=self.booking()
        with self.app.store.connect() as db:
            original=dict(db.execute('SELECT * FROM bookings WHERE id=?',(identifier,)).fetchone())
            db.execute('UPDATE bookings SET start=?,end=? WHERE id=?',(self.start,self.start+7200000,identifier))
            for i in range(501):
                db.execute('INSERT INTO bookings(id,owner,space_id,driver,plate,start,end,total,released,request_id,request_body,customer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (f'PK-{i:016X}',original['owner'],'T01','Report test',original['plate'],self.start+1000,self.start+7200000,160,1,f'report-{i}',original['request_body'],original['customer_id']))
        before=self.app.store.admin_state()['totalRecords']
        admin=self.login(); report=self.get_report(admin)
        self.assertEqual(report['summary']['bookings'],502)
        self.assertEqual(report['summary']['bookedAmount'],502*160)
        self.assertEqual(len(report['events']),502)
        self.assertEqual(self.app.store.admin_state()['totalRecords'],before)


if __name__=='__main__':
    unittest.main()
