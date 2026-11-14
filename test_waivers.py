"""Waivers, balance reconciliation and legacy backups; disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import io
import json
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
from excel_report import outstanding_xlsx, tag


class WaiverTests(unittest.TestCase):
    setUp=test_server.BackendTests.setUp
    tearDown=test_server.BackendTests.tearDown
    login=test_admin.AdminTests.login

    def setup_booking(self):
        status,result=self.a.book();self.assertEqual(status,201)
        self.booking=result['booking'];self.identifier=self.booking['id']
        self.path=f'/api/admin/bookings/{self.identifier}'
        return self.login()

    def payload(self,reason='Cancelled without using the space',amount=None):
        d=self.app.store.payment_details(self.identifier)
        return dict(amount=d['balance'] if amount is None else amount,reason=reason,expectedTotal=d['total'],expectedPaid=d['paid'],expectedWaived=d['waived'],requestId=secrets.token_hex(16))

    def pay(self,amount):
        d=self.app.store.payment_details(self.identifier)
        return self.app.store.record_payment(self.identifier,dict(amount=amount,method='cash',expectedTotal=d['total'],expectedPaid=d['paid'],expectedWaived=d['waived'],requestId=secrets.token_hex(16)))

    def test_cancelled_balance_waiver_history_receipt_reversal_and_restart(self):
        admin=self.setup_booking();self.app.store.admin_action(self.identifier,'cancel')
        payload=self.payload('Private staff reason')
        status,result=admin.request('POST',self.path+'/waivers',payload);self.assertEqual(status,200)
        self.assertEqual(admin.request('POST',self.path+'/waivers',payload),(status,result))
        self.assertEqual(self.app.store.outstanding_payments()['count'],0)
        row=self.app.store.admin_state()['bookings'][0]
        self.assertEqual((row['status'],row['paid'],row['waived'],row['balance'],row['paymentStatus']),('cancelled',0,160,0,'Waived'))
        self.assertEqual(self.a.state()['history'][0]['balance'], 0)
        self.assertEqual(self.b.state()['history'], [])
        receipt=self.a.request('GET',f'/api/bookings/{self.identifier}/receipt')[1]
        self.assertEqual(receipt['payment'],dict(paid=0,waived=160,balance=0,status='Waived',cash=0,upi=0))
        self.assertNotIn('Private staff reason',json.dumps(receipt))
        self.assertEqual(self.b.request('GET',f'/api/bookings/{self.identifier}/receipt')[0],404)
        self.app.store=server.Store(self.app.store.path)
        reverse=self.path+f"/waivers/{result['waiverId']}/reverse"
        self.assertEqual(admin.request('POST',reverse,{'reason':'Wrong booking'})[0],200)
        self.assertEqual(admin.request('POST',reverse,{'reason':'Wrong booking'})[0],200)
        self.assertEqual(admin.request('POST',reverse,{'reason':'Different'})[0],409)
        # Replaying the original request never reactivates a reversed waiver.
        self.assertEqual(admin.request('POST',self.path+'/waivers',payload)[0],200)
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['balance'],d['waived'],len(d['waivers'])),(160,0,1))
        self.assertEqual(d['waivers'][0]['void_reason'],'Wrong booking')
        self.assertEqual(self.app.store.outstanding_payments()['balance'],160)
        self.assertEqual(self.a.state()['history'][0]['balance'], 160)

    def test_partial_payment_extension_excel_and_collections(self):
        admin=self.setup_booking();entry=self.pay(60)['paymentId']
        self.assertEqual(admin.request('POST',self.path+'/waivers',self.payload())[0],200)
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['paid'],d['waived'],d['balance'],d['status']),(60,100,0,'Settled'))
        day=datetime.now(server.REPORT_TIMEZONE).strftime('%Y-%m-%d')
        self.assertEqual(self.app.store.daily_report(day)['collections']['totalAmount'],60)
        quote=test_extensions.ExtensionTests.quote(self,self.booking,duration=1)
        self.assertEqual(self.a.request('POST',f'/api/bookings/{self.identifier}/extend',quote)[0],200)
        report=self.app.store.outstanding_payments(export=True)
        self.assertEqual((report['balance'],report['bookings'][0]['waived']),(80,100))
        with ZipFile(io.BytesIO(outstanding_xlsx(report))) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            cells={c.get('r'):c for c in sheet.iter(tag('c'))}
            self.assertEqual(cells['I13'].find(tag('v')).text,'100')
            self.assertEqual(cells['J13'].find(tag('v')).text,'80')
            self.assertEqual(cells['J13'].find(tag('f')).text,'G13-H13-I13')
        self.app.store.void_payment(self.identifier,entry,{'reason':'Incorrect cash entry'})
        self.assertEqual(self.app.store.payment_details(self.identifier)['balance'],140)
        self.assertEqual(self.app.store.daily_report(day)['collections']['totalAmount'],0)
        self.pay(140)
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['paid'],d['waived'],d['balance']),(140,100,0))

    def test_auth_validation_csrf_and_stale_changes(self):
        admin=self.setup_booking();payload=self.payload();path=self.path+'/waivers'
        self.assertEqual(self.a.request('POST',path,payload)[0],401)
        self.assertEqual(admin.request('POST',path,payload,extra={'X-CSRF-Token':'wrong'})[0],403)
        for reason in ['', ' ', 'x'*161, 'two\nlines', 10]:
            self.assertEqual(admin.request('POST',path,{**payload,'reason':reason})[0],400)
        self.assertEqual(admin.request('POST',path,{**payload,'expectedWaived':True})[0],400)
        self.pay(10)
        self.assertEqual(admin.request('POST',path,payload)[0],409)
        payload=self.payload();result=admin.request('POST',path,payload)
        self.assertEqual(result[0],200)
        self.assertEqual(admin.request('POST',path,{**payload,'reason':'Changed reason'})[0],409)
        self.assertEqual(admin.request('POST',path,self.payload(amount=1))[0],409)
        # An old payment client cannot record against a now-waived balance.
        self.assertEqual(admin.request('POST',self.path+'/payments',dict(amount=150,method='cash',expectedTotal=160,expectedPaid=10,requestId=secrets.token_hex(16)))[0],409)
        self.app.store.admin_action(self.identifier,'check-in')
        stale=dict(expectedTotal=160,expectedPaid=10,expectedWaived=0,allowUnpaid=True)
        self.assertEqual(admin.request('POST',self.path+'/check-out',stale)[0],409)
        self.assertEqual(admin.request('POST',self.path+'/check-out',{**stale,'expectedWaived':150,'allowUnpaid':False})[0],200)

    def test_concurrent_payment_and_waiver_cannot_over_settle(self):
        self.setup_booking();payload=self.payload();barrier=threading.Barrier(2)
        def run(waiver):
            barrier.wait()
            try:
                if waiver:self.app.store.record_waiver(self.identifier,payload)
                else:self.app.store.record_payment(self.identifier,dict(amount=160,method='upi',expectedTotal=160,expectedPaid=0,expectedWaived=0,requestId=secrets.token_hex(16)))
                return 200
            except server.ApiError as error:return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,[True,False]))
        self.assertEqual(sorted(results),[200,409])
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['paid']+d['waived'],d['balance']),(160,0))

    def test_backup_roundtrip_legacy_and_over_settled_rejection(self):
        self.setup_booking();self.pay(60)
        self.app.store.record_waiver(self.identifier,self.payload())
        root=Path(self.temp.name)
        saved=backups.create_backup(self.app.store.path,root/'new.sqlite3')
        backups.prepare_restore(saved,root/'restored.sqlite3',server.Store)
        restored=server.Store(root/'restored.sqlite3').payment_details(self.identifier)
        self.assertEqual((restored['paid'],restored['waived'],restored['balance']),(60,100,0))
        with closing(sqlite3.connect(saved)) as db:
            db.execute('UPDATE waivers SET amount=101');db.commit()
        with self.assertRaises(backups.BackupError):backups.prepare_restore(saved,root/'bad.sqlite3',server.Store)
        with closing(sqlite3.connect(saved)) as db:
            db.execute('DROP TABLE waivers');db.execute('UPDATE parking_backup_meta SET version=2');db.commit()
        old=backups.create_backup(saved,root/'version2.sqlite3')
        backups.prepare_restore(old,root/'old-restored.sqlite3',server.Store)
        d=server.Store(root/'old-restored.sqlite3').payment_details(self.identifier)
        self.assertEqual((d['paid'],d['waived'],d['balance']),(60,0,100))

    def test_partial_waivers_payments_and_individual_reversals(self):
        admin=self.setup_booking()
        quote=test_extensions.ExtensionTests.quote(self,self.booking,duration=3)
        self.assertEqual(self.a.request('POST',f'/api/bookings/{self.identifier}/extend',quote)[0],200)
        path=self.path+'/waivers';payload=self.payload(amount=160)
        status,result=admin.request('POST',path,payload);self.assertEqual(status,200)
        first=result['waiverId']
        self.assertEqual(admin.request('POST',path,payload),(status,result))
        self.assertEqual(admin.request('POST',path,{**payload,'amount':120})[0],409)
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['total'],d['paid'],d['waived'],d['balance'],d['status']),(400,0,160,240,'Unpaid'))
        self.assertEqual(self.a.state()['bookings'][0]['balance'], 240)
        receipt=self.a.request('GET',f'/api/bookings/{self.identifier}/receipt')[1]['payment']
        self.assertEqual((receipt['waived'],receipt['balance']),(160,240))
        report=self.app.store.outstanding_payments(export=True)
        self.assertEqual((report['count'],report['balance']),(1,240))
        with ZipFile(io.BytesIO(outstanding_xlsx(report))) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            cells={c.get('r'):c for c in sheet.iter(tag('c'))}
            self.assertEqual(cells['I13'].find(tag('v')).text,'160')
            self.assertEqual(cells['J13'].find(tag('v')).text,'240')
        self.pay(80)
        self.assertEqual(self.a.state()['bookings'][0]['balance'], 160)
        status,result=admin.request('POST',path,self.payload(amount=60));self.assertEqual(status,200)
        second=result['waiverId']
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['paid'],d['waived'],d['balance'],d['status']),(80,220,100,'Part paid'))
        self.app.store.reverse_waiver(self.identifier,first,{'reason':'Wrong amount'})
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['paid'],d['waived'],d['balance']),(80,60,260))
        self.app.store.reverse_waiver(self.identifier,second,{'reason':'No waiver required'})
        self.assertEqual(self.app.store.payment_details(self.identifier)['balance'],320)
        self.assertEqual(admin.request('POST',path,self.payload(amount=320))[0],200)
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['waived'],d['balance'],d['status']),(320,0,'Settled'))
        self.assertEqual(self.app.store.outstanding_payments()['count'],0)

    def test_partial_waiver_requires_explicit_valid_amount_and_current_balance(self):
        admin=self.setup_booking();path=self.path+'/waivers';payload=self.payload(amount=40)
        missing={k:v for k,v in payload.items() if k!='amount'}
        self.assertEqual(admin.request('POST',path,missing)[0],400)
        for amount in [None,True,False,0,-1,1.5,'40']:
            self.assertEqual(admin.request('POST',path,{**payload,'amount':amount})[0],400)
        self.assertEqual(admin.request('POST',path,{**payload,'amount':161})[0],409)
        self.assertEqual(admin.request('POST',path,payload)[0],200)
        # A second window's old balance must be reviewed even if its amount fits.
        self.assertEqual(admin.request('POST',path,{**payload,'amount':10,'requestId':secrets.token_hex(16)})[0],409)
        self.assertEqual(admin.request('POST',path,self.payload(amount=121))[0],409)
        self.assertEqual(self.app.store.payment_details(self.identifier)['balance'],120)

    def test_competing_partial_waivers_require_a_new_review(self):
        self.setup_booking();payload=self.payload(amount=100);barrier=threading.Barrier(2)
        def run(_):
            data={**payload,'requestId':secrets.token_hex(16)};barrier.wait()
            try:self.app.store.record_waiver(self.identifier,data);return 200
            except server.ApiError as error:return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertEqual(sorted(results),[200,409])
        d=self.app.store.payment_details(self.identifier)
        self.assertEqual((d['waived'],d['balance'],len(d['waivers'])),(100,60,1))


if __name__=='__main__':unittest.main()
