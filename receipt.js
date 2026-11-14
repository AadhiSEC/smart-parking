'use strict';
const $ = (id) => document.getElementById(id);
const bookingId = new URLSearchParams(location.search).get('booking');
const money = (value) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value);
const date = (value) => new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const statuses = { reserved: 'Awaiting arrival', parked: 'Checked in', overdue: 'Overdue · on site', departed: 'Checked out', cancelled: 'Cancelled by admin', released: 'Released by you', expired: 'Expired · no check-in' };
let loading = null;
function loadReceipt() {
  if (loading) return loading;
  loading = (async () => {
    $('print-receipt').disabled = true; $('refresh-receipt').disabled = true; $('retry-receipt').hidden = true;
    $('receipt').hidden = true; $('receipt-message').hidden = false;
    $('receipt-message').className = 'notice'; $('receipt-message').textContent = 'Loading your receipt…';
    document.title = 'Booking receipt — Parkway';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      if (!bookingId || !/^PK-[A-F0-9]{16}$/.test(bookingId)) throw new Error('Choose a booking from My bookings to view its receipt.');
      const response = await fetch(`/api/bookings/${encodeURIComponent(bookingId)}/receipt`, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) throw new Error('Please sign in from My bookings, then open your receipt again.');
      if (!response.ok) throw new Error(data.error || 'Could not load the receipt. Try again shortly.');
      const booking = data.booking;
      if (!booking || booking.id !== bookingId) throw new Error('The server returned an incomplete receipt. Restart python server.py and try again.');
      const payment = data.payment;
      if (!payment || !['Paid', 'Part paid', 'Unpaid', 'Waived', 'Settled'].includes(payment.status) ||
          !['paid', 'waived', 'balance', 'cash', 'upi'].every(key => Number.isSafeInteger(payment[key]) && payment[key] >= 0) ||
          payment.paid + payment.waived + payment.balance !== booking.total || payment.cash + payment.upi !== payment.paid) {
        throw new Error('Payment details are unavailable. Restart python server.py, then refresh this receipt.');
      }
      const fields = { id: booking.id, driver: booking.driver, plate: booking.plate, vehicle: booking.type === 'truck' ? 'Truck' : 'Bus', space: booking.spaceId,
        start: date(booking.start), end: date(booking.end), status: statuses[booking.status] || booking.status,
        total: money(booking.total),
        timezone: `Times shown in ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`, generated: `Receipt loaded ${date(data.generatedAt)}.` };
      for (const [key, value] of Object.entries(fields)) $(`receipt-${key}`).textContent = value;
      $('receipt-payment-status').textContent = payment.status;
      $('receipt-paid').textContent = money(payment.paid);
      $('receipt-waived').textContent = money(payment.waived);
      $('receipt-balance').textContent = money(payment.balance);
      $('receipt-methods').textContent = payment.paid
        ? [['Cash', payment.cash], ['UPI', payment.upi]].filter(([, amount]) => amount > 0).map(([method, amount]) => `${method}: ${money(amount)}`).join(' · ')
        : 'No payment recorded yet.';
      $('receipt-lines').replaceChildren();
      const charges=booking.charges || [{label:'Original booking',unit:booking.unit,duration:booking.duration,rate:booking.rate,amount:booking.total}];
      for(const charge of charges) {
        const row=document.createElement('div'); row.className='charge-line';
        const info=document.createElement('div'),label=document.createElement('strong'),terms=document.createElement('small'),amount=document.createElement('strong');
        label.textContent=charge.label;
        terms.textContent=`${charge.duration} ${charge.unit}${charge.duration===1?'':'s'} × ${money(charge.rate)}${charge.unit==='day'?' · 24 hours per day':''}`;
        amount.textContent=money(charge.amount); info.append(label,terms);
        if(charge.end){const end=document.createElement('small');end.textContent=`Extended until ${date(charge.end)}`;info.append(end);}
        row.append(info,amount); $('receipt-lines').append(row);
      }
      for (const [name, value] of [['arrival', booking.checkedInAt], ['departure', booking.checkedOutAt]]) {
        $(`${name}-row`).hidden = !value; $(`receipt-${name}`).textContent = value ? date(value) : '';
      }
      document.title = `Receipt ${booking.id} — Parkway`;
      $('receipt').hidden = false; $('receipt-message').hidden = true; $('print-receipt').disabled = false;
      return true;
    } catch (error) {
      $('receipt-message').className = 'notice error';
      $('receipt-message').textContent = error.name === 'AbortError' || error instanceof TypeError ? 'Cannot reach the parking server. Check the VS Code terminal, then try again.' : error.message;
      $('retry-receipt').hidden = false;
      return false;
    } finally { clearTimeout(timeout); $('refresh-receipt').disabled = false; }
  })().finally(() => { loading = null; });
  return loading;
}
$('print-receipt').addEventListener('click', async () => { if (await loadReceipt()) window.print(); });
$('retry-receipt').addEventListener('click', loadReceipt);
$('refresh-receipt').addEventListener('click', loadReceipt);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadReceipt(); });
window.addEventListener('pageshow', (event) => { if (event.persisted) loadReceipt(); });
loadReceipt();
