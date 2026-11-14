"""Fill the styled Excel template using only Python's standard library."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('x', NS)
ET.register_namespace('r', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships')
IST = timezone(timedelta(hours=5, minutes=30))
STATUSES = {
    'reserved': 'Awaiting arrival', 'parked': 'On site',
    'departed': 'Departed', 'overdue': 'Overdue · on site',
    'expired': 'Expired', 'released': 'Released', 'cancelled': 'Cancelled',
}


def tag(name):
    return f'{{{NS}}}{name}'


def serial(date):
    # Excel's 1900 date system includes a fictional leap day.
    days = (date - datetime(1899, 12, 31)).total_seconds() / 86400
    return days + (1 if date >= datetime(1900, 3, 1) else 0)


def local_serial(milliseconds):
    return serial(datetime.fromtimestamp(milliseconds / 1000, IST).replace(tzinfo=None))


def put(cell, value, formula=None):
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop('t', None)
    if formula is not None:
        ET.SubElement(cell, tag('f')).text = formula
    if value is None:
        return
    if isinstance(value, (int, float)):
        cell.set('t', 'n')
        ET.SubElement(cell, tag('v')).text = str(value)
    else:
        # Explicit strings prevent spreadsheet formula execution, including =/+/@.
        cell.set('t', 'inlineStr')
        text = ET.SubElement(ET.SubElement(cell, tag('is')), tag('t'))
        text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        text.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value))


def report_xlsx(report):
    """Return an independent workbook; the template and database are unchanged."""
    with ZipFile(Path(__file__).with_name('report-template.xlsx')) as template:
        parts = {name: template.read(name) for name in template.namelist()}
    summary = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    events = ET.fromstring(parts['xl/worksheets/sheet2.xml'])
    cells = {c.get('r'): c for c in summary.iter(tag('c'))}
    put(cells['B4'], serial(datetime.strptime(report['date'], '%Y-%m-%d')))
    put(cells['B5'], report['timezone'])
    put(cells['B6'], local_serial(report['generatedAt']))
    last = max(5, 4 + len(report['events']))
    for row, label, key in [(9, 'Booking', 'bookings'), (10, 'Extension', 'extensions'),
                            (11, 'Arrival', 'arrivals'), (12, 'Departure', 'departures')]:
        put(cells[f'B{row}'], report['summary'][key], f'COUNTIFS(\'Events\'!A5:A{last},"{label}")')
    for row, label, key in [(14, 'Booking', 'originalAmount'), (15, 'Extension', 'extensionAmount')]:
        put(cells[f'B{row}'], report['summary'][key],
            f'SUMIFS(\'Events\'!H5:H{last},\'Events\'!A5:A{last},"{label}")')
    put(cells['B16'], report['summary']['bookedAmount'], 'SUM(B14:B15)')
    data = events.find(tag('sheetData'))
    prototype = data.find(f"{tag('row')}[@r='5']")
    data.remove(prototype)
    for index, event in enumerate(report['events'] or [None], 5):
        row = deepcopy(prototype)
        row.set('r', str(index))
        values = ([event['event'], local_serial(event['time']), event['bookingId'], event['spaceId'],
                   event['type'].title(), event['driver'], event['plate'], event['amount'],
                   STATUSES.get(event['currentStatus'], event['currentStatus'].replace('_', ' ').replace('-', ' ').title())]
                  if event else [None] * 9)
        # Accommodate long names and explicit line breaks, including legacy data.
        lines = max((sum(max(1, (len(line) + width - 1) // width)
                         for line in str(value).split('\n'))
                     for value, width in zip(values, [13, 24, 27, 10, 13, 30, 22, 22, 23])), default=1)
        row.set('ht', str(min(409, max(48, 16 * lines + 8))))
        for column, (cell, value) in enumerate(zip(row, values)):
            cell.set('r', f'{chr(65 + column)}{index}')
            put(cell, value)
        data.append(row)
    if not report['events']:
        put(data.find(f"{tag('row')}[@r='2']/{tag('c')}[@r='A2']"),
            'No parking events on this date. All times are IST (UTC+05:30).')
    table = ET.fromstring(parts['xl/tables/table1.xml'])
    table.set('ref', f'A4:I{last}')
    auto_filter = table.find(tag('autoFilter'))
    if auto_filter is None:
        auto_filter = ET.Element(tag('autoFilter'))
        table.insert(0, auto_filter)
    auto_filter.set('ref', f'A4:I{last}')
    payment_sheet=ET.fromstring(parts['xl/worksheets/sheet3.xml'])
    payment_data=payment_sheet.find(tag('sheetData'))
    prototype=payment_data.find(f"{tag('row')}[@r='5']")
    payment_data.remove(prototype)
    payments=report.get('payments',[])
    payment_last=max(5,4+len(payments))
    for index,payment in enumerate(payments or [None],5):
        row=deepcopy(prototype);row.set('r',str(index))
        values=([f"{payment['event']} #{payment['id']}",local_serial(payment['time']),payment['bookingId'],payment['spaceId'],payment['method'].upper(),payment['driver'],payment['plate'],payment['amount'],payment['note']] if payment else [None]*9)
        lines=max((sum(max(1,(len(line)+29)//30) for line in str(values[i] or '').split('\n')) for i in [5,8]),default=1)
        row.set('ht',str(min(409,max(48,lines*16+8))))
        for column,(cell,value) in enumerate(zip(row,values)):
            cell.set('r',f'{chr(65+column)}{index}');put(cell,value)
        payment_data.append(row)
    if not payments:
        put(payment_data.find(f"{tag('row')}[@r='2']/{tag('c')}[@r='A2']"),'No payments or corrections recorded on this date.')
    payment_table=ET.fromstring(parts['xl/tables/table2.xml'])
    payment_table.set('ref',f'A4:I{payment_last}')
    payment_filter=payment_table.find(tag('autoFilter'))
    if payment_filter is None:
        payment_filter=ET.Element(tag('autoFilter'));payment_table.insert(0,payment_filter)
    payment_filter.set('ref',f'A4:I{payment_last}')
    for row,method,key in [(25,'CASH','cashAmount'),(26,'UPI','upiAmount')]:
        put(cells[f'B{row}'],report.get('collections',{}).get(key,0),f'SUMIFS(Payments!H5:H{payment_last},Payments!E5:E{payment_last},"{method}")')
    put(cells['B27'],report.get('collections',{}).get('totalAmount',0),'SUM(B25:B26)')
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    calc = workbook.find(tag('calcPr'))
    if calc is None:
        calc = ET.SubElement(workbook, tag('calcPr'))
    calc.attrib.update(calcMode='auto', fullCalcOnLoad='1')
    for name, tree in [('xl/worksheets/sheet1.xml', summary), ('xl/worksheets/sheet2.xml', events),
                       ('xl/worksheets/sheet3.xml',payment_sheet),('xl/tables/table2.xml',payment_table),
                       ('xl/tables/table1.xml', table), ('xl/workbook.xml', workbook)]:
        parts[name] = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as result:
        for name, content in parts.items():
            result.writestr(name, content)
    return output.getvalue()


def outstanding_xlsx(report):
    """Fill all matching balances into a separate, printable snapshot workbook."""
    with ZipFile(Path(__file__).with_name('outstanding-template.xlsx')) as template:
        parts = {name: template.read(name) for name in template.namelist()}
    sheet = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    cells = {cell.get('r'): cell for cell in sheet.iter(tag('c'))}
    filters = report['filters']
    put(cells['B4'], local_serial(report['serverTime']))
    put(cells['B5'], filters['search'] or 'All bookings')
    put(cells['B6'], {'all':'Unpaid and part paid','unpaid':'Unpaid','partial':'Part paid'}[filters['payment']])
    put(cells['B7'], 'Checked-out vehicles only' if filters['status']=='departed' else 'All bookings')
    bookings = report['bookings']
    last = max(13, 12+len(bookings))
    put(cells['E4'], len(bookings), f'COUNTA(B13:B{last})')
    put(cells['I4'], sum(b['balance'] for b in bookings), f'SUM(J13:J{last})')
    data = sheet.find(tag('sheetData'))
    data.find(f"{tag('row')}[@r='5']").set('ht', str(max(24, ((len(filters['search'])+24)//25)*16+8)))
    prototype = data.find(f"{tag('row')}[@r='13']")
    data.remove(prototype)
    for index, booking in enumerate(bookings or [None], 13):
        row = deepcopy(prototype); row.set('r', str(index))
        values = ([local_serial(booking['start']),booking['id'],booking['plate'],booking['driver'],booking['spaceId'],
                   STATUSES[booking['status']],booking['total'],booking['paid'],booking['waived'],booking['balance'],booking['paymentStatus']]
                  if booking else [None]*11)
        lines = max((sum(max(1,(len(line)+width-1)//width) for line in str(value or '').split('\n'))
                     for value,width in zip(values,[20,27,20,24,8,18,15,15,15,15,14])),default=1)
        row.set('ht', str(min(409,max(40,16*lines+8))))
        for column,(cell,value) in enumerate(zip(row,values)):
            cell.set('r',f'{chr(65+column)}{index}')
            formula = None
            if booking and column==9: formula=f'G{index}-H{index}-I{index}'
            elif booking and column==10: formula=f'IF(J{index}=0,IF(I{index}>0,IF(H{index}>0,"Settled","Waived"),"Paid"),IF(H{index}=0,"Unpaid","Part paid"))'
            put(cell,value,formula)
        data.append(row)
    if not bookings:
        put(cells['A10'],'No outstanding bookings match these filters.')
    table = ET.fromstring(parts['xl/tables/table1.xml'])
    table.set('ref',f'A12:K{last}')
    auto_filter = table.find(tag('autoFilter'))
    if auto_filter is None:
        auto_filter=ET.Element(tag('autoFilter'));table.insert(0,auto_filter)
    auto_filter.set('ref',f'A12:K{last}')
    dimension=sheet.find(tag('dimension'))
    if dimension is not None:dimension.set('ref',f'A1:K{last}')
    # Print the complete snapshot, with headers repeated and one page wide.
    margins=sheet.find(tag('pageMargins'))
    if margins is None:margins=ET.SubElement(sheet,tag('pageMargins'))
    margins.attrib.update(left='0.25',right='0.25',top='0.4',bottom='0.4',header='0.2',footer='0.2')
    setup=sheet.find(tag('pageSetup'))
    if setup is None:setup=ET.SubElement(sheet,tag('pageSetup'))
    setup.attrib.update(paperSize='8',orientation='landscape',fitToWidth='1',fitToHeight='0')
    # Keep page settings before tableParts in SpreadsheetML element order.
    tables=sheet.find(tag('tableParts'))
    if tables is not None:sheet.remove(tables);sheet.append(tables)
    props=sheet.find(tag('sheetPr'))
    if props is None:props=ET.Element(tag('sheetPr'));sheet.insert(0,props)
    page_props=props.find(tag('pageSetUpPr'))
    if page_props is None:page_props=ET.SubElement(props,tag('pageSetUpPr'))
    page_props.set('fitToPage','1')
    workbook=ET.fromstring(parts['xl/workbook.xml'])
    names=workbook.find(tag('definedNames'))
    if names is None:names=ET.SubElement(workbook,tag('definedNames'))
    for name,value in [('_xlnm.Print_Area',f"'Outstanding'!$A$1:$K${last}"),('_xlnm.Print_Titles',"'Outstanding'!$12:$12")]:
        for old in list(names):
            if old.get('name')==name:names.remove(old)
        ET.SubElement(names,tag('definedName'),name=name,localSheetId='0').text=value
    calc=workbook.find(tag('calcPr'))
    if calc is None:calc=ET.SubElement(workbook,tag('calcPr'))
    else:workbook.remove(calc);workbook.append(calc)
    calc.attrib.update(calcMode='auto',fullCalcOnLoad='1')
    for name,tree in [('xl/worksheets/sheet1.xml',sheet),('xl/tables/table1.xml',table),('xl/workbook.xml',workbook)]:
        parts[name]=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as result:
        for name,content in parts.items():result.writestr(name,content)
    return output.getvalue()
