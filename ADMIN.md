# Admin page

## First-time setup in VS Code

1. Open your Desktop `smart-parking` folder.
2. Stop the running server using **Ctrl+C**.
3. In the terminal, run:

   ```powershell
   python server.py --set-admin
   ```

4. Choose a password with **10–128 characters**, then enter it again. Password entry is hidden: no letters or dots will appear as you type. This is normal. There is no default password.
5. Start the server:

   ```powershell
   python server.py
   ```

6. Open **http://localhost:8000/admin**, then enter your password. No username is required. The customer website remains at **http://localhost:8000**.

If `python` is unavailable, use `py -3` in its place. If you use a different server port, use that same port in the admin address. After upgrading, press **Ctrl+F5** on both pages once.

Existing bookings remain in the same database. When an old database first opens with this version, the server creates `data/parking.before-admin.sqlite3` before adding the check-in/check-out fields. The old source files are preserved in `backup-before-admin.zip` in your Desktop project folder.

## What you can do

- See available truck and bus spaces, vehicles awaiting arrival, vehicles on site, and overdue stays.
- View bookings from all customer browsers, including driver names and registrations.
- Search by driver, registration, space, or booking ID; filter by trucks/buses and Active/History/All.
- **Check in** a booked vehicle when it arrives.
- **Check out** a vehicle after it leaves. Confirming departure releases its space.
- **Cancel** an unarrived booking. Confirmation releases the space and preserves the record in History.
- Open **Price settings** to change hourly and daily prices for trucks and buses.
- Sign out when finished.

The dashboard updates every 5 seconds. Actions are disabled if the server cannot be reached. It loads all active bookings and recent history, up to 500 records total, and displays the total record count. Prices are estimates, not payments collected.

## Price settings

Open **Price settings** above Bookings. Enter all four hourly/daily prices in whole rupees (₹1–₹1,00,000), then click **Save prices**. They apply to new bookings and persist across server restarts. Existing bookings and receipts keep their original amounts. Customer pages pick up saved prices on their normal 5-second refresh.

Unsaved edits stay in place while the dashboard refreshes. **Reload current prices** discards those edits. If another admin window saves first, your save is rejected: reload the current prices before editing again. Customer bookings submitted with an outdated displayed rate are rejected so the customer can review the new total.

This upgrade adds a price settings table without altering booking records. The previous source files are in `backup-before-prices.zip` in the Desktop project folder.

## Daily reports

Open **Daily reports**, choose a date, then click **Show report**. Dates use **Indian Standard Time (IST, UTC+05:30)** from midnight inclusive to the following midnight exclusive, regardless of the computer's timezone. The default date is today in IST.

The report shows new bookings, confirmed extensions, actual arrivals (admin check-ins), and actual departures (admin check-outs). Movements are counted on the day they happened even when the booking was created earlier. The table includes every matching event, without the dashboard's 500-record limit; a booking can appear on several event rows. Empty dates display zero totals.

**Gross booked amount is not payment received.** It adds original booking amounts recorded that day and extension amounts confirmed that day. Later extensions do not change the original day's amount. Cancelled and released bookings remain included, with their current status shown; this is not a net revenue or refund report. Status reflects the time the report is generated, not the historical status at midnight.

Click **Download Excel (.xlsx)** to download the selected date's summary and complete event table in separate sheets. The workbook includes sized columns, wrapped text, numeric dates and rupee amounts, frozen event headings, and filters. Customer text, including names in Indian languages, stays text instead of running as spreadsheet formulas. No extra installation is needed. Download a new workbook after this update; previously downloaded CSV files do not gain formatting automatically. The download regenerates the report using the latest saved data. Reports are manual snapshots: click **Show report** again to update the page after new activity.

Both the screen and download require an active admin login. Reports do not change bookings. The previous source version is saved in `backup-before-reports.zip` in the Desktop project folder.

## Booking statuses

| Status | Meaning | Admin action |
| --- | --- | --- |
| Awaiting arrival | Valid booking, vehicle has not checked in | Check in or Cancel |
| On site | Arrival recorded and booked time has not ended | Check out |
| Overdue · on site | Arrival recorded, booked end time has passed | Check out after actual departure |
| Departed | Admin confirmed departure | View history |
| Cancelled | Admin cancelled an unarrived booking | View history |
| Released | Customer released an unarrived booking | View history |
| Expired | Unarrived booking reached its end time | View history; create a new booking for a late arrival |

**A checked-in vehicle does not automatically free its space at the booked end time.** It stays occupied until admin check-out, including across server restarts. Customers cannot release checked-in vehicles. Unarrived bookings still expire automatically as before. Existing active bookings begin as Awaiting arrival because the earlier version did not record arrivals; check in any vehicles already parked.

## Quick test

