'use strict';
const FILTER_KEY = 'parkway-demo-filter-v1';
const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(n);
let rates = null, spaces = [], bookings = [], csrfToken = '';
let connected = false, busy = false, refreshing = null, pendingRequest = null;
let selectedId = null, unit = 'hour', filter = 'all';
let customer = null, history = [], historyTotal = 0, bookingView = 'active', signup = false;
let renderedSpaces = '', renderedBookings = '';
let lastServerTime = 0;
let bookingSearch = '', loadedSearch = '', searchTimer = null, bookingsTotal = 0, historyMatches = 0;
let extension = null;
function clearBookingSearch() {
  clearTimeout(searchTimer); bookingSearch = ''; $('booking-search').value = '';
}
const registrationCodes = new Set('AN AP AR AS BR CG CH DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ NL OD OR PB PY RJ SK TN TR TS TG UA UK UP WB'.split(' '));
function normalizedPlate(value) {
  if (value.length > 20 || !/^[A-Za-z0-9 -]+$/.test(value)) return null;
  const compact = value.replace(/[ -]/g, '').toUpperCase();
  const match = compact.match(/^([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{1,4})$/);
  if (!match) return null;
  const [, state, district, series, number] = match;
  if (!registrationCodes.has(state) || !Number(district) || !Number(number)) return null;
  return [state, district.padStart(2, '0'), series, number.padStart(4, '0')].filter(Boolean).join(' ');
}
function validatePlate(format = false) {
  const input = $('plate'), normalized = normalizedPlate(input.value);
  const bh = /^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$/.test(input.value.replace(/[ -]/g, '').toUpperCase());
  const error = !input.value ? '' : normalized ? '' : bh ? 'BH-series is for non-transport vehicles. Enter your truck or bus registration.' : 'Include the letter series shown on your vehicle, e.g. AB in WB 55 AB 5577. Check all letters and digits.';
  input.setCustomValidity(error); input.setAttribute('aria-invalid', String(Boolean(error)));
  $('plate-error').textContent = error;
  if (format && normalized) input.value = normalized;
  return normalized;
}
$('plate').addEventListener('input', () => {
  const start = $('plate').selectionStart, end = $('plate').selectionEnd;
  $('plate').value = $('plate').value.replace(/[a-z]/g, (letter) => letter.toUpperCase());
  $('plate').setSelectionRange(start, end);
  $('plate').setCustomValidity(''); $('plate').setAttribute('aria-invalid', 'false'); $('plate-error').textContent = '';
});
$('plate').addEventListener('blur', () => validatePlate(true));
try {
  const saved = localStorage.getItem(FILTER_KEY);
  if (['truck', 'bus', 'all'].includes(saved)) filter = saved;
} catch { /* The preference is optional. Bookings live on the server. */ }

function message(text, kind = '') {
  $('form-message').textContent = text;
  $('form-message').className = `form-message ${kind}`;
}
async function api(path, data) {
  if (!['http:', 'https:'].includes(location.protocol)) throw new Error('Run python server.py, then open http://localhost:8000.');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      headers: data === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: data === undefined ? undefined : JSON.stringify(data)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || 'Backend unavailable. Run python server.py instead of the old static server.');
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) throw new Error('Cannot reach the server. Check the VS Code terminal, then try again.');
    throw error;
  } finally { clearTimeout(timeout); }
}
function refreshState() {
  if (refreshing) return refreshing;
  const requestedSearch = bookingSearch;
  refreshing = (async () => {
    try {
      const data = await api(`/api/state?search=${encodeURIComponent(requestedSearch)}`);
      if (requestedSearch !== bookingSearch) return false;
      if (!Array.isArray(data.spaces) || !Array.isArray(data.bookings) || !data.rates || !data.csrfToken) throw new Error('Backend unavailable. Run python server.py and refresh.');
      const identityChanged = customer?.id !== data.customer?.id;
      spaces = data.spaces; bookings = data.bookings; rates = data.rates; csrfToken = data.csrfToken;
      lastServerTime = data.serverTime;
      customer = data.customer || null; history = data.history || []; historyTotal = data.historyTotal || 0;
      bookingsTotal = data.bookingsTotal ?? bookings.length; historyMatches = data.historyMatches ?? historyTotal; loadedSearch = requestedSearch;
      if (identityChanged) { clearBookingSearch(); pendingRequest = null; extension = null; $('extension-dialog').close(); $('driver').value = ''; $('plate').value = ''; validatePlate(); }
      connected = true;
      if (selectedId && !spaces.some((s) => s.id === selectedId && !s.occupied && !s.blocked)) {
        selectedId = null;
        message('That space is no longer available. Please choose another space.', 'error');
      }
      $('connection-status').textContent = 'Connected · updates every 5 seconds';
      for (const kind of ['truck', 'bus']) for (const billing of ['hour', 'day']) $(`${kind}-${billing}-rate`).textContent = money(rates[kind][billing]);
      renderAll();
      return true;
    } catch (error) {
      if (requestedSearch !== bookingSearch) return false;
      connected = false;
      $('connection-status').textContent = error.message;
      renderAll();
      return false;
    }
  })().finally(() => {
    refreshing = null;
    if (requestedSearch !== bookingSearch && !busy) refreshState();
  });
  return refreshing;
}
function durationValue() {
  const n = Number($('duration').value);
  return Number.isInteger(n) && n >= 1 && n <= (unit === 'hour' ? 24 : 30) ? n : null;
}
function renderSpaces() {
  const key = JSON.stringify([spaces, filter, selectedId, busy, connected]);
  if (key === renderedSpaces) return;
  renderedSpaces = key;
  $('space-grid').replaceChildren();
  spaces.filter((s) => filter === 'all' || s.type === filter).forEach((space) => {
    const taken = Boolean(space.occupied), blocked = Boolean(space.blocked), selected = selectedId === space.id;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `space ${space.type}${taken ? ' occupied' : ''}${blocked ? ' blocked' : ''}${selected ? ' selected' : ''}`;
    button.disabled = taken || blocked || busy || !connected;
    button.setAttribute('aria-pressed', String(selected));
    button.setAttribute('aria-label', `${space.id}, ${space.type}, ${blocked ? `blocked: ${space.blockReason}` : taken ? 'occupied' : selected ? 'selected' : 'available'}`);
    const name = document.createElement('strong'); name.textContent = space.id;
    const icon = document.createElement('span'); icon.className = 'vehicle-art'; icon.setAttribute('aria-hidden', 'true');
    const status = document.createElement('small'); status.textContent = blocked ? 'Blocked' : taken ? 'Occupied' : selected ? 'Selected ✓' : 'Available';
    button.append(name, icon, status);
    if(blocked) {
      const reason = document.createElement('small'); reason.className='block-reason'; reason.textContent=space.blockReason;
      button.append(reason); button.title=space.blockReason;
    }
    button.addEventListener('click', () => {
      selectedId = space.id; setFilter(space.type);
      message(`Space ${space.id} selected. Add vehicle details to create a test booking.`);
    });
    $('space-grid').append(button);
  });
  const trucks = spaces.filter((s) => s.type === 'truck' && !s.occupied && !s.blocked).length;
  const buses = spaces.filter((s) => s.type === 'bus' && !s.occupied && !s.blocked).length;
  $('truck-available').textContent = connected ? trucks : '—';
  $('bus-available').textContent = connected ? buses : '—';
  $('total-available').textContent = connected ? (filter === 'truck' ? trucks : filter === 'bus' ? buses : trucks + buses) : '—';
  $('availability-label').textContent = connected ? (filter === 'all' ? 'spaces available' : `${filter} spaces available`) : 'availability unavailable';
  $('selected-space').textContent = selectedId || 'Choose on the map';
}
function updateQuote() {
  const duration = durationValue(), rate = rates?.[$('vehicle').value]?.[unit];
  const total = duration === null || rate === undefined ? null : duration * rate;
  $('price-description').textContent = rate === undefined ? 'Waiting for server rates' : duration === null ? 'Enter a valid duration' : `${duration} ${unit}${duration === 1 ? '' : 's'} × ${money(rate)}`;
  $('subtotal').textContent = $('total-price').textContent = total === null ? '—' : money(total);
  $('reserve').disabled = !connected || busy || (customer && (!selectedId || total === null));
  $('reserve').textContent = busy ? 'Saving…' : customer ? 'Create test booking ↗' : 'Sign in to book ↗';
  $('decrease').disabled = duration === 1 || busy;
  $('increase').disabled = duration === Number($('duration').max) || busy;
  for (const id of ['vehicle', 'duration', 'driver', 'plate']) $(id).disabled = busy;
  document.querySelectorAll('.billing, [data-filter]').forEach((b) => { b.disabled = busy; });
  $('reset-demo').disabled = busy || !connected || !bookings.some(booking => !booking.checkedInAt);
}
function setFilter(value, persist = true) {
  filter = value;
  if (persist) { try { localStorage.setItem(FILTER_KEY, filter); } catch { /* Optional preference. */ } }
  document.querySelectorAll('[data-filter]').forEach((b) => {
    b.classList.toggle('active', b.dataset.filter === filter);
    b.setAttribute('aria-pressed', String(b.dataset.filter === filter));
  });
  const selected = spaces.find((s) => s.id === selectedId);
  if (selected && filter !== 'all' && selected.type !== filter) selectedId = null;
  if (filter !== 'all') $('vehicle').value = filter;
  renderSpaces(); updateQuote();
}
async function releaseBooking(id) {
  if (busy) return;
  busy = true; renderAll();
  try {
    if (refreshing) await refreshing;
    await api(id ? `/api/bookings/${encodeURIComponent(id)}/release` : '/api/bookings/release-all', {});
    await refreshState();
    message(id ? 'Your space has been released.' : 'Unarrived bookings released. Checked-in vehicles stay reserved until admin check-out.', 'success');
  } catch (error) { await refreshState(); message(error.message, 'error'); }
  finally { busy = false; renderAll(); }
}
function updateBookingCountdowns() {
  for (const label of $('booking-list').querySelectorAll('[data-booking-end]')) {
    const remaining = Number(label.dataset.bookingEnd) - lastServerTime;
    let text;
    if (remaining <= 0) {
      text = label.dataset.checkedIn === 'true' ? 'Overdue' : 'Booking ended';
    } else if (remaining < 60000) {
      text = 'Less than a minute left';
    } else {
      const minutes = Math.ceil(remaining / 60000);
      const parts = [];
      for (const [count, unit] of [[Math.floor(minutes / 1440), 'day'], [Math.floor(minutes / 60) % 24, 'hour'], [minutes % 60, 'minute']]) {
        if (count > 0) parts.push(`${count} ${unit}${count === 1 ? '' : 's'}`);
      }
      text = parts.join(' ') + ' left';
    }
    if (label.textContent !== text) label.textContent = text;
  }
}
function renderBookings() {
  $('booking-count').textContent = bookingsTotal;
  $('show-active').textContent = `Active (${bookingsTotal})`;
  $('show-history').textContent = `History (${historyTotal})`;
  for (const view of ['active', 'history']) {
    $(`show-${view}`).classList.toggle('active', bookingView === view);
    $(`show-${view}`).setAttribute('aria-pressed', String(bookingView === view));
  }
  $('reset-demo').hidden = bookingView === 'history' || Boolean(bookingSearch);
  $('booking-search').disabled = busy;
  $('clear-booking-search').disabled = busy || !bookingSearch;
  const waiting = loadedSearch !== bookingSearch;
  const rows = bookingView === 'active' ? bookings : history;
  const matches = bookingView === 'active' ? bookings.length : historyMatches;
  $('search-summary').textContent = !connected ? 'Reconnect to load current results.' : waiting ? 'Searching your bookings…' : bookingSearch ? `${matches} ${bookingView === 'active' ? 'active' : 'past'} booking${matches === 1 ? '' : 's'} found${matches > rows.length ? ` · showing the latest ${rows.length}` : ''}.` : '';
  const key = JSON.stringify([rows, bookingView, historyTotal, historyMatches, customer?.id, connected, busy, bookingSearch, waiting]);
  if (key === renderedBookings) {
    updateBookingCountdowns();
    return;
  }
  renderedBookings = key;
  $('booking-list').replaceChildren();
  if (waiting) return;
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'empty-bookings';
    empty.textContent = !connected ? 'Connect to the server to load your bookings.' : bookingSearch ? `No matching ${bookingView === 'active' ? 'active' : 'past'} bookings. Try another search, clear it, or switch tabs.` : bookingView === 'history' ? 'No past bookings yet.' : customer ? 'No active bookings. Select a space to get started.' : 'Sign in to see your bookings, or create an account for your first stay.';
    $('booking-list').append(empty);
  }
  if (bookingView === 'history' && !bookingSearch && historyTotal > history.length) {
    const note = document.createElement('p'); note.className = 'storage-note';
    note.textContent = `Showing the latest ${history.length} of ${historyTotal} past bookings.`;
    $('booking-list').append(note);
  }
  rows.forEach((booking) => {
    const item = document.createElement('article'); item.className = 'booking-item';
    const info = document.createElement('div');
    const title = document.createElement('h3'); title.textContent = `Space ${booking.spaceId} · ${booking.plate}`;
    const driver = document.createElement('p'); driver.textContent = `${booking.driver} · Booking ${booking.id}`;
    const time = document.createElement('p');
    const fmt = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
    time.textContent = `${new Date(booking.start).toLocaleString(undefined, fmt)} → ${new Date(booking.end).toLocaleString(undefined, fmt)} (your local time)`;
    const total = document.createElement('p');
    total.textContent = `Booking total ${money(booking.total)}`;
    const balance = document.createElement('p');
    balance.className = 'booking-cost';
    balance.textContent = Number.isSafeInteger(booking.balance) && booking.balance >= 0
      ? `${money(booking.balance)} still to pay`
      : 'Balance unavailable';
    info.append(title, driver, time);
    if (bookingView === 'active' && connected && Number.isFinite(lastServerTime)) {
      const countdown = document.createElement('strong');
      countdown.dataset.bookingEnd = booking.end;
      countdown.dataset.checkedIn = Boolean(booking.checkedInAt);
      time.append(' · ', countdown);
    }
    info.append(total, balance);
    if (booking.status) {
      const status = document.createElement('p'); status.className = 'booking-status';
      status.textContent = ({ released: 'Released by you', cancelled: 'Cancelled by admin', departed: 'Checked out', expired: 'Expired · no check-in' })[booking.status] || booking.status;
      info.append(status);
    } else if (booking.checkedInAt) {
      const status = document.createElement('p');
      status.textContent = 'Checked in · ask the admin to record departure when you leave.';
      info.append(status);
    }
    const release = document.createElement('button'); release.type = 'button'; release.className = 'text-button';
    release.disabled = busy || !connected || Boolean(booking.checkedInAt);
    release.textContent = booking.checkedInAt ? 'Admin check-out required' : 'Release space';
    release.setAttribute('aria-label', `Release space ${booking.spaceId}`);
    release.addEventListener('click', () => releaseBooking(booking.id));
    item.append(info);
    const actions = document.createElement('div'); actions.className = 'booking-actions';
    const receipt = document.createElement('a'); receipt.className = 'text-button receipt-link';
    receipt.href = `/receipt?booking=${encodeURIComponent(booking.id)}`;
    receipt.textContent = 'View receipt ↗';
    receipt.setAttribute('aria-label', `View receipt for ${booking.spaceId}, booking ${booking.id}`);
    actions.append(receipt);
    if (bookingView === 'active') {
      const extend = document.createElement('button'); extend.type = 'button'; extend.className = 'text-button';
      extend.textContent = 'Extend time'; extend.disabled = busy || !connected || !customer;
      extend.setAttribute('aria-label', `Extend time for ${booking.spaceId}, booking ${booking.id}`);
      extend.addEventListener('click', () => openExtension(booking));
      actions.append(extend, release);
    }
    item.append(actions);
    $('booking-list').append(item);
  });
  updateBookingCountdowns();
}
function renderAccount() {
  $('account-name').textContent = customer ? `Hi, ${customer.name}` : '';
  $('open-account').hidden = Boolean(customer); $('sign-out').hidden = !customer;
  $('open-account').disabled = $('sign-out').disabled = busy || !connected;
  $('account-note').textContent = customer ? `Signed in as ${customer.email}. Your bookings follow this account across browsers.` : 'Sign in to book and access your bookings from another browser.';
  for (const id of ['customer-name', 'customer-email', 'customer-password', 'customer-confirm', 'submit-account', 'switch-account', 'close-account']) $(id).disabled = busy;
}
function renderAll() { renderSpaces(); updateQuote(); renderBookings(); renderAccount(); }
function extensionMessage(text, error=false) {
  $('extension-message').textContent=text; $('extension-message').className=`form-message ${error?'error':'success'}`;
}
function extensionControls() {
  for(const id of ['extension-unit','extension-duration','preview-extension','close-extension']) $(id).disabled=busy;
  $('confirm-extension').disabled=busy||!extension?.quote;
}
function openExtension(booking) {
  if(busy||!connected)return;
  extension={id:booking.id,quote:null,pending:null};
  $('extension-booking').textContent=`Space ${booking.spaceId} · ${booking.plate}`;
  $('extension-form').reset(); $('extension-duration').max=24; $('extension-limit').textContent='Add 1–24 whole hours.';
  $('extension-quote').hidden=true; extensionMessage('Choose the extra time, then preview its cost.'); extensionControls();
  $('extension-dialog').showModal();
}
function clearExtensionQuote() {
  if(extension) {extension.quote=null; extension.pending=null;}
  $('extension-quote').hidden=true;
  const hours=$('extension-unit').value==='hour';
  $('extension-duration').max=hours?24:30; $('extension-limit').textContent=hours?'Add 1–24 whole hours.':'Add 1–30 whole days.';
  extensionMessage('Preview the cost before confirming.'); extensionControls();
}
$('extension-form').addEventListener('input',clearExtensionQuote);
$('extension-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(busy||!extension)return;
  const target=extension; busy=true; extensionControls(); renderAll();
  target.quote=null; target.pending=null; $('extension-quote').hidden=true; extensionMessage('Checking the latest price…');
  try {
    if(refreshing)await refreshing;
    if(extension!==target)return;
    const data=await api(`/api/bookings/${target.id}/extension-quote`,{unit:$('extension-unit').value,duration:Number($('extension-duration').value)});
    if(extension!==target)return;
    const q=data.quote; target.quote=q;
    const date=value=>new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'});
    $('extension-old-end').textContent=date(q.previousEnd); $('extension-new-end').textContent=date(q.newEnd);
    $('extension-terms').textContent=`${q.duration} ${q.unit}${q.duration===1?'':'s'} × ${money(q.rate)}`;
    $('extension-cost').textContent=money(q.addedAmount); $('extension-total').textContent=money(q.newTotal);
    $('extension-quote').hidden=false; extensionMessage('Review the extra cost and new end time, then confirm.');
  } catch(error) {extensionMessage(error.message,true);}
  finally {busy=false; extensionControls(); renderAll();}
});
$('confirm-extension').addEventListener('click',async()=>{
  if(busy||!extension?.quote)return;
  const target=extension,q=target.quote;
  if(!target.pending)target.pending={unit:q.unit,duration:q.duration,expectedEnd:q.previousEnd,expectedTotal:q.previousTotal,expectedRate:q.rate,requestId:crypto.randomUUID()};
  busy=true; extensionControls(); renderAll(); extensionMessage('Saving your extension…');
  try {
    if(refreshing)await refreshing;
    if(extension!==target)return;
    const result=await api(`/api/bookings/${target.id}/extend`,target.pending);
    target.quote=null; target.pending=null; extension=null; $('extension-dialog').close();
    await refreshState();
    message(`Parking time extended. Additional cost: ${money(result.addedAmount)}. View the updated receipt under My bookings.`,'success');
    const confirmation=document.createElement('p'); confirmation.className='form-message success'; confirmation.setAttribute('role','status');
    confirmation.textContent=`Parking time extended. Additional cost: ${money(result.addedAmount)}. Your end time and receipt have been updated.`;
    $('bookings').querySelector('.extension-success')?.remove(); confirmation.classList.add('extension-success'); $('bookings').insertBefore(confirmation,$('booking-list'));
  } catch(error) {
    if(error.status && error.status<500){target.quote=null;target.pending=null;$('extension-quote').hidden=true;}
    extensionMessage(error.message+(error.status?'':' You can confirm again safely to retry.'),true);
    await refreshState();
  } finally {busy=false; extensionControls(); renderAll();}
});
$('close-extension').addEventListener('click',()=>{if(!busy)$('extension-dialog').close();});
$('extension-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();});
$('extension-dialog').addEventListener('close',()=>{extension=null;});
function accountMessage(text, error = false) {
  $('account-message').textContent = text;
  $('account-message').className = `form-message ${error ? 'error' : 'success'}`;
}
function setAccountMode(create) {
  signup = create;
  $('account-title').textContent = signup ? 'Make room for your next trip.' : 'Welcome back.';
  $('account-description').textContent = signup ? 'Create an account to keep your parking bookings in one place.' : 'Sign in to see your bookings and plan your next stay.';
  $('signup-name').hidden = $('signup-confirm').hidden = !signup;
  $('customer-name').required = $('customer-confirm').required = signup;
  $('customer-password').minLength = signup ? 10 : 1;
  $('customer-password').autocomplete = signup ? 'new-password' : 'current-password';
  $('password-hint').textContent = signup ? 'Use 10–128 characters for your password.' : 'Forgot your password? Ask the local site owner to reset it.';
  $('submit-account').textContent = signup ? 'Create account ↗' : 'Sign in ↗';
  $('switch-account').textContent = signup ? 'Already registered? Sign in' : 'New here? Create an account';
  $('customer-confirm').value = ''; accountMessage('');
}
function openAccount() { if (!busy && connected) { setAccountMode(false); $('account-dialog').showModal(); } }
$('open-account').addEventListener('click', openAccount);
$('close-account').addEventListener('click', () => { $('account-dialog').close(); });
$('account-dialog').addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
$('account-dialog').addEventListener('close', () => { $('customer-password').value = ''; $('customer-confirm').value = ''; });
$('switch-account').addEventListener('click', () => setAccountMode(!signup));
$('account-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !connected) return;
  if (signup && $('customer-password').value !== $('customer-confirm').value) { accountMessage('Passwords do not match.', true); return; }
  const data = { email: $('customer-email').value.trim(), password: $('customer-password').value, name: $('customer-name').value.trim() };
  busy = true; renderAll(); accountMessage(signup ? 'Creating your account…' : 'Signing in…');
  try {
    if (refreshing) await refreshing;
    const result = await api(`/api/customer/${signup ? 'signup' : 'login'}`, data);
    csrfToken = result.csrfToken; customer = result.customer; bookings = []; history = []; historyTotal = 0; bookingsTotal = 0; historyMatches = 0; clearBookingSearch();
    pendingRequest = null; bookingView = 'active'; $('driver').value = customer.name; $('plate').value = ''; validatePlate();
    const refreshed = await refreshState();
    $('account-dialog').close();
    message(refreshed ? 'Signed in. Choose a space to book your stay.' : 'Signed in. Reconnect to load your bookings.', 'success');
  } catch (error) { await refreshState(); accountMessage(error.message, true); }
  finally { busy = false; renderAll(); }
});
$('sign-out').addEventListener('click', async () => {
  if (busy || !connected) return;
  busy = true; renderAll();
  try {
    if (refreshing) await refreshing;
    const result = await api('/api/customer/logout', {});
    csrfToken = result.csrfToken; customer = null; bookings = []; history = []; historyTotal = 0; bookingsTotal = 0; historyMatches = 0; clearBookingSearch();
    pendingRequest = null; selectedId = null; bookingView = 'active'; $('driver').value = ''; $('plate').value = ''; validatePlate();
    await refreshState(); message('Signed out. Your bookings remain saved in your account.', 'success');
  } catch (error) { await refreshState(); message(error.message, 'error'); }
  finally { busy = false; renderAll(); }
});
for (const view of ['active', 'history']) $(`show-${view}`).addEventListener('click', () => { bookingView = view; renderBookings(); });
$('booking-search').addEventListener('input', () => {
  bookingSearch = $('booking-search').value.trim();
  clearTimeout(searchTimer); renderBookings();
  searchTimer = setTimeout(refreshState, 250);
});
$('clear-booking-search').addEventListener('click', () => {
  clearBookingSearch(); renderBookings(); refreshState(); $('booking-search').focus();
});
document.querySelectorAll('[data-filter]').forEach((b) => b.addEventListener('click', () => setFilter(b.dataset.filter)));
$('vehicle').addEventListener('change', () => setFilter($('vehicle').value));
document.querySelectorAll('.billing').forEach((button) => button.addEventListener('click', () => {
  unit = button.dataset.unit;
  document.querySelectorAll('.billing').forEach((b) => {
    b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', String(b === button));
  });
  $('duration').max = unit === 'hour' ? 24 : 30;
  $('duration-label').textContent = unit === 'hour' ? 'Number of hours' : 'Number of days';
  $('duration-hint').textContent = unit === 'hour' ? '1–24 hours · each day is 24 hours' : '1–30 days · each day is 24 hours';
  $('duration').value = Math.min(Math.max(Math.trunc(Number($('duration').value)) || 1, 1), Number($('duration').max));
  updateQuote();
}));
$('duration').addEventListener('input', updateQuote);
[['decrease', -1], ['increase', 1]].forEach(([id, step]) => $(id).addEventListener('click', () => {
  $('duration').value = Math.min(Number($('duration').max), Math.max(1, (durationValue() || 1) + step)); updateQuote();
}));
$('reserve').addEventListener('click', (event) => {
  if (!customer) { event.preventDefault(); openAccount(); }
  else { $('driver-details').open = true; validatePlate(true); }
});
$('booking-form').addEventListener('invalid', () => { $('driver-details').open = true; }, true);
$('booking-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !connected) return;
  if (!customer) { openAccount(); return; }
  const duration = durationValue(), driver = $('driver').value.trim(), plate = validatePlate(true);
  if (!selectedId || !duration || !driver || !plate) { message('Choose a space and enter a valid duration, driver name, and registration.', 'error'); return; }
  const draft = { spaceId: selectedId, unit, duration, driver, plate }, fingerprint = JSON.stringify(draft);
  // Retry a lost response with the same ID so the server cannot book twice.
  if (!pendingRequest || pendingRequest.fingerprint !== fingerprint) pendingRequest = { fingerprint, requestId: crypto.randomUUID(), expectedRate: rates[$('vehicle').value][unit] };
  busy = true; renderAll();
  try {
    if (refreshing) await refreshing;
    const result = await api('/api/bookings', { ...draft, requestId: pendingRequest.requestId, expectedRate: pendingRequest.expectedRate });
    pendingRequest = null; selectedId = null; clearBookingSearch(); bookingView = 'active';
    $('driver').value = ''; $('plate').value = ''; validatePlate(); $('driver-details').open = false;
    const refreshed = await refreshState();
    message(`Booking confirmed for ${result.booking.spaceId}: ${money(result.booking.total)}. No payment collected.${refreshed ? ' View your receipt under My bookings.' : ' Reconnect to view your booking.'}`, 'success');
  } catch (error) {
    if (error.status && error.status < 500) pendingRequest = null;
    await refreshState(); message(error.message, 'error');
  } finally { busy = false; renderAll(); }
});
$('reset-demo').addEventListener('click', () => $('reset-dialog').showModal());
$('cancel-reset').addEventListener('click', () => $('reset-dialog').close());
$('confirm-reset').addEventListener('click', () => { $('reset-dialog').close(); releaseBooking(null); });
setInterval(() => { if (!busy) refreshState(); }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !busy) refreshState(); });
setFilter(filter, false); renderAll(); refreshState();
