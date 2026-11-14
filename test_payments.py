from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import io
from urllib.parse import urlencode
from pathlib import Path
import secrets
import sqlite3
import threading
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile
import backups
import server
import test_admin
import test_extensions
import test_server
from excel_report import tag, report_xlsx


class PaymentTests(unittest.TestCase):
    setUp=test_server.BackendTests.setUp
    tearDown=test_server.BackendTests.tearDown
    login=test_admin.AdminTests.login

    def setup_booking(self):
        status,data=self.a.book();self.assertEqual(status,201)
        return self.login(),data['booking']

    def payload(self,amount=160,method='cash',paid=0,total=160):
        return dict(amount=amount,method=method,expectedPaid=paid,expectedTotal=total,requestId=secrets.token_hex(16))

    def path(self,b):return f"/api/admin/bookings/{b['id']}/payments"

    def test_outstanding_auth_empty_and_pagination_inputs(self):
        path='/api/admin/outstanding'
        self.assertEqual(self.a.request('GET',path)[0],401)
        admin=self.login()
        status,data=admin.request('GET',path)
        self.assertEqual(status,200)
        self.assertEqual((data['count'],data['balance'],data['bookings']),(0,0,[]))
        for query in ['offset=', 'offset=-1', 'offset=1.5', 'offset=abc', 'offset=1000000001', 'offset=0&offset=50']:
            self.assertEqual(admin.request('GET',path+'?'+query)[0],400,query)
        self.assertEqual(admin.request('GET',path+'?offset=500')[1]['offset'],0)

    def test_outstanding_payments_checkout_correction_and_history(self):
        admin,b=self.setup_booking();_,other=self.b.book(spaceId='B01')
        path='/api/admin/outstanding'
        def get():
            status,data=admin.request('GET',path);self.assertEqual(status,200);return data
        self.assertEqual((get()['count'],get()['balance']),(2,360))
        cash=admin.request('POST',self.path(b),self.payload(60))[1]['paymentId']
        self.app.store.admin_action(b['id'],'check-in')
        self.app.store.admin_action(b['id'],'check-out',test_server.checkout_confirmation(self.app.store,b['id']))
        data=get();row=next(r for r in data['bookings'] if r['id']==b['id'])
        self.assertEqual((data['balance'],row['balance'],row['status'],row['paymentStatus']),(300,100,'departed','Part paid'))
        self.assertFalse({'owner','customer_id','request_body','request_id'} & row.keys())
        self.assertEqual(admin.request('POST',self.path(b),self.payload(100,'upi',60))[0],200)
        self.assertEqual((get()['count'],get()['balance']),(1,200))
        self.app.store.void_payment(b['id'],cash,{'reason':'Wrong entry'})
        self.app.store.admin_action(other['booking']['id'],'cancel')
        self.app.store=server.Store(self.app.store.path)
        data=get();self.assertEqual((data['count'],data['balance']),(2,260))
        self.assertEqual({r['status'] for r in data['bookings']},{'departed','cancelled'})

    def test_outstanding_older_than_dashboard_limit_and_all_page_totals(self):
        admin,b=self.setup_booking()
        self.app.store.admin_action(b['id'],'check-in')
        self.app.store.admin_action(b['id'],'check-out',test_server.checkout_confirmation(self.app.store,b['id']))
        with self.app.store.connect() as db:
            columns=[r['name'] for r in db.execute('PRAGMA table_info(bookings)')]
            seed=dict(db.execute('SELECT * FROM bookings WHERE id=?',(b['id'],)).fetchone())
            for i in range(501):
                row={**seed,'driver':'Archive Fleet' if i<51 else 'Other Driver','id':f'PK-{i:016X}','request_id':f'old-booking-{i}','start':seed['start']+i+1,'end':seed['end']+i+1}
                db.execute('INSERT INTO bookings ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[row[k] for k in columns])
        self.assertNotIn(b['id'],[r['id'] for r in self.app.store.admin_state()['bookings']])
        page=self.app.store.outstanding_payments()
        self.assertEqual((page['count'],page['balance'],len(page['bookings'])),(502,502*160,50))
        self.assertEqual(page['bookings'][0]['id'],b['id'])
        ids=[]
        for offset in range(0,502,50):
            data=admin.request('GET',f'/api/admin/outstanding?offset={offset}')[1]
            self.assertEqual(data['balance'],502*160);ids.extend(r['id'] for r in data['bookings'])
        self.assertEqual(len(set(ids)),502)
        last=self.app.store.outstanding_payments(1000)
        self.assertEqual((last['offset'],len(last['bookings'])),(500,2))
        filtered=self.app.store.outstanding_payments(search='archive',payment='unpaid',status='departed')
        self.assertEqual((filtered['count'],filtered['balance'],len(filtered['bookings'])),(51,51*160,50))
        last=self.app.store.outstanding_payments(50,search='archive',payment='unpaid',status='departed')
        self.assertEqual((last['count'],last['balance'],len(last['bookings'])),(51,51*160,1))
        old=self.app.store.outstanding_payments(search=b['id'])
        self.assertEqual([r['id'] for r in old['bookings']],[b['id']])
        identifier=last['bookings'][0]['id']
        self.app.store.record_payment(identifier,self.payload())
        last=self.app.store.outstanding_payments(50,search='archive',payment='unpaid',status='departed')
        self.assertEqual((last['offset'],last['count'],last['balance']),(0,50,50*160))


    def test_outstanding_search_and_combined_filters(self):
        admin,b=self.setup_booking()
        status,other=self.b.book(spaceId='B01',driver='Élodie Fleet 50%_');self.assertEqual(status,201)
        status,third=self.a.book(spaceId='T02',driver='Fleet North');self.assertEqual(status,201)
        admin.request('POST',self.path(b),self.payload(60))
        self.app.store.admin_action(b['id'],'check-in')
        self.app.store.admin_action(b['id'],'check-out',test_server.checkout_confirmation(self.app.store,b['id']))
        self.app.store.admin_action(other['booking']['id'],'cancel')
        self.app.store.admin_action(third['booking']['id'],'check-in')
        def result(**filters):
            status,data=admin.request('GET','/api/admin/outstanding?'+urlencode(filters));self.assertEqual(status,200);return data
        self.assertEqual((result()['count'],result()['balance']),(3,460))
        for filters,count,balance in [({'payment':'unpaid'},2,360),({'payment':'partial'},1,100),
            ({'status':'departed'},1,100),({'payment':'unpaid','status':'departed'},0,0),
            ({'search':'fLeEt'},2,360),({'search':'ÉLODIE'},1,200),({'search':'%'},1,200),
            ({'search':'_'},1,200),({'search':"' OR 1=1 --"},0,0),({'search':'-'},0,0),
            ({'search':'Fleet','payment':'partial'},0,0)]:
            data=result(**filters);self.assertEqual((data['count'],data['balance']),(count,balance),filters)
        for term in [b['plate'].replace(' ', '').lower(), b['plate'].replace(' ','-'), b['id'].replace('-','').lower(), b['id'][-12:]]:
            data=result(search=term,payment='partial',status='departed')
            self.assertEqual([r['id'] for r in data['bookings']],[b['id']])
            self.assertEqual(data['balance'],100)
        data=result(search='  Fleet  ');self.assertEqual(data['filters'],dict(search='Fleet',payment='all',status='all'))
        self.assertEqual(self.a.request('GET','/api/admin/outstanding?search=Fleet')[0],401)
        for query in ['payment=paid','status=parked','search=a&search=b','payment=all&payment=unpaid','status=all&status=departed',urlencode({'search':'x'*101}),urlencode({'search':'a'+chr(0)+'b'}),urlencode({'search':'a'+chr(10)+'b'})]:
            self.assertEqual(admin.request('GET','/api/admin/outstanding?'+query)[0],400,query)
        self.assertEqual(admin.request('POST',self.path(b),self.payload(100,'upi',60))[0],200)
        self.assertEqual(result(payment='partial',status='departed')['count'],0)

    def test_partial_full_retry_extension_and_history(self):
        admin,b=self.setup_booking();path=self.path(b)
        draft=self.payload(60)
        self.assertEqual(admin.request('POST',path,draft)[0],200)
        self.assertEqual(admin.request('POST',path,draft)[0],200)
        self.assertEqual(admin.request('POST',path,{**draft,'amount':61})[0],409)
        self.assertEqual(self.app.store.admin_state()['bookings'][0]['paymentStatus'],'Part paid')
        self.assertEqual(admin.request('POST',path,self.payload(100,'upi',60))[0],200)
        self.assertEqual(self.app.store.admin_state()['bookings'][0]['paymentStatus'],'Paid')
        self.assertEqual(admin.request('POST',path,self.payload(1,'cash',160))[0],409)
        extension=test_extensions.ExtensionTests.quote(self,b,duration=1)
        self.assertEqual(self.a.request('POST',f"/api/bookings/{b['id']}/extend",extension)[0],200)
        state=self.app.store.admin_state()['bookings'][0]
        self.assertEqual((state['paid'],state['balance'],state['paymentStatus']),(160,80,'Part paid'))
        self.assertEqual(len(admin.request('GET',path)[1]['entries']),2)
        self.app.store=server.Store(self.app.store.path)
        self.assertEqual(self.app.store.payment_details(b['id'])['paid'],160)

    def test_checkout_requires_explicit_unpaid_confirmation_and_keeps_balance(self):
        admin,b=self.setup_booking();self.app.store.admin_action(b['id'],'check-in')
        path=f"/api/admin/bookings/{b['id']}/check-out"
        for data in [{}, [], {'expectedTotal':True,'expectedPaid':0}]:
            self.assertEqual(admin.request('POST',path,data)[0],400)
        review=dict(expectedTotal=160,expectedPaid=0)
        for allow in [None,False,'true',1]:
            self.assertEqual(admin.request('POST',path,{**review,'allowUnpaid':allow})[0],409)
        self.assertEqual(self.a.request('POST',path,{**review,'allowUnpaid':True})[0],401)
        self.assertEqual(admin.request('POST',path,{**review,'allowUnpaid':True},extra={'X-CSRF-Token':''})[0],403)
        self.assertTrue(next(s for s in self.a.state()['spaces'] if s['id']=='T01')['occupied'])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_events WHERE action='check-out'").fetchone()[0],0)
        self.assertEqual(admin.request('POST',path,{**review,'allowUnpaid':True})[0],200)
        self.assertEqual(admin.request('POST',path,{**review,'allowUnpaid':True})[0],200)
        self.assertEqual(self.app.store.payment_details(b['id'])['balance'],160)
        self.assertFalse(next(s for s in self.a.state()['spaces'] if s['id']=='T01')['occupied'])
        self.assertEqual(self.a.request('GET',f"/api/bookings/{b['id']}/receipt")[1]['payment']['status'],'Unpaid')
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_events WHERE action='check-out'").fetchone()[0],1)

    def test_checkout_rejects_stale_payments_and_allows_fully_paid(self):
        admin,b=self.setup_booking();self.app.store.admin_action(b['id'],'check-in')
        path=f"/api/admin/bookings/{b['id']}/check-out"
        old=dict(expectedTotal=160,expectedPaid=0,allowUnpaid=True)
        cash=admin.request('POST',self.path(b),self.payload(60))[1]['paymentId']
        self.assertEqual(admin.request('POST',path,old)[0],409)
        admin.request('POST',self.path(b),self.payload(100,'upi',60))
        paid=dict(expectedTotal=160,expectedPaid=160)
        self.app.store.void_payment(b['id'],cash,{'reason':'Wrong entry'})
        self.assertEqual(admin.request('POST',path,paid)[0],409)
        admin.request('POST',self.path(b),self.payload(60,'cash',100))
        self.assertEqual(admin.request('POST',path,paid)[0],200)
        self.assertEqual(self.app.store.payment_details(b['id'])['paid'],160)

    def test_checkout_rejects_stale_extension_total(self):
        admin,b=self.setup_booking();self.app.store.admin_action(b['id'],'check-in')
        path=f"/api/admin/bookings/{b['id']}/check-out"
        old=dict(expectedTotal=160,expectedPaid=0,allowUnpaid=True)
        quote=test_extensions.ExtensionTests.quote(self,b,duration=1)
        self.assertEqual(self.a.request('POST',f"/api/bookings/{b['id']}/extend",quote)[0],200)
        self.assertEqual(admin.request('POST',path,old)[0],409)
        self.assertEqual(admin.request('POST',path,{**old,'expectedTotal':240})[0],200)

    def test_auth_inputs_stale_state_and_duplicate_race(self):
        admin,b=self.setup_booking();path=self.path(b)
        self.assertEqual(self.a.request('GET',path)[0],401)
        self.assertEqual(self.a.request('POST',path,self.payload())[0],401)
        self.assertEqual(admin.request('POST',path,self.payload(),extra={'X-CSRF-Token':''})[0],403)
        self.assertEqual(admin.request('POST',path,self.payload(),extra={'Origin':'https://elsewhere.test'})[0],403)
        for amount in [0,-1,True,1.5,'80',None]:self.assertEqual(admin.request('POST',path,self.payload(amount))[0],400)
        self.assertEqual(admin.request('POST',path,self.payload(method='card'))[0],400)
        self.assertEqual(admin.request('POST',path,self.payload(paid=50))[0],409)
        other=self.login();barrier=threading.Barrier(2)
        def pay(client):barrier.wait();return client.request('POST',path,self.payload())[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,c=pool.submit(pay,admin),pool.submit(pay,other)
            self.assertEqual(sorted([a.result(),c.result()]),[200,409])
        self.assertEqual(self.app.store.payment_details(b['id'])['paid'],160)

    def test_correction_keeps_history_and_dates_do_not_move(self):
        admin,b=self.setup_booking();path=self.path(b)
        entry=admin.request('POST',path,self.payload())[1]['paymentId']
        day='2026-10-20';at=int(datetime(2026,10,20,tzinfo=server.REPORT_TIMEZONE).timestamp()*1000)
        with self.app.store.connect() as db:
            db.execute('UPDATE payments SET created_at=?',(at,))
            db.execute('UPDATE bookings SET start=?,end=?',(at-7200000,at-1))
        correction=path+f'/{entry}/void'
        self.assertEqual(admin.request('POST',correction,{'reason':''})[0],400)
        self.assertEqual(admin.request('POST',correction,{'reason':'Wrong booking'})[0],200)
        self.assertEqual(admin.request('POST',correction,{'reason':'Wrong booking'})[0],200)
        with self.app.store.connect() as db:db.execute('UPDATE payments SET voided_at=?',(at+86400000,))
        old=self.app.store.daily_report(day);new=self.app.store.daily_report('2026-10-21')
        self.assertEqual(old['summary']['bookedAmount'],0)
        self.assertEqual(old['collections'],{'cashAmount':160,'upiAmount':0,'totalAmount':160})
        self.assertEqual(new['collections']['totalAmount'],-160)
        self.assertEqual(new['payments'][0]['note'],'Wrong booking')
        details=admin.request('GET',path)[1]
        self.assertEqual((details['paid'],details['balance'],len(details['entries'])),(0,160,1))

    def test_collection_csv_and_excel_formulas_values_and_safe_notes(self):
        admin,b=self.setup_booking();path=self.path(b)
        entry=admin.request('POST',path,self.payload(60,'cash'))[1]['paymentId']
        self.assertEqual(admin.request('POST',path,self.payload(100,'upi',60))[0],200)
        self.assertEqual(admin.request('POST',path+f'/{entry}/void',{'reason':'=SUM(1,2)'})[0],200)
        report=self.app.store.daily_report(datetime.now(server.REPORT_TIMEZONE).strftime('%Y-%m-%d'))
        self.assertEqual(report['collections'],{'cashAmount':0,'upiAmount':100,'totalAmount':100})
        csv=self.app.store.report_csv(report).decode('utf-8-sig')
        self.assertIn('Net recorded collections,100',csv);self.assertIn("'=SUM(1,2)",csv)
        with ZipFile(io.BytesIO(report_xlsx(report))) as book:
            summary=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(summary.find(f".//{tag('c')}[@r='B27']/{tag('v')}").text,'100')
            self.assertEqual(summary.find(f".//{tag('c')}[@r='B27']/{tag('f')}").text,'SUM(B25:B26)')
            detail=ET.fromstring(book.read('xl/worksheets/sheet3.xml'))
            self.assertFalse(list(detail.iter(tag('f'))))
            self.assertIn('=SUM(1,2)',ET.tostring(detail,encoding='unicode'))
            self.assertEqual(ET.fromstring(book.read('xl/tables/table2.xml')).get('ref'),'A4:I7')

    def test_payment_backup_roundtrip_and_legacy_backup_upgrade(self):
        admin,b=self.setup_booking();admin.request('POST',self.path(b),self.payload())
        root=Path(self.temp.name);saved=backups.create_backup(self.app.store.path,root/'paid.sqlite3')
        backups.prepare_restore(saved,root/'roundtrip.sqlite3',server.Store)
        with closing(sqlite3.connect(root/'roundtrip.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT SUM(amount) FROM payments').fetchone()[0],160)
        with closing(sqlite3.connect(saved)) as db:
            db.execute('DROP TABLE waivers');db.execute('DROP TABLE payments');db.execute('UPDATE parking_backup_meta SET version=1');db.commit()
        legacy=backups.create_backup(saved,root/'legacy-backup.sqlite3')
        with closing(sqlite3.connect(legacy)) as db:self.assertEqual(db.execute('SELECT version FROM parking_backup_meta').fetchone()[0],1)
        backups.prepare_restore(legacy,root/'old.sqlite3',server.Store)
        with closing(sqlite3.connect(root/'old.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM payments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM bookings').fetchone()[0],1)


if __name__=='__main__':unittest.main()
