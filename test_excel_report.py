"""Verify complete, styled, safe Excel downloads without additional packages."""
from copy import deepcopy
import io
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from excel_report import report_xlsx, outstanding_xlsx, tag, local_serial
import test_admin
import test_server


def sample_outstanding():
    rows=[dict(id=f'PK-{i:016X}',start=1794394800000+i*3600000,plate=f'TN 01 AB {1234+i}',
               driver=driver,spaceId=f'T0{i+1}',status='departed',total=200,paid=paid,waived=20,balance=180-paid,
               paymentStatus='Part paid' if paid else 'Unpaid')
          for i,(driver,paid) in enumerate([('=SUM(1,2)',80),('செல்வம் & Co',0),('Fleet driver with a longer name for wrapping',120)])]
    return dict(bookings=rows,count=3,balance=340,serverTime=1794402000000,
                filters=dict(search='=1+1',payment='all',status='departed'))


class OutstandingExcelTests(unittest.TestCase):
    def read(self,report):
        with ZipFile(io.BytesIO(outstanding_xlsx(report))) as book:
            self.assertIsNone(book.testzip())
            return {name:ET.fromstring(book.read(name)) for name in
                    ['xl/worksheets/sheet1.xml','xl/tables/table1.xml','xl/workbook.xml']}

    def test_values_formulas_literal_text_and_print_settings(self):
        report=sample_outstanding();original=deepcopy(report);parts=self.read(report)
        sheet=parts['xl/worksheets/sheet1.xml'];cells={c.get('r'):c for c in sheet.iter(tag('c'))}
        self.assertEqual(cells['E4'].find(tag('v')).text,'3')
        self.assertEqual(cells['I4'].find(tag('v')).text,'340')
        self.assertEqual(cells['I4'].find(tag('f')).text,'SUM(J13:J15)')
        for row in range(13,16):
            self.assertEqual(cells[f'J{row}'].find(tag('f')).text,f'G{row}-H{row}-I{row}')
            self.assertEqual(cells[f'G{row}'].get('t'),'n')
        self.assertEqual(cells['D13'].get('t'),'inlineStr')
        self.assertEqual(cells['D13'].find(f"{tag('is')}/{tag('t')}").text,'=SUM(1,2)')
        self.assertIsNone(cells['D13'].find(tag('f')))
        self.assertEqual(cells['B5'].find(f"{tag('is')}/{tag('t')}").text,'=1+1')
        self.assertEqual(float(cells['A13'].find(tag('v')).text),local_serial(report['bookings'][0]['start']))
        self.assertEqual(next(sheet.iter(tag('pane'))).get('ySplit'),'12')
        self.assertEqual(parts['xl/tables/table1.xml'].get('ref'),'A12:K15')
        self.assertEqual(sheet.find(tag('pageSetup')).get('fitToHeight'),'0')
        self.assertIn("'Outstanding'!$A$1:$K$15",[n.text for n in parts['xl/workbook.xml'].iter(tag('definedName'))])
        self.assertEqual(report,original)

    def test_empty_and_long_search(self):
        report=sample_outstanding();report.update(bookings=[],count=0,balance=0);report['filters']['search']='x'*100
        parts=self.read(report);sheet=parts['xl/worksheets/sheet1.xml']
        cells={c.get('r'):c for c in sheet.iter(tag('c'))}
        self.assertEqual(cells['I4'].find(tag('v')).text,'0')
        self.assertEqual(cells['E4'].find(tag('v')).text,'0')
        self.assertIn('No outstanding bookings',cells['A10'].find(f"{tag('is')}/{tag('t')}").text)
        self.assertTrue(all(len(cells[f'{c}13'])==0 for c in 'ABCDEFGHIJK'))
        self.assertGreaterEqual(float(sheet.find(f"{tag('sheetData')}/{tag('row')}[@r='5']").get('ht')),72)


