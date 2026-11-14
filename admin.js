'use strict';
const $ = (id) => document.getElementById(id);
const activeStatuses = ['reserved', 'parked', 'overdue'];
const statusNames = {reserved:'Awaiting arrival', parked:'On site', overdue:'Overdue · on site', departed:'Departed', cancelled:'Cancelled', released:'Released', expired:'Expired'};
let csrf = '', authenticated = false, connected = false, busy = false, loading = null;
let bookings = [], spaces = [], totalRecords = 0, view = 'active', confirmation = null;
let priceRevision = null, pricesDirty = false;
let reportRequest = 0, reportLoading = false;
let spaceChoice = null;
let paymentBooking=null, paymentData=null, paymentPending=null, correctionId=null;
let waiverPending=null, reversalId=null;
let checkoutData=null, checkoutAfterPayment=false;
let outstandingData=null, outstandingOffset=0, outstandingRequest=0, outstandingLoading=false, outstandingConnected=false;
let outstandingFilters={search:'',payment:'all',status:'all'};
const reportDateParts = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
$('report-date').value=['year','month','day'].map(type=>reportDateParts.find(part=>part.type===type).value).join('-');
function clearReport() {
  reportRequest++; reportLoading=false; $('load-report').disabled=false;
  $('report-results').hidden=true; $('report-rows').replaceChildren(); $('download-report').hidden=true; $('download-report').removeAttribute('href');
  notice('report-message','Choose a date and show its report.');
}
const reportTime = value=>new Date(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'});
const priceFields = ['truck-hour', 'truck-day', 'bus-hour', 'bus-day'];
function fillPrices(data) {
  priceRevision=data.priceRevision;
  priceFields.forEach(key=>{const [kind,unit]=key.split('-'); $(`${key}-price`).value=data.rates[kind][unit];});
  pricesDirty=false; priceControls();
}
function priceControls() {
  priceFields.forEach(key=>{$(`${key}-price`).disabled=busy||priceRevision===null;});
  $('save-prices').disabled=busy||!connected||!pricesDirty||priceRevision===null;
  $('reload-prices').disabled=busy;
}
const formatMoney = (value) => new Intl.NumberFormat('en-IN', {style:'currency',currency:'INR',maximumFractionDigits:0}).format(value);
const formatDate = (value) => new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
function notice(id, text, kind = '') { $(id).textContent=text; $(id).classList.toggle('error',kind==='error'); $(id).classList.toggle('success',kind==='success'); }
async function api(path, body) {
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),10000);
  try {
    const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,
      headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
    const data=await response.json().catch(()=>({}));
    if (!response.ok) { const error=new Error(data.error || 'Could not contact the admin server. Restart python server.py.'); error.status=response.status; throw error; }
    return data;
  } catch(error) {
    if(error.name==='AbortError'||error instanceof TypeError) throw new Error('Server unavailable. Check the VS Code terminal and refresh.');
    throw error;
  } finally { clearTimeout(timeout); }
}
function showSession() {
  $('login-panel').hidden=authenticated; $('dashboard').hidden=!authenticated; $('logout').hidden=!authenticated;
  if(!authenticated){notice('outstanding-download-message','');$('outstanding-filter-form').reset();outstandingFilters={search:'',payment:'all',status:'all'};outstandingRequest++;outstandingData=null;outstandingOffset=0;outstandingLoading=false;outstandingConnected=false;renderOutstanding();$('action-dialog').close();confirmation=null;checkoutData=null;checkoutAfterPayment=false;notice('backup-message','');$('payment-dialog').close();paymentBooking=null;paymentData=null;paymentPending=null;}
  if(!authenticated) { clearReport(); bookings=[]; spaces=[]; priceRevision=null; pricesDirty=false; $('price-form').reset(); notice('price-message',''); $('booking-rows').replaceChildren(); $('space-status').replaceChildren(); $('space-controls').replaceChildren(); $('space-dialog').close(); spaceChoice=null; notice('space-message',''); }
}
async function bootstrap() {
  try {
    const session=await api('/api/admin/session');
    csrf=session.csrfToken; authenticated=Boolean(session.authenticated);
    $('setup-note').hidden=session.configured; $('login-form').hidden=!session.configured;
    notice('login-message',session.configured?'Enter your password to continue.':'Create your password using the instructions above.');
    showSession(); if(authenticated) await loadState();
  } catch(error) { notice('login-message',error.message,'error'); }
}
function loadState() {
  if(loading) return loading;
  loading=(async()=>{
    try {
      const data=await api('/api/admin/state');
      const changed=!connected || JSON.stringify(bookings)!==JSON.stringify(data.bookings) || JSON.stringify(spaces)!==JSON.stringify(data.spaces) || totalRecords!==data.totalRecords;
      bookings=data.bookings; spaces=data.spaces; totalRecords=data.totalRecords; connected=true;
      if(!pricesDirty) fillPrices(data);
      else if(priceRevision!==data.priceRevision) notice('price-message','Prices changed in another admin window. Reload current prices before editing again.','error');
      $('sync-status').textContent=`Connected · updated ${new Date(data.serverTime).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})} · refreshes every 5 seconds`;
      // Keep focused action buttons in place when only the refresh time changes.
      if(changed)render(); priceControls();
      if($('outstanding-payments').open)await loadOutstanding();
      return true;
    } catch(error) {
      connected=false;
      if(error.status===401) { authenticated=false; showSession(); notice('login-message','Session ended. Refresh this page to sign in again.','error'); }
      else { $('sync-status').textContent=`${error.message} Displaying the last received data.`; render(); }
      return false;
    }
  })().finally(()=>{loading=null;});
  return loading;
}
function cell(tag, text, className='') {const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;return el;}
function render() {
  $('truck-count').textContent=connected?spaces.filter(s=>s.type==='truck'&&!s.occupied&&!s.blocked).length:'—';
  $('bus-count').textContent=connected?spaces.filter(s=>s.type==='bus'&&!s.occupied&&!s.blocked).length:'—';
  $('reserved-count').textContent=connected?bookings.filter(b=>b.status==='reserved').length:'—';
  $('parked-count').textContent=connected?bookings.filter(b=>['parked','overdue'].includes(b.status)).length:'—';
  const overdue=bookings.filter(b=>b.status==='overdue').length;
  $('overdue-summary').textContent=overdue?`${overdue} past booked end time · check departure`:'Checked in and still parked';
  $('space-status').replaceChildren();
  spaces.forEach(s=>{const chip=cell('span',s.blocked?`${s.id} · Blocked`:s.id,`space-chip${s.occupied?' taken':''}${s.blocked?' blocked':''}`);chip.setAttribute('title',`${s.type} ${s.id}: ${s.blocked?s.blockReason:s.occupied?'occupied or reserved':'available'}`);$('space-status').append(chip);});
  renderSpaceControls();
  renderRows(); outstandingControls(); $('refresh').disabled=busy; $('logout').disabled=busy;
  $('download-backup').disabled=busy||!connected;
  priceControls();
}
function outstandingControls() {
  for(const field of $('outstanding-filter-form').elements)field.disabled=busy;
  const disabled=busy||outstandingLoading||!outstandingConnected||!connected;
  $('refresh-outstanding').disabled=busy||outstandingLoading;
  $('download-outstanding').disabled=disabled||!outstandingData;
  $('outstanding-prev').disabled=disabled||!outstandingData||outstandingData.offset===0;
  $('outstanding-next').disabled=disabled||!outstandingData||outstandingData.offset+outstandingData.limit>=outstandingData.count;
  document.querySelectorAll('#outstanding-rows button').forEach(button=>button.disabled=disabled);
}
function renderOutstanding() {
  const d=outstandingData;
  const filters=d?.filters||outstandingFilters;
  const labels=[];
  if(filters.search)labels.push(`Search: “${filters.search}”`);
  if(filters.payment!=='all')labels.push(filters.payment==='unpaid'?'Unpaid only':'Part paid only');
  if(filters.status==='departed')labels.push('Checked-out vehicles only');
  $('outstanding-applied').textContent=labels.length?`Applied filters: ${labels.join(' · ')}`:'Showing all outstanding bookings.';
  $('outstanding-empty').textContent=labels.length?'No outstanding bookings match these filters.':'No outstanding payments. All booking balances are settled.';
  $('outstanding-total').textContent=d?formatMoney(d.balance):'—';
  $('outstanding-count').textContent=d?d.count:'—';
  $('outstanding-rows').replaceChildren();$('outstanding-empty').hidden=!d||d.count!==0;
  $('outstanding-page').textContent=d&&d.count?`${d.offset+1}–${d.offset+d.bookings.length} of ${d.count} · oldest first`:'';
  if(d)d.bookings.forEach(b=>{
    const row=cell('tr',''),who=cell('td',''),where=cell('td',''),balance=cell('td',''),action=cell('td','');
    who.append(cell('strong',b.plate),cell('small',b.driver),cell('small',b.id),cell('small',formatDate(b.start)));
    where.append(cell('strong',`${b.spaceId} · ${b.type==='truck'?'Truck':'Bus'}`),cell('small',statusNames[b.status]||b.status));
    balance.append(cell('strong',formatMoney(b.balance)),cell('small',b.paymentStatus));
    const button=cell('button','Record payment','button secondary');button.type='button';button.setAttribute('aria-label',`Record payment for ${b.plate} booking ${b.id}`);button.addEventListener('click',()=>openPayments(b));action.append(button);
    const received=cell('td','');received.append(cell('strong',formatMoney(b.paid)),cell('small',`${formatMoney(b.waived)} waived`));
    row.append(who,where,cell('td',formatMoney(b.total)),received,balance,action);
    [...row.children].forEach((td,index)=>{td.dataset.label=['Vehicle / booking','Space / status','Booked','Paid','Remaining','Action'][index];});
    $('outstanding-rows').append(row);
  });outstandingControls();
}
async function loadOutstanding() {
  if(!authenticated||outstandingLoading)return;
  const request=++outstandingRequest;outstandingLoading=true;outstandingControls();
  if(!outstandingData)notice('outstanding-message','Loading pending balances…');
  try {
    const query=new URLSearchParams({...outstandingFilters,offset:String(outstandingOffset)});
    const d=await api(`/api/admin/outstanding?${query}`);
    if(request!==outstandingRequest||!authenticated)return;
    const changed=!outstandingData||JSON.stringify({...outstandingData,serverTime:0})!==JSON.stringify({...d,serverTime:0});
    outstandingData=d;outstandingOffset=d.offset;outstandingConnected=true;
    if(changed)renderOutstanding();
    notice('outstanding-message',`Totals cover all ${d.count} matching bookings across every results page. Updates every 5 seconds while this section is open.`);
  } catch(error) {
    if(request!==outstandingRequest)return;
    outstandingConnected=false;
    notice('outstanding-message',`${error.message}${outstandingData?' Showing previously loaded balances. Refresh to update.':''}`,'error');
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh to sign in again.','error');}
  } finally {if(request===outstandingRequest){outstandingLoading=false;outstandingControls();}}
}
function applyOutstandingFilters(clear=false) {
  if(busy)return;
  if(clear)$('outstanding-filter-form').reset();
  outstandingFilters={search:$('outstanding-search').value.trim(),payment:$('outstanding-payment-filter').value,status:$('outstanding-status-filter').value};
  notice('outstanding-download-message','');
  outstandingRequest++;outstandingLoading=false;outstandingConnected=false;outstandingData=null;outstandingOffset=0;
  renderOutstanding();loadOutstanding();
}
$('download-outstanding').addEventListener('click',async()=>{
  if(busy||outstandingLoading||!outstandingData||!outstandingConnected||!connected||!authenticated)return;
  const query=new URLSearchParams(outstandingFilters);
  busy=true;render();notice('outstanding-download-message','Preparing all matching bookings for Excel…');
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),30000);
  try {
    const response=await fetch(`/api/admin/outstanding.xlsx?${query}`,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(!response.ok){const data=await response.json().catch(()=>({}));const error=new Error(data.error||'Could not prepare the Excel report. Try again.');error.status=response.status;throw error;}
    if(!response.headers.get('Content-Type')?.includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))throw new Error('The server did not return an Excel file. Restart the server and try again.');
    const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
    link.href=url;link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||'parking-outstanding.xlsx';
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
    notice('outstanding-download-message','Excel download started. It contains all matching bookings at download time. Check your Downloads folder.','success');
  } catch(error) {
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh to sign in again.','error');}
    else notice('outstanding-download-message',error.name==='AbortError'?'The download took too long. Try narrower filters or retry.':error instanceof TypeError?'Cannot reach the server. Check the VS Code terminal and try again.':error.message,'error');
  } finally {clearTimeout(timeout);busy=false;render();}
});
$('outstanding-filter-form').addEventListener('submit',event=>{event.preventDefault();applyOutstandingFilters();});
$('clear-outstanding-filters').addEventListener('click',()=>applyOutstandingFilters(true));
$('outstanding-payments').addEventListener('toggle',()=>{if($('outstanding-payments').open)loadOutstanding();});
$('refresh-outstanding').addEventListener('click',()=>{if(!busy)loadState();});
for(const [id,direction] of [['outstanding-prev',-1],['outstanding-next',1]])$(id).addEventListener('click',()=>{
  if(busy||outstandingLoading||!outstandingData)return;
  outstandingOffset=Math.max(0,outstandingData.offset+direction*outstandingData.limit);loadOutstanding();
});
function renderSpaceControls() {
  $('space-controls').replaceChildren();
  spaces.forEach(s=>{
    const card=cell('article','',`space-control${s.blocked?' blocked':''}`);
    card.append(cell('h3',`${s.id} · ${s.type==='truck'?'Truck':'Bus'}`),
      cell('strong',s.blocked?'Blocked':s.occupied?'Occupied or reserved':'Available','space-state'),
      cell('p',s.blocked?s.blockReason:s.occupied?'Can be blocked after the booking ends or the vehicle leaves.':'Ready for booking.','muted'));
    const button=cell('button',s.blocked?'Reopen space':'Block space','button secondary');
    button.type='button';button.disabled=busy||!connected||Boolean(s.occupied);
    button.setAttribute('aria-label',`${s.blocked?'Reopen':'Block'} space ${s.id}`);
    button.addEventListener('click',()=>{
      spaceChoice={id:s.id,blocked:!s.blocked,blockRevision:s.blockRevision};
      $('space-dialog-title').textContent=`${s.blocked?'Reopen':'Block'} space ${s.id}`;
      $('space-dialog-description').textContent=s.blocked?'This space will become available for new bookings.':'New bookings will be disabled until you reopen this space.';
      $('block-reason-field').hidden=Boolean(s.blocked);$('block-reason').disabled=Boolean(s.blocked);
      $('block-reason').value='';$('confirm-space').textContent=s.blocked?'Reopen space':'Block space';
      $('space-dialog').showModal();
      if(!s.blocked)$('block-reason').focus();
    });
    card.append(button);$('space-controls').append(card);
  });
}
$('dismiss-space').addEventListener('click',()=>{$('space-dialog').close();spaceChoice=null;});
$('space-dialog').addEventListener('cancel',()=>{spaceChoice=null;});
$('space-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy||!connected||!spaceChoice)return;
  const choice={...spaceChoice,reason:$('block-reason').value.trim()};
  if(choice.blocked&&!choice.reason){$('block-reason').focus();return;}
  $('space-dialog').close();spaceChoice=null;busy=true;render();
  try {
    if(loading)await loading;
    await api(`/api/admin/spaces/${encodeURIComponent(choice.id)}/block`,choice);
    const refreshed=await loadState();
    notice('space-message',`${choice.id} ${choice.blocked?'blocked for new bookings':'reopened for bookings'}.${refreshed?'':' Reconnect to view its latest status.'}`,'success');
  } catch(error) {await loadState();notice('space-message',error.message,'error');}
  finally {busy=false;render();}
});
function paymentControls() {
  for(const id of ['return-checkout','close-payments','reload-payments','payment-amount','payment-method','save-correction','cancel-correction','correction-reason'])$(id).disabled=busy;
  const waiverOpen=!$('waiver-form').hidden;
  $('save-payment').disabled=busy||!paymentData||paymentData.balance<=0||correctionId!==null||waiverOpen;
  $('open-waiver').disabled=busy||!paymentData||paymentData.balance<=0||correctionId!==null||waiverOpen;
  for(const id of ['cancel-waiver','waiver-reason'])$(id).disabled=busy;
  $('waiver-amount').disabled=busy||reversalId!==null;
  $('save-waiver').disabled=busy||!paymentData||(reversalId===null&&paymentData.balance<=0);
  document.querySelectorAll('#payment-entries button, #waiver-entries button').forEach(button=>button.disabled=busy||waiverOpen||correctionId!==null);
}
async function loadPayments() {
  paymentData=null;paymentControls();
  paymentData=await api(`/api/admin/bookings/${paymentBooking.id}/payments`);
  const d=paymentData;
  $('payment-booking').textContent=`${d.plate} · ${d.spaceId} · ${d.bookingId}`;
  $('payment-balance').textContent=`${d.status} · Booking ${formatMoney(d.total)} · Received ${formatMoney(d.paid)} · Waived ${formatMoney(d.waived)} · Balance ${formatMoney(d.balance)}`;
  $('payment-amount').max=d.balance;$('payment-amount').value=d.balance>0?d.balance:'';
  $('payment-entries').replaceChildren();
  if(!d.entries.length)$('payment-entries').append(cell('p','No payments recorded.','muted'));
  d.entries.forEach(entry=>{
    const item=cell('article','','payment-entry');
    item.append(cell('strong',`${formatMoney(entry.amount)} · ${entry.method.toUpperCase()}`),cell('p',`Entry ${entry.id} · ${formatDate(entry.created_at)}`,'muted'));
    if(entry.voided_at!==null)item.append(cell('p',`Corrected ${formatDate(entry.voided_at)} · ${entry.void_reason}`,'muted'));
    else {const button=cell('button','Correct entry','button secondary');button.type='button';button.setAttribute('aria-label',`Correct payment entry ${entry.id}`);button.addEventListener('click',()=>{
      correctionId=entry.id;$('correction-title').textContent=`Correct ${formatMoney(entry.amount)} ${entry.method.toUpperCase()} entry?`;
      $('correction-form').hidden=false;$('correction-reason').value='';$('correction-reason').focus();paymentControls();
    });item.append(button);}
    $('payment-entries').append(item);
  });
  $('waiver-entries').replaceChildren();
  if(!d.waivers.length)$('waiver-entries').append(cell('p','No waivers recorded.','muted'));
  d.waivers.forEach(entry=>{
    const item=cell('article','','payment-entry');
    item.append(cell('strong',`${formatMoney(entry.amount)} ${entry.voided_at===null?'waived':'waiver reversed'}`),cell('p',`Waiver ${entry.id} · ${formatDate(entry.created_at)}`,'muted'),cell('p',entry.reason));
    if(entry.voided_at!==null)item.append(cell('p',`Reversed ${formatDate(entry.voided_at)} · ${entry.void_reason}`,'muted'));
    else {const button=cell('button','Reverse waiver','button secondary');button.type='button';button.setAttribute('aria-label',`Reverse waiver ${entry.id}`);
      button.addEventListener('click',()=>openWaiver(entry));item.append(button);}
    $('waiver-entries').append(item);
  });paymentControls();
}
function closeWaiver() {
  $('waiver-form').hidden=true;reversalId=null;waiverPending=null;paymentControls();
}
function openWaiver(entry=null) {
  if(busy||!paymentData)return;
  reversalId=entry?.id??null;waiverPending=null;$('waiver-reason').value='';
  $('waiver-title').textContent=entry?'Reverse this waiver?':'Waive an amount';
  $('waiver-description').textContent=entry
    ? `This restores ${formatMoney(entry.amount)} to the amount owed. The original waiver and your reversal reason stay in history.`
    : `Current unpaid balance: ${formatMoney(paymentData.balance)}. Enter part or all of this amount to waive. No money is collected or refunded. Parking status stays unchanged.`;
  $('waiver-amount-field').hidden=Boolean(entry);$('waiver-amount').required=!entry;
  $('waiver-amount').value='';$('waiver-amount').max=paymentData.balance;
  $('save-waiver').textContent=entry?'Confirm reversal':'Confirm waiver';
  $('waiver-form').hidden=false;updateWaiverPreview();paymentControls();$(entry?'waiver-reason':'waiver-amount').focus();
}
function updateWaiverPreview() {
  if(reversalId!==null||!paymentData)return;
  const amount=Number($('waiver-amount').value);
  const valid=$('waiver-amount').value!==''&&Number.isSafeInteger(amount)&&amount>0&&amount<=paymentData.balance;
  notice('waiver-preview',valid?`Waiving ${formatMoney(amount)} leaves ${formatMoney(paymentData.balance-amount)} unpaid.`:`Enter a whole-rupee amount from ₹1 to ${formatMoney(paymentData.balance)}.`);
  $('save-waiver').textContent=valid?`Waive ${formatMoney(amount)}`:'Confirm waiver';
}
$('waiver-amount').addEventListener('input',updateWaiverPreview);
$('open-waiver').addEventListener('click',()=>openWaiver());
$('cancel-waiver').addEventListener('click',closeWaiver);
$('waiver-form').addEventListener('submit',event=>{
  event.preventDefault();if(busy||!paymentData)return;
  const reason=$('waiver-reason').value.trim();if(!reason){$('waiver-reason').focus();return;}
  if(reversalId!==null){savePaymentAction(`/api/admin/bookings/${paymentBooking.id}/waivers/${reversalId}/reverse`,{reason},'Waiver reversed. The amount is owed again.');return;}
  const amount=Number($('waiver-amount').value);
  if(!Number.isSafeInteger(amount)||amount<=0||amount>paymentData.balance){$('waiver-amount').reportValidity();return;}
  if(!waiverPending||waiverPending.reason!==reason||waiverPending.amount!==amount)waiverPending={amount,reason,expectedTotal:paymentData.total,expectedPaid:paymentData.paid,expectedWaived:paymentData.waived,requestId:crypto.randomUUID()};
  savePaymentAction(`/api/admin/bookings/${paymentBooking.id}/waivers`,waiverPending,`${formatMoney(amount)} waived. ${formatMoney(paymentData.balance-amount)} remains unpaid. No payment was recorded.`);
});
async function openPayments(booking, fromCheckout=false) {
  if(busy||!connected)return;
  checkoutAfterPayment=fromCheckout;$('return-checkout').hidden=!fromCheckout;
  paymentBooking=booking;paymentData=null;paymentPending=null;correctionId=null;
  closeWaiver();$('waiver-entries').replaceChildren();
  $('correction-form').hidden=true;$('payment-entries').replaceChildren();$('payment-booking').textContent=booking.plate;
  $('payment-balance').textContent='Loading payments…';notice('payment-message','');$('payment-dialog').showModal();
  busy=true;render();paymentControls();
  try{await loadPayments();}catch(error){notice('payment-message',error.message,'error');}
  finally{busy=false;render();paymentControls();}
}
async function savePaymentAction(path,data,success) {
  busy=true;render();paymentControls();notice('payment-message','Saving…');
  try {
    if(loading)await loading;
    await api(path,data);paymentPending=null;correctionId=null;$('correction-form').hidden=true;
    closeWaiver();
    await loadPayments();await loadState();clearReport();notice('payment-message',success,'success');
  } catch(error) {
    if(error.status===409){paymentPending=null;waiverPending=null;paymentData=null;}
    notice('payment-message',`${error.message} Reload payments to check what was saved before trying again.`,'error');
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh to sign in again.','error');}
  } finally{busy=false;render();paymentControls();}
}
$('payment-form').addEventListener('submit',event=>{
  event.preventDefault();if(busy||!paymentData||correctionId!==null)return;
  const amount=Number($('payment-amount').value),method=$('payment-method').value;
  const signature=JSON.stringify([paymentBooking.id,amount,method]);
  if(!paymentPending||paymentPending.signature!==signature)paymentPending={signature,data:{amount,method,expectedTotal:paymentData.total,expectedPaid:paymentData.paid,expectedWaived:paymentData.waived,requestId:crypto.randomUUID()}};
  savePaymentAction(`/api/admin/bookings/${paymentBooking.id}/payments`,paymentPending.data,'Payment recorded.');
});
$('correction-form').addEventListener('submit',event=>{
  event.preventDefault();if(busy||correctionId===null)return;
  savePaymentAction(`/api/admin/bookings/${paymentBooking.id}/payments/${correctionId}/void`,{reason:$('correction-reason').value.trim()},'Incorrect entry corrected. Original entry remains in history.');
});
$('cancel-correction').addEventListener('click',()=>{correctionId=null;$('correction-form').hidden=true;paymentControls();});
$('close-payments').addEventListener('click',()=>{if(!busy)$('payment-dialog').close();});
$('payment-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();});
$('reload-payments').addEventListener('click',async()=>{
  if(busy)return;busy=true;paymentControls();
  try{correctionId=null;paymentPending=null;closeWaiver();$('correction-form').hidden=true;await loadPayments();notice('payment-message','Latest payments and waivers loaded. Review entries and balance.');}
  catch(error){notice('payment-message',error.message,'error');}
  finally{busy=false;paymentControls();}
});
function checkoutControls() {
  const checkout=confirmation?.action==='check-out';
  $('dismiss-action').disabled=busy;
  $('reload-checkout').disabled=busy;
  $('allow-unpaid').disabled=busy||!checkoutData;
  $('checkout-pay').disabled=busy||!checkoutData||checkoutData.balance<=0;
  $('confirm-action').disabled=busy||(checkout&&(!checkoutData||(checkoutData.balance>0&&!$('allow-unpaid').checked)));
}
async function loadCheckoutBalance() {
  checkoutData=null;$('allow-unpaid').checked=false;$('unpaid-confirmation').hidden=true;
  $('checkout-balance').textContent='Loading the latest balance…';notice('checkout-message','');checkoutControls();
  try {
    const d=await api(`/api/admin/bookings/${confirmation.booking.id}/payments`);
    checkoutData=d;
    $('checkout-balance').textContent=`Booked ${formatMoney(d.total)} · Paid ${formatMoney(d.paid)} · Waived ${formatMoney(d.waived)} · Remaining ${formatMoney(d.balance)}`;
    $('unpaid-confirmation').hidden=d.balance<=0;
    $('unpaid-label').textContent=`I confirm checkout with ${formatMoney(d.balance)} still unpaid.`;
    $('confirm-action').textContent=d.balance>0?'Check out with balance':'Confirm check-out';
    notice('checkout-message',d.balance>0?'Record payment now, or confirm that this balance will remain unpaid after checkout.':d.waived?'Balance settled, including a waiver. Confirm once the vehicle has left.':'Fully paid. Confirm once the vehicle has left.');
  } catch(error) {
    $('checkout-balance').textContent='Balance unavailable. Checkout has not been confirmed.';
    notice('checkout-message',error.message,'error');
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh to sign in again.','error');}
  }
}
async function openCheckout(booking) {
  if(busy||!connected)return;
  confirmation={booking,action:'check-out'};checkoutData=null;
  $('dialog-title').textContent='Review payment before checkout';
  $('dialog-description').textContent=`Has ${booking.plate} left ${booking.spaceId}? Confirming checkout releases this parking space.`;
  $('checkout-payment').hidden=false;$('action-dialog').showModal();
  busy=true;render();checkoutControls();
  try {await loadCheckoutBalance();}finally{busy=false;render();checkoutControls();}
}
$('allow-unpaid').addEventListener('change',checkoutControls);
$('reload-checkout').addEventListener('click',async()=>{
  if(busy||confirmation?.action!=='check-out')return;
  busy=true;render();checkoutControls();
  try{await loadCheckoutBalance();}finally{busy=false;render();checkoutControls();}
});
$('checkout-pay').addEventListener('click',()=>{
  if(busy||!checkoutData||!confirmation)return;
  const booking=confirmation.booking;confirmation=null;$('action-dialog').close();openPayments(booking,true);
});
$('return-checkout').addEventListener('click',()=>{
  if(busy||!checkoutAfterPayment||!paymentBooking)return;
  const booking=paymentBooking;$('payment-dialog').close();checkoutAfterPayment=false;openCheckout(booking);
});
function renderRows() {
  const search=$('search').value.trim().toLowerCase(), type=$('vehicle-filter').value;
  const filtered=bookings.filter(b=>(view==='all'||(view==='active')===activeStatuses.includes(b.status))&&(type==='all'||type===b.type)&&
    [b.plate,b.driver,b.id,b.spaceId].some(value=>value.toLowerCase().includes(search)));
  $('record-summary').textContent=`${filtered.length} shown · ${totalRecords} total records${totalRecords>bookings.length?' · active bookings and recent history loaded':''}`;
  $('booking-rows').replaceChildren(); $('empty-state').hidden=filtered.length>0;
  filtered.forEach(b=>{
    const row=document.createElement('tr'), who=document.createElement('td');
    who.append(cell('strong',b.plate,'registration'),cell('small',b.driver),cell('small',b.id));
    const space=document.createElement('td');space.append(cell('strong',b.spaceId),cell('small',b.type==='truck'?'Truck':'Bus'));
    const stay=document.createElement('td');stay.append(cell('strong',formatDate(b.start)),cell('small',`Until ${formatDate(b.end)}`));
    if(b.checkedInAt)stay.append(cell('small',`Arrived ${formatDate(b.checkedInAt)}`));
    if(b.checkedOutAt)stay.append(cell('small',`Departed ${formatDate(b.checkedOutAt)}`));
    const status=document.createElement('td');status.append(cell('span',statusNames[b.status],`status ${b.status}`));
    const actions=document.createElement('td'), buttons=document.createElement('div');buttons.className='row-actions';
    const choices=b.status==='reserved'?[['check-in','Check in'],['cancel','Cancel']]:['parked','overdue'].includes(b.status)?[['check-out','Check out']]:[];
    choices.forEach(([action,label])=>{
      const button=cell('button',label,action==='cancel'?'cancel':'');button.type='button';button.disabled=busy||!connected;
      button.setAttribute('aria-label',`${label} ${b.plate} in ${b.spaceId}`);
      button.addEventListener('click',()=>{
        if(action==='check-in') performAction(b,action);
        else if(action==='check-out') openCheckout(b);
        else {
          $('checkout-payment').hidden=true;$('confirm-action').disabled=false;$('dismiss-action').disabled=false;
          confirmation={booking:b,action};
          $('dialog-title').textContent=action==='cancel'?'Cancel this booking?':'Confirm vehicle departure';
          $('dialog-description').textContent=action==='cancel'?`Cancel ${b.plate} in ${b.spaceId}? The space will become available and the booking will stay in history.`:`Has ${b.plate} left ${b.spaceId}? Confirming check-out makes this space available for another booking.`;
          $('confirm-action').textContent=action==='cancel'?'Cancel booking':'Confirm check-out';
          $('action-dialog').showModal();
        }
      });buttons.append(button);
    });
    const payButton=cell('button','Payments');payButton.type='button';payButton.disabled=busy||!connected;payButton.setAttribute('aria-label',`Payments for ${b.plate} in ${b.spaceId}`);payButton.addEventListener('click',()=>openPayments(b));buttons.append(payButton);
    const amount=cell('td','');amount.append(cell('strong',formatMoney(b.total)),cell('small',b.paymentStatus),cell('small',`${formatMoney(b.paid)} paid · ${formatMoney(b.waived)} waived · ${formatMoney(b.balance)} balance`));
    actions.append(buttons);row.append(who,space,stay,amount,status,actions);$('booking-rows').append(row);
  });
}
async function performAction(booking,action,data={}) {
  if(busy||!connected)return;
  busy=true;render();
  try {
    if(loading)await loading;
    await api(`/api/admin/bookings/${encodeURIComponent(booking.id)}/${action}`,data);
    clearReport();
    const refreshed=await loadState();
    notice('action-message',`${booking.plate}: ${action==='check-in'?'arrival recorded':action==='check-out'?'departure recorded; space released':'booking cancelled; space released'}.${refreshed?'':' Reconnect to view the updated status.'}`,'success');
  } catch(error) {await loadState();notice('action-message',error.message,'error');}
  finally{busy=false;render();}
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;busy=true;$('login-button').disabled=true;
  try{
    const result=await api('/api/admin/login',{password:$('password').value});
    csrf=result.csrfToken;authenticated=true;showSession();notice('action-message','');await loadState();
  }catch(error){notice('login-message',error.message,'error');}
  finally{$('password').value='';busy=false;$('login-button').disabled=false;if(authenticated)render();}
});
$('logout').addEventListener('click',async()=>{
  if(busy)return;busy=true;render();
  try{if(loading)await loading;await api('/api/admin/logout',{});authenticated=false;connected=false;showSession();await bootstrap();}
  catch(error){notice('action-message',error.message,'error');}
  finally{busy=false;if(authenticated)render();}
});
$('refresh').addEventListener('click',()=>loadState());
$('download-backup').addEventListener('click',async()=>{
  if(busy||!connected||!authenticated)return;
  busy=true;render();notice('backup-message','Preparing your database backup…');
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),60000);
  try {
    const response=await fetch('/api/admin/backup',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(!response.ok) {
      const data=await response.json().catch(()=>({}));
      const error=new Error(data.error||'Could not create the backup. Please try again.');error.status=response.status;throw error;
    }
    const blob=await response.blob();
    if(!response.headers.get('Content-Type')?.includes('application/vnd.sqlite3'))throw new Error('The server did not return a database backup. Restart the server and try again.');
    const url=URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url;link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||'parking-backup.sqlite3';
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
    notice('backup-message','Backup download started. Check your Downloads folder and keep a private copy on another drive.','success');
  } catch(error) {
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh this page to sign in again.','error');}
    else notice('backup-message',error.name==='AbortError'?'Backup took too long. Check the server and try again.':error instanceof TypeError?'Cannot reach the server. Check the VS Code terminal and try again.':error.message,'error');
  } finally {clearTimeout(timeout);busy=false;render();}
});
$('report-date').addEventListener('input',clearReport);
$('report-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(reportLoading||!authenticated)return;
  clearReport(); const request=reportRequest, day=$('report-date').value;
  reportLoading=true; $('load-report').disabled=true; notice('report-message','Loading daily report…');
  try {
    const data=await api(`/api/admin/reports?date=${encodeURIComponent(day)}`);
    if(request!==reportRequest||!authenticated)return;
    for(const [key,value] of Object.entries(data.summary)) $(`report-${key}`).textContent=key.endsWith('Amount')?formatMoney(value):value;
    data.events.forEach(event=>{
      const row=document.createElement('tr'), what=document.createElement('td'), who=document.createElement('td'), booking=document.createElement('td');
      what.append(cell('strong',event.event),cell('small',reportTime(event.time)));
      who.append(cell('strong',event.plate),cell('small',event.driver),cell('small',event.type==='truck'?'Truck':'Bus'));
      booking.append(cell('strong',event.spaceId),cell('small',event.bookingId));
      row.append(what,who,booking,cell('td',event.amount===null?'—':formatMoney(event.amount)),cell('td',statusNames[event.currentStatus]||event.currentStatus));
      $('report-rows').append(row);
    });
    $('report-empty').hidden=data.events.length>0; $('report-records').textContent=`${data.events.length} events · all records for ${data.date}`;
    for(const [key,value] of Object.entries(data.collections))$(`collection-${key}`).textContent=formatMoney(value);
    $('collection-rows').replaceChildren();$('collection-empty').hidden=data.payments.length>0;
    data.payments.forEach(p=>{const row=cell('tr',''),what=cell('td',''),who=cell('td','');what.append(cell('strong',`${p.event} #${p.id}`),cell('small',reportTime(p.time)));who.append(cell('strong',`${p.plate} · ${p.spaceId}`),cell('small',p.bookingId));row.append(what,who,cell('td',p.method.toUpperCase()),cell('td',formatMoney(p.amount)),cell('td',p.note));$('collection-rows').append(row);});
    $('report-results').hidden=false; $('download-report').href=`/api/admin/reports.xlsx?date=${encodeURIComponent(day)}`; $('download-report').hidden=false;
    notice('report-message',`Report for ${data.date} · generated ${reportTime(data.generatedAt)} IST. Show report again to refresh. The Excel download uses the latest saved data.`);
  } catch(error) {
    if(request!==reportRequest)return;
    if(error.status===401){authenticated=false;showSession();notice('login-message','Session ended. Refresh this page to sign in again.','error');}
    else notice('report-message',error.message,'error');
  } finally {if(request===reportRequest){reportLoading=false;$('load-report').disabled=false;}}
});
$('price-form').addEventListener('input',()=>{
  pricesDirty=true; notice('price-message','Unsaved changes. Save to apply these prices to new bookings.'); priceControls();
});
$('price-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(busy||!connected||!pricesDirty)return;
  const rates={truck:{},bus:{}};
  priceFields.forEach(key=>{const [kind,unit]=key.split('-');rates[kind][unit]=Number($(`${key}-price`).value);});
  const revision=priceRevision;
  busy=true;render();
  try{
    if(loading)await loading;
    const result=await api('/api/admin/prices',{rates,priceRevision:revision});
    fillPrices(result); notice('price-message','Prices saved. New bookings use these rates; existing bookings keep their original amounts.','success');
    await loadState();
  }catch(error){await loadState();notice('price-message',error.message,'error');}
  finally{busy=false;render();}
});
$('reload-prices').addEventListener('click',async()=>{
  if(busy)return;busy=true;render();
  try{
    if(loading)await loading;
    const data=await api('/api/admin/state'); fillPrices(data);
    notice('price-message','Current prices loaded. Your unsaved edits have been cleared.');
    await loadState();
  }catch(error){notice('price-message',error.message,'error');}
  finally{busy=false;render();}
});
$('search').addEventListener('input',renderRows);$('vehicle-filter').addEventListener('change',renderRows);
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{
  view=button.dataset.view;document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});renderRows();
}));
$('dismiss-action').addEventListener('click',()=>{if(busy)return;$('action-dialog').close();confirmation=null;checkoutData=null;});
$('action-dialog').addEventListener('cancel',event=>{if(busy){event.preventDefault();return;}confirmation=null;checkoutData=null;});
$('confirm-action').addEventListener('click',()=>{
  if(busy||!confirmation||$('confirm-action').disabled)return;
  const choice=confirmation;
  const data=choice.action==='check-out'?{expectedTotal:checkoutData.total,expectedPaid:checkoutData.paid,expectedWaived:checkoutData.waived,allowUnpaid:$('allow-unpaid').checked}:{};
  confirmation=null;checkoutData=null;$('action-dialog').close();performAction(choice.booking,choice.action,data);
});
setInterval(()=>{if(authenticated&&!busy&&!$('action-dialog').open&&!$('space-dialog').open&&!$('payment-dialog').open)loadState();},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&authenticated&&!busy)loadState();});
bootstrap();