1. In the customer page, create or sign into a customer account, then book T01 for 2 hours using fictional driver details.
2. Open the admin page and sign in. The booking should appear as Awaiting arrival.
3. Click **Check in**. The status becomes On site. The customer page shows that admin check-out is required.
4. Click **Check out**, review the balance, and either record payment or explicitly confirm checkout with an unpaid balance. The truck availability returns to 5 if there are no other truck bookings.
5. Open **History** in the admin page to see the departure record.
6. Create another customer booking, then test **Cancel** from the admin page.

## Password and sessions

The password is stored as a salted PBKDF2 hash, not plaintext. Admin sessions are separate from customer sessions and expire after 8 hours. Changing the password invalidates all existing admin sessions. Five wrong password attempts trigger a one-minute login cooldown. The local terminal command is the recovery route: stop the server and run `python server.py --set-admin` again to replace a forgotten password.

The server checks admin access and CSRF protection on every administrative API action; hiding dashboard controls is not the access control. Arrival, departure, and cancellation actions are recorded in the `admin_events` database table. There is one local admin account in this version, without multiple staff roles.

The server is still local-only. This page is intended for testing on your computer, not public hosting. Keep database backups private because they contain booking details and authentication records.

## Customer accounts

Customers can now use **My bookings → Extend time** to preview and confirm extra hours or days. The dashboard picks up the new end time and total on its normal refresh. Original charges remain unchanged; additional time uses the current rate. Checked-in overdue vehicles can extend if the added time brings the end time into the future. An extension does not check the vehicle out or free its space.

Customer signup and sign-in are separate from admin access. Customers see only their own active bookings and history across browsers; admin still sees all bookings. A customer can release an unarrived booking, but only admin can check in or check out a vehicle.

The local owner can reset a forgotten customer password with `python server.py --reset-customer customer@example.com` in the project terminal. Use the customer's actual email and enter the replacement password twice. This signs out all sessions for that customer and preserves bookings. See `README.md` for the account upgrade and testing steps.


## Block spaces for maintenance

In the admin page, open **Manage spaces**. Select **Block space**, enter a short reason customers can see (up to 160 characters), then confirm. Blocked spaces show the reason on the customer map and are excluded from available counts. Use **Reopen space** when work is complete. Blocks persist after restarting the server.

A reserved space or a checked-in vehicle cannot be blocked, including overdue vehicles. Wait for an unarrived booking to expire, or record the actual vehicle departure. Blocking does not cancel or erase bookings. If another admin changes the same space, review the latest status before retrying. Customer pages refresh every five seconds; the server also prevents bookings made from an outdated page.


## Database backup and restore

In admin, open **Database backup & restore** and click **Download database backup**. The `.sqlite3` file includes bookings, extension charges, customer accounts and password hashes, admin credentials, check-in/check-out history, prices, and maintenance blocks. This is a database backup, not the Excel report or the source-code ZIP. Keep it private and copy it to another drive. The download requires an active admin login and includes committed bookings even while the website is running.

You can also run `python server.py --backup` in a second VS Code terminal. It saves a uniquely named file under `data/backups/` without stopping the server or overwriting older backups. These backups are manual; they are not scheduled automatically.

To restore:

1. Stop the server with **Ctrl+C**. Close any database editor using the parking database.
2. Run `python server.py --restore "FULL_PATH_TO_BACKUP.sqlite3"`, replacing the example path with the downloaded file's full path.
3. Check the backup date and booking/customer counts. Type **RESTORE** to continue, or press Enter to cancel.
4. The app saves the current database under `data/backups/before-restore-...sqlite3` before replacing it. If validation or this safety backup fails, the restore does not proceed. A missing database can also be restored.
5. Run `python server.py`, refresh the website, and sign in again. The passwords, prices, blocks and bookings are those saved in the backup. Newer records are no longer in the active database, but remain in the safety backup when one was created. Check actual vehicle occupancy after restoring an older snapshot.

Restore accepts backups created by this version of the app, validates their structure and records, and clears saved sign-in sessions. It does not run through a website upload. The new server prevents restore or a second server from opening the same database concurrently. A `.lock` file remains next to the database; the operating system releases the lock when the server exits, so do not delete it to force a restore. Stop the server before using either password-reset command as well.

Only restore backups you created and trust. Keep the current source files separately: the database backup does not contain the website code.


## Cash and UPI payment records

In admin, select **Payments** beside any booking. Enter the whole-rupee amount you have already received and choose **Cash** or **UPI**, then select **Record payment received**. Verify UPI receipt in your bank/payment app first. This feature records collections; it does not process payments or check bank transactions.

Bookings show **Unpaid**, **Part paid**, or **Paid**, plus the paid amount and remaining balance. Part payments and mixed Cash/UPI are supported. Extending a paid booking creates a new balance. The server rejects amounts above the balance, duplicate retries, and stale submissions from another admin window.

If an entry is incorrect, choose **Correct entry**, enter a reason, and confirm. The original entry stays in history. This corrects a record only and does not issue a refund. Cancellation, release and checkout do not remove payment records or automatically refund money.