class OutstandingDownloadTests(unittest.TestCase):
    setUp=test_server.BackendTests.setUp
    tearDown=test_server.BackendTests.tearDown
    login=test_admin.AdminTests.login

    def test_private_filtered_download_includes_all_pages_and_latest_payments(self):
        admin=self.login();_,created=self.a.book(driver='Fleet Driver');b=created['booking']
        self.app.store.admin_action(b['id'],'check-in')
        self.app.store.admin_action(b['id'],'check-out',test_server.checkout_confirmation(self.app.store,b['id']))
        with self.app.store.connect() as db:
            columns=[r['name'] for r in db.execute('PRAGMA table_info(bookings)')]
            seed=dict(db.execute('SELECT * FROM bookings WHERE id=?',(b['id'],)).fetchone())
            for i in range(501):
                row={**seed,'id':f'PK-{i:016X}','request_id':f'export-{i}','start':seed['start']+i+1,'end':seed['end']+i+1}
                db.execute('INSERT INTO bookings ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[row[k] for k in columns])
        self.app.store.record_payment(b['id'],dict(amount=60,method='cash',expectedPaid=0,expectedTotal=160,requestId='payment-export-test-0001'))
        path='/api/admin/outstanding.xlsx?search=fleet&payment=unpaid&status=departed&offset=500'
        self.assertEqual(self.a.request('GET',path)[0],401)
        self.assertEqual(test_server.Client(self.app).request('GET',path)[0],401)
        for suffix in ['payment=bad','search=a&search=b','offset=-1']:
            self.assertEqual(admin.request('GET','/api/admin/outstanding.xlsx?'+suffix)[0],400)
        status,content=admin.request('GET',path);self.assertEqual(status,200)
        with ZipFile(io.BytesIO(content)) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'));cells={c.get('r'):c for c in sheet.iter(tag('c'))}
            self.assertEqual(cells['E4'].find(tag('v')).text,'501')
            self.assertEqual(cells['I4'].find(tag('v')).text,str(501*160))
            self.assertIn('B513',cells)
            self.assertEqual(ET.fromstring(book.read('xl/tables/table1.xml')).get('ref'),'A12:K513')
        self.app.store.record_payment('PK-0000000000000000',dict(amount=160,method='upi',expectedPaid=0,expectedTotal=160,requestId='payment-export-test-0002'))
        with ZipFile(io.BytesIO(admin.request('GET',path)[1])) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(sheet.find(f".//{tag('c')}[@r='E4']/{tag('v')}").text,'500')


def sample_report():
    event = dict(event='Booking', time=1792369800000, bookingId='PK-0123456789ABCDEF',
                 spaceId='T01', type='truck', driver='=SUM(1,2)', plate='TN 01 AB 1234',
                 amount=160, currentStatus='reserved')
    return dict(date='2026-10-19', timezone='IST (UTC+05:30)', generatedAt=event['time'],
                summary=dict(bookings=1, extensions=0, arrivals=1, departures=0,
                             originalAmount=160, extensionAmount=0, bookedAmount=160),
                events=[event, {**event, 'event': 'Arrival', 'amount': None, 'driver': 'செல்வம் & Co'}])


class ExcelTests(unittest.TestCase):
    def read(self, report):
        with ZipFile(io.BytesIO(report_xlsx(report))) as book:
            self.assertIsNone(book.testzip())
            return {name: ET.fromstring(book.read(name)) for name in
                    ['xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml',
                     'xl/tables/table1.xml', 'xl/styles.xml']}

    def test_numeric_dates_amounts_formulas_and_safe_customer_text(self):
        report = sample_report()
        untouched = deepcopy(report)
        parts = self.read(report)
        summary = {c.get('r'): c for c in parts['xl/worksheets/sheet1.xml'].iter(tag('c'))}
        cells = {c.get('r'): c for c in parts['xl/worksheets/sheet2.xml'].iter(tag('c'))}
        self.assertEqual(summary['B16'].find(tag('v')).text, '160')
        self.assertEqual(summary['B16'].find(tag('f')).text, 'SUM(B14:B15)')
        self.assertIn('A5:A6', summary['B9'].find(tag('f')).text)
        self.assertEqual(float(cells['B5'].find(tag('v')).text), local_serial(report['events'][0]['time']))
        self.assertEqual(cells['B5'].get('t'), 'n')
        self.assertEqual(cells['H5'].get('t'), 'n')
        self.assertEqual(len(cells['H6']), 0)
        self.assertEqual(cells['F5'].get('t'), 'inlineStr')
        self.assertEqual(cells['F5'].find(f"{tag('is')}/{tag('t')}").text, '=SUM(1,2)')
        self.assertEqual(cells['F6'].find(f"{tag('is')}/{tag('t')}").text, 'செல்வம் & Co')
        self.assertFalse(list(parts['xl/worksheets/sheet2.xml'].iter(tag('f'))))
        self.assertEqual(report, untouched)

    def test_layout_filters_and_complete_long_report(self):
        report = sample_report()
        report['events'] = [dict(report['events'][0], driver='Long driver name ' * 4)] * 502
        parts = self.read(report)
        events = parts['xl/worksheets/sheet2.xml']
        self.assertEqual(len(events.find(tag('sheetData'))), 506)
        widths = [float(c.get('width')) for c in events.find(tag('cols'))]
        self.assertEqual(len(widths), 9)
        self.assertGreaterEqual(widths[5], 30)
        self.assertEqual(next(events.iter(tag('pane'))).get('ySplit'), '4')
        table = parts['xl/tables/table1.xml']
        self.assertEqual(table.get('ref'), 'A4:I506')
        self.assertEqual(table.find(tag('autoFilter')).get('ref'), 'A4:I506')
        styles = list(parts['xl/styles.xml'].find(tag('cellXfs')))
        row = events.find(f"{tag('sheetData')}/{tag('row')}[@r='506']")
        self.assertGreaterEqual(float(row.get('ht')), 56)
        self.assertEqual(styles[int(row[5].get('s'))].find(tag('alignment')).get('wrapText'), '1')

    def test_empty_day_removes_template_sample_and_returns_zero_totals(self):
        report = sample_report()
        report['events'] = []
        report['summary'] = dict.fromkeys(report['summary'], 0)
        parts = self.read(report)
        summary = parts['xl/worksheets/sheet1.xml']
        total = summary.find(f".//{tag('c')}[@r='B16']/{tag('v')}")
        self.assertEqual(total.text, '0')
        events = parts['xl/worksheets/sheet2.xml']
        row = events.find(f"{tag('sheetData')}/{tag('row')}[@r='5']")
        self.assertTrue(all(len(cell) == 0 for cell in row))
        self.assertIn('No parking events', ET.tostring(events, encoding='unicode'))


class ExcelDownloadTests(unittest.TestCase):
    setUp = test_server.BackendTests.setUp
    tearDown = test_server.BackendTests.tearDown
    login = test_admin.AdminTests.login

    def test_download_uses_current_saved_booking_and_requires_admin(self):
        from datetime import datetime
        import server
        path = '/api/admin/reports.xlsx?date=' + datetime.now(server.REPORT_TIMEZONE).strftime('%Y-%m-%d')
        self.assertEqual(self.a.request('GET', path)[0], 401)
        self.assertEqual(self.a.book()[0], 201)
        admin = self.login()
        status, content = admin.request('GET', path)
        self.assertEqual(status, 200)
        with ZipFile(io.BytesIO(content)) as book:
            summary = ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(summary.find(f".//{tag('c')}[@r='B9']/{tag('v')}").text, '1')
            self.assertEqual(summary.find(f".//{tag('c')}[@r='B16']/{tag('v')}").text, '160')
        self.assertEqual(admin.request('GET', '/api/admin/reports.xlsx?date=invalid')[0], 400)
        with self.app.store.connect() as db:
            db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(admin.request('GET', path)[0], 401)


if __name__ == '__main__':
    unittest.main()