Daily reports show gross booked charges separately from **Recorded collections**. Collections use the entry time in IST, even if the booking is from an earlier day. A correction is deducted on the correction date; it does not rewrite an older day's report. Cash, UPI and total collection values are net of those corrections and can be negative. The Excel download adds a **Payments** sheet and collection totals to **Summary**. Customer receipts still show booking charges, not proof of payment.

Database backups now include payment entries and corrections. Backups from before payment tracking are still supported; restoring one starts with no payment entries. Keep a current backup before any restore.


## Payment reminder at checkout

Click **Check out** after a vehicle leaves. The dialog loads the latest booked total, amount paid, and remaining balance. Choose **Record payment** to enter Cash/UPI already received, then **Continue to checkout** to review the updated balance and confirm departure. Saving a payment alone does not check the vehicle out.

If money is still owed, tick **I confirm checkout with ... still unpaid**, then choose **Check out with balance**. The amount remains outstanding in the booking, receipt, and payment history; checkout does not mark it paid or waive it. Fully paid bookings need only the normal departure confirmation.

If a payment, correction, or extension changes the totals before confirmation, checkout is rejected. Open **Check out** again to review the current balance. **Go back** makes no changes. A balance-loading error keeps checkout disabled; use **Reload balance** after reconnecting. No database migration is needed for this update.


## Outstanding payments

Open **Outstanding payments** in admin to see every booking with a remaining balance, including checked-out vehicles and older records outside the dashboard's recent history. The total pending amount and booking count cover all matching bookings; the list shows 50 at a time, oldest first. Use **Previous** and **Next** for older/newer pages.

Cancelled, released and expired bookings are included because this app retains their recorded charges. Booking status is shown on each row. No payment is waived or collected automatically. **Record payment** opens the existing Cash/UPI form. A part payment reduces the balance; full payment removes that booking from the list. Correcting a payment can bring it back. The list refreshes after recording payment and every five seconds while open (except while an action dialog is open); **Refresh balances** reloads it manually. No database migration is needed.


### Search outstanding payments

In **Outstanding payments**, enter part of a vehicle number, driver name or booking ID. Select **Unpaid**, **Part paid**, or both, and optionally **Checked-out vehicles only**, then click **Apply filters** (or press Enter in the search box). Searches ignore letter case; spaces and hyphens in vehicle numbers and booking IDs are optional. Search terms are literal text, not wildcards.

The pending total and count cover all matching records across every results page, including older bookings. Applying or clearing filters starts at the first page. **Clear filters** restores all outstanding bookings. Applied filters remain active during refresh and payment recording; a booking disappears if it no longer matches after a payment. Filters reset when signing out.


### Download outstanding payments as Excel

In **Outstanding payments**, apply the search and filters you want, then select **Download Excel (.xlsx)**. The file includes **all matching bookings across every page**, the applied filters, snapshot time in IST, matching booking count and pending total. Unsaved changes in the filter form are not used until you click **Apply filters**. Downloading uses the latest saved balances, which can differ from an older screen view.

The workbook includes numeric dates and rupee amounts, calculated remaining balances and totals, frozen headings and Excel column filters. An empty result produces a valid workbook with zero totals and a clear empty-result message. The print layout uses A3 landscape, one page wide with as many pages tall as needed and repeated column headings. Choose your printer's paper size if needed. It is a snapshot: edits in Excel do not change website bookings or payments. Opening or downloading the report requires an admin session. Keep the new `outstanding-template.xlsx` file beside `server.py`; no additional Python packages are required.
## Waive part or all of a balance

Open a booking's **Payments**, then choose **Waive an amount**. Enter a positive whole-rupee **Amount to waive**, up to the current unpaid balance. Review the preview showing how much will remain unpaid, enter an admin-only reason, and confirm. The amount starts blank so you choose it explicitly. Typing a number in the reason does not change the amount. A waiver does not collect money, refund a payment, cancel a booking, or release a parking space.

For example, waiving ₹160 from an unpaid ₹400 booking leaves ₹240 unpaid. You can also waive the whole balance: a ₹200 booking with ₹80 received can have its remaining ₹120 waived. The receipt then shows ₹80 paid, ₹120 waived and ₹0 remaining. It is marked **Settled**; a fully waived booking with no payment is marked **Waived**. A booking with a remaining balance is **Part paid** if money has been received, otherwise **Unpaid**. Only actual Cash/UPI entries count toward recorded collections. Gross booked charges stay unchanged.

Waiver history keeps each amount, date and reason. To undo a mistake, choose **Reverse waiver** beside that entry and give a reason; only that entry's amount becomes owed again and the original entry stays in history marked **waiver reversed**. To replace it with a smaller waiver, reverse the incorrect entry first, then create a new one with the correct amount. Customer receipts show the waived total, but not private staff reasons.

Outstanding balances and their Excel downloads subtract both payments and active waivers. A settled booking disappears from outstanding payments. A later extension creates a new amount owed. Backups include waiver history, and older backups remain supported with no waiver entries.

Restart the server after updating. It adds the waiver-history table automatically and preserves existing bookings and payments. Refresh admin and customer pages with Ctrl+F5.

