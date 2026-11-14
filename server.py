"""Local parking backend. Python 3.10+; only standard-library dependencies."""
import argparse
import csv
import io
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, closing
import hashlib
import getpass
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, parse_qs
from excel_report import report_xlsx, outstanding_xlsx
from backups import BackupError, DatabaseLock, create_backup, download_backup, restore_database, unique_name

ROOT = Path(__file__).resolve().parent
RATES = {"truck": {"hour": 80, "day": 600}, "bus": {"hour": 100, "day": 800}}
REPORT_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
COOKIE = "parking_session"
SESSION_SECONDS = 365 * 86400
ADMIN_COOKIE = "parking_admin_session"
ADMIN_SECONDS = 8 * 3600
PASSWORD_ROUNDS = 600000
CUSTOMER_SECONDS = 30 * 86400
REGISTRATION_CODES = set('AN AP AR AS BR CG CH DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ NL OD OR PB PY RJ SK TN TR TS TG UA UK UP WB'.split())
PLATE_ERROR = 'Include the letter series shown on your vehicle, e.g. AB in WB 55 AB 5577. Registrations without a letter series are not supported for new bookings.'


def normalize_plate(value):
    """Format check for common permanent registrations; not a VAHAN lookup."""
    if not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r'[A-Za-z0-9 -]+', value):
        raise ApiError(400, PLATE_ERROR)
    compact = re.sub(r'[ -]', '', value).upper()
    if re.fullmatch(r'[0-9]{2}BH[0-9]{4}[A-Z]{1,2}', compact):
        raise ApiError(400, 'BH-series is for non-transport vehicles. Enter the regular registration shown on your truck or bus.')
    match = re.fullmatch(r'([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{1,4})', compact)
    if not match:
        raise ApiError(400, PLATE_ERROR)
    state, district, series, number = match.groups()
    if state not in REGISTRATION_CODES or int(district) == 0 or int(number) == 0:
        raise ApiError(400, PLATE_ERROR)
    return ' '.join(part for part in (state, district.zfill(2), series, number.zfill(4)) if part)


def plate_key(value):
    # Existing records are left intact, but equivalent valid registrations still match.
    try:
        return normalize_plate(value).replace(' ', '')
    except ApiError:
        return re.sub(r'[ -]', '', value).upper()


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS spaces (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('truck','bus'))
                );
                CREATE TABLE IF NOT EXISTS space_blocks (
                    space_id TEXT PRIMARY KEY REFERENCES spaces(id),
                    blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
                    reason TEXT NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL,
                    customer_id TEXT REFERENCES customers(id)
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id),
                    space_id TEXT NOT NULL REFERENCES spaces(id),
                    driver TEXT NOT NULL, plate TEXT NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL,
                    total INTEGER NOT NULL CHECK(total >= 0),
                    released INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT NOT NULL, request_body TEXT NOT NULL,
                    checked_in_at INTEGER, checked_out_at INTEGER, cancelled_at INTEGER,
                    customer_id TEXT REFERENCES customers(id),
                    UNIQUE(owner, request_id), CHECK(end > start)
                );
                CREATE INDEX IF NOT EXISTS bookings_space_time ON bookings(space_id, released, end);
                CREATE INDEX IF NOT EXISTS bookings_owner ON bookings(owner);
                CREATE TABLE IF NOT EXISTS admin_credentials (
                    id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    locked_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL,
                    authenticated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS admin_events (
                    id INTEGER PRIMARY KEY, booking_id TEXT NOT NULL REFERENCES bookings(id),
                    action TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS price_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    truck_hour INTEGER NOT NULL CHECK(truck_hour BETWEEN 1 AND 100000),
                    truck_day INTEGER NOT NULL CHECK(truck_day BETWEEN 1 AND 100000),
                    bus_hour INTEGER NOT NULL CHECK(bus_hour BETWEEN 1 AND 100000),
                    bus_day INTEGER NOT NULL CHECK(bus_day BETWEEN 1 AND 100000)
                );
                CREATE TABLE IF NOT EXISTS booking_extensions (
                    id INTEGER PRIMARY KEY, booking_id TEXT NOT NULL REFERENCES bookings(id),
                    request_id TEXT NOT NULL, request_body TEXT NOT NULL,
                    unit TEXT NOT NULL CHECK(unit IN ('hour','day')), duration INTEGER NOT NULL,
                    rate INTEGER NOT NULL, amount INTEGER NOT NULL,
                    previous_end INTEGER NOT NULL, new_end INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    UNIQUE(booking_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY, booking_id TEXT NOT NULL REFERENCES bookings(id),
                    amount INTEGER NOT NULL CHECK(amount>0), method TEXT NOT NULL CHECK(method IN ('cash','upi')),
                    created_at INTEGER NOT NULL, request_id TEXT NOT NULL,
                    voided_at INTEGER, void_reason TEXT NOT NULL DEFAULT '', UNIQUE(booking_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS payments_booking ON payments(booking_id);
                CREATE TABLE IF NOT EXISTS waivers (
                    id INTEGER PRIMARY KEY, booking_id TEXT NOT NULL REFERENCES bookings(id),
                    amount INTEGER NOT NULL CHECK(amount>0), reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL, request_id TEXT NOT NULL,
                    voided_at INTEGER, void_reason TEXT NOT NULL DEFAULT '', UNIQUE(booking_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS waivers_booking ON waivers(booking_id);
            """)
            db.execute("INSERT OR IGNORE INTO price_settings VALUES (1,1,?,?,?,?)",
                       (RATES['truck']['hour'], RATES['truck']['day'], RATES['bus']['hour'], RATES['bus']['day']))
            columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)")}
            missing = [name for name in ("checked_in_at", "checked_out_at", "cancelled_at") if name not in columns]
            if missing:
                # Preserve the old database before the first additive schema upgrade.
                backup_path = self.path.with_name(self.path.stem + ".before-admin.sqlite3")
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as backup:
                        db.backup(backup)
                db.execute("BEGIN IMMEDIATE")
                for name in missing:
                    db.execute(f"ALTER TABLE bookings ADD COLUMN {name} INTEGER")
                db.commit()
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            booking_columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)")}
            if "customer_id" not in session_columns or "customer_id" not in booking_columns:
                backup_path = self.path.with_name(self.path.stem + ".before-accounts.sqlite3")
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as backup:
                        db.backup(backup)
                db.execute("BEGIN IMMEDIATE")
                for table, columns in [("sessions", session_columns), ("bookings", booking_columns)]:
                    if "customer_id" not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN customer_id TEXT REFERENCES customers(id)")
                db.commit()
            db.execute("CREATE INDEX IF NOT EXISTS bookings_customer ON bookings(customer_id)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS customer_requests ON bookings(customer_id,request_id) WHERE customer_id IS NOT NULL")
            db.executemany("INSERT OR IGNORE INTO spaces VALUES (?, ?)",
                           [(f"{prefix}{i:02d}", kind) for prefix, kind in [("T", "truck"), ("B", "bus")] for i in range(1, 6)])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.create_function('parking_plate_key', 1, plate_key, deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def session(self, token, create=False):
        owner = hashlib.sha256(token.encode()).hexdigest() if token else ""
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (owner, int(time.time()))).fetchone()
            if row:
                return dict(row), None
            if not create:
                raise ApiError(401, "Your browser session expired. Refresh the page and try again.")
            token = secrets.token_urlsafe(32)
            row = {"id": hashlib.sha256(token.encode()).hexdigest(), "csrf": secrets.token_urlsafe(32),
                   "expires": int(time.time()) + SESSION_SECONDS}
            row["customer_id"] = None
            db.execute("INSERT INTO sessions(id,csrf,expires,customer_id) VALUES (:id,:csrf,:expires,:customer_id)", row)
            return row, token

    @staticmethod
    def customer_view(row):
        return {"id": row["id"], "name": row["name"], "email": row["email"]} if row else None

    @staticmethod
    def ownership(session):
        if session.get("customer_id"):
            return "customer_id=?", (session["customer_id"],)
        return "customer_id IS NULL AND owner=?", (session["id"],)

    @staticmethod
    def valid_email(email):
        if not isinstance(email, str) or len(email) > 254:
            raise ApiError(400, "Enter a valid email address.")
        email = email.strip().lower()
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", email):
            raise ApiError(400, "Enter a valid email address.")
        return email

    def _rotate_customer_session(self, db, session, customer_id):
        current = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (session["id"], int(time.time()))).fetchone()
        if not current:
            raise ApiError(401, "Your session changed. Refresh the page and try again.")
        if current["customer_id"] is not None:
            raise ApiError(409, "Sign out before using a different customer account.")
        # Claim only bookings from this browser session; never by email or driver name.
        guest_rows = db.execute("SELECT id,request_id FROM bookings WHERE owner=? AND customer_id IS NULL", (session["id"],)).fetchall()
        for row in guest_rows:
            request_id = row["request_id"]
            while db.execute("SELECT 1 FROM bookings WHERE (customer_id=? OR owner=?) AND request_id=? AND id<>?",
                             (customer_id, session["id"], request_id, row["id"])).fetchone():
                request_id = "claimed-" + secrets.token_hex(16)
            db.execute("UPDATE bookings SET customer_id=?,request_id=? WHERE id=?", (customer_id, request_id, row["id"]))
        db.execute("UPDATE sessions SET expires=0 WHERE id=?", (session["id"],))
        token = secrets.token_urlsafe(32)
        new_session = {"id": hashlib.sha256(token.encode()).hexdigest(), "csrf": secrets.token_urlsafe(32),
                       "expires": int(time.time()) + CUSTOMER_SECONDS, "customer_id": customer_id}
        db.execute("INSERT INTO sessions(id,csrf,expires,customer_id) VALUES (:id,:csrf,:expires,:customer_id)", new_session)
        return new_session, token

    def customer_auth(self, session, data, signup=False):
        if not isinstance(data, dict):
            raise ApiError(400, "Enter your account details.")
        email = self.valid_email(data.get("email"))
        password = data.get("password")
        if not isinstance(password, str) or not (10 if signup else 1) <= len(password) <= 128:
            raise ApiError(400, "Use a password with 10–128 characters." if signup else "Enter your password.")
        name = data.get("name", "")
        if signup and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name)):
            raise ApiError(400, "Enter your name (maximum 60 characters).")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
            if signup:
                if existing:
                    raise ApiError(409, "An account already uses this email. Please sign in.")
                salt = secrets.token_hex(16)
                digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
                customer_id = secrets.token_hex(16)
                db.execute("INSERT INTO customers(id,name,email,salt,password_hash,created_at) VALUES (?,?,?,?,?,?)",
                           (customer_id, name.strip(), email, salt, digest, int(time.time())))
            else:
                now = int(time.time())
                if existing and existing["locked_until"] > now:
                    raise ApiError(429, "Too many attempts. Wait one minute before trying again.")
                salt = existing["salt"] if existing else "00" * 16
                digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
                if not existing or not secrets.compare_digest(digest, existing["password_hash"]):
                    if existing:
                        failures = (0 if existing["locked_until"] else existing["failures"]) + 1
                        db.execute("UPDATE customers SET failures=?,locked_until=? WHERE id=?",
                                   (failures, now + 60 if failures >= 5 else 0, existing["id"]))
                        db.commit()
                    raise ApiError(401, "Email or password is incorrect.")
                customer_id = existing["id"]
                db.execute("UPDATE customers SET failures=0,locked_until=0 WHERE id=?", (customer_id,))
            new_session, token = self._rotate_customer_session(db, session, customer_id)
            customer = self.customer_view(db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())
            db.commit()
            return {"customer": customer, "csrfToken": new_session["csrf"]}, token

    def customer_logout(self, session):
        with self.connect() as db:
            db.execute("UPDATE sessions SET expires=0 WHERE id=?", (session["id"],))
        return self.session(None, create=True)

    def reset_customer_password(self, email, password):
        email = self.valid_email(email)
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ApiError(400, "Choose a password with 10–128 characters.")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM customers WHERE email=?", (email,)).fetchone()
            if not row:
                raise ApiError(404, "No customer account uses that email.")
            db.execute("UPDATE customers SET salt=?,password_hash=?,failures=0,locked_until=0 WHERE id=?", (salt, digest, row["id"]))
            db.execute("UPDATE sessions SET expires=0 WHERE customer_id=?", (row["id"],))
            db.commit()

    @staticmethod
    def prices(db):
        row = db.execute('SELECT * FROM price_settings WHERE id=1').fetchone()
        return {'rates': {kind: {unit: row[f'{kind}_{unit}'] for unit in ('hour', 'day')}
                          for kind in ('truck', 'bus')}, 'priceRevision': row['revision']}

    def save_prices(self, session, data):
        if not isinstance(data, dict) or type(data.get('priceRevision')) is not int:
            raise ApiError(400, 'Reload the current prices before saving.')
        rates = data.get('rates')
        if not isinstance(rates, dict) or set(rates) != {'truck', 'bus'}:
            raise ApiError(400, 'Enter all four prices.')
        values = []
        for kind in ('truck', 'bus'):
            if not isinstance(rates[kind], dict) or set(rates[kind]) != {'hour', 'day'}:
                raise ApiError(400, 'Enter all four prices.')
            for unit in ('hour', 'day'):
                value = rates[kind][unit]
                if type(value) is not int or not 1 <= value <= 100000:
                    raise ApiError(400, 'Prices must be whole rupees from 1 to 100,000.')
                values.append(value)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM admin_sessions WHERE id=? AND authenticated=1 AND expires>?',
                              (session['id'], int(time.time()))).fetchone():
                raise ApiError(401, 'Please sign in as admin again.')
            if self.prices(db)['priceRevision'] != data['priceRevision']:
                raise ApiError(409, 'Prices changed in another admin window. Reload current prices, then edit again.')
            db.execute('UPDATE price_settings SET truck_hour=?,truck_day=?,bus_hour=?,bus_day=?,revision=revision+1 WHERE id=1', values)
            result = self.prices(db)
            db.commit()
            return result

    @staticmethod
    def booking(row):
        return {"id": row["id"], "spaceId": row["space_id"], "driver": row["driver"], "plate": row["plate"],
                "start": row["start"], "end": row["end"], "total": row["total"],
                "checkedInAt": row.get("checked_in_at") if isinstance(row, dict) else row["checked_in_at"]}

    def state(self, session, search=''):
        if not isinstance(search, str) or len(search) > 80:
            raise ApiError(400, 'Search must be 80 characters or fewer.')
        now = int(time.time() * 1000)
        clause, parameters = self.ownership(session)
        needle = re.sub(r'[ -]', '', search.strip()).upper()
        search_clause = ''
        search_parameters = ()
        if needle:
            search_clause = " AND (instr(upper(replace(replace(plate,' ',''),'-','')),?)>0 OR instr(upper(replace(id,'-','')),?)>0 OR instr(upper(space_id),?)>0)"
            search_parameters = (needle, needle, needle)
        with self.connect() as db:
            db.execute("BEGIN")
            spaces = [dict(row) for row in db.execute("""
                SELECT s.id, s.type, COALESCE(m.blocked,0) AS blocked,
                COALESCE(m.reason,'') AS blockReason, EXISTS(SELECT 1 FROM bookings b
                WHERE b.space_id=s.id AND b.released=0 AND (b.end>? OR b.checked_in_at IS NOT NULL)) AS occupied
                FROM spaces s LEFT JOIN space_blocks m ON m.space_id=s.id ORDER BY s.type DESC, s.id
            """, (now,))]
            select_with_balance = """SELECT b.*, b.total
                - COALESCE((SELECT SUM(amount) FROM payments WHERE booking_id=b.id AND voided_at IS NULL), 0)
                - COALESCE((SELECT SUM(amount) FROM waivers WHERE booking_id=b.id AND voided_at IS NULL), 0)
                AS balance FROM bookings b"""
            bookings = [{**self.booking(row), "balance": row["balance"]} for row in db.execute(
                f"{select_with_balance} WHERE {clause} AND released=0 AND (end>? OR checked_in_at IS NOT NULL){search_clause} ORDER BY start DESC",
                (*parameters, now, *search_parameters))]
            history = [{**self.booking(row), "balance": row["balance"], "status": self.booking_status(row, now)} for row in db.execute(
                f"{select_with_balance} WHERE {clause} AND (released=1 OR (end<=? AND checked_in_at IS NULL)){search_clause} ORDER BY start DESC, id DESC LIMIT 100",
                (*parameters, now, *search_parameters))]
            history_total = db.execute(f"SELECT count(*) FROM bookings WHERE {clause} AND (released=1 OR (end<=? AND checked_in_at IS NULL))", (*parameters, now)).fetchone()[0]
            active_total = db.execute(f"SELECT count(*) FROM bookings WHERE {clause} AND released=0 AND (end>? OR checked_in_at IS NOT NULL)", (*parameters, now)).fetchone()[0]
            history_matches = db.execute(f"SELECT count(*) FROM bookings WHERE {clause} AND (released=1 OR (end<=? AND checked_in_at IS NULL)){search_clause}", (*parameters, now, *search_parameters)).fetchone()[0]
            customer = self.customer_view(db.execute("SELECT * FROM customers WHERE id=?", (session.get("customer_id"),)).fetchone())
            prices = self.prices(db)
            db.commit()
        return {"spaces": spaces, "bookings": bookings, "history": history, "historyTotal": history_total,
                "bookingsTotal": active_total, "historyMatches": history_matches,
                "customer": customer, **prices, "serverTime": now, "csrfToken": session["csrf"]}

    def receipt(self, session, booking_id):
        clause, parameters = self.ownership(session)
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute(f"SELECT * FROM bookings WHERE id=? AND {clause}", (booking_id, *parameters)).fetchone()
            if not row:
                raise ApiError(404, "Receipt not found in your bookings. Sign in with the account that made this booking.")
            vehicle = db.execute("SELECT type FROM spaces WHERE id=?", (row["space_id"],)).fetchone()[0]
            extensions = [dict(item) for item in db.execute(
                'SELECT unit,duration,rate,amount,previous_end,new_end,created_at FROM booking_extensions WHERE booking_id=? ORDER BY id', (booking_id,))]
            methods = {item['method']: item['amount'] for item in db.execute(
                'SELECT method,SUM(amount) AS amount FROM payments WHERE booking_id=? AND voided_at IS NULL GROUP BY method', (booking_id,))}
            waived = self.waived_total(db, booking_id)
            db.commit()
        # Use the original booking terms and stored total, never today's prices.
        original = json.loads(row["request_body"])
        unit, duration = original[1], original[2]
        original_total = row['total'] - sum(item['amount'] for item in extensions)
        charges = [{'label': 'Original booking', 'unit': unit, 'duration': duration,
                    'rate': original_total / duration, 'amount': original_total}]
        charges.extend({'label': f'Extension {i}', 'unit': item['unit'], 'duration': item['duration'],
                        'rate': item['rate'], 'amount': item['amount'], 'end': item['new_end'],
                        'createdAt': item['created_at']} for i, item in enumerate(extensions, 1))
        now = int(time.time() * 1000)
        paid = sum(methods.values())
        return {"booking": {**self.booking(row), "type": vehicle, "unit": unit, "duration": duration,
                            "rate": original_total / duration, "charges": charges, "status": self.booking_status(row, now),
                            "checkedOutAt": row["checked_out_at"]},
                "payment": {"paid": paid, "waived": waived, "balance": row['total'] - paid - waived,
                            "status": self.payment_status(row['total'], paid, waived),
                            "cash": methods.get('cash', 0), "upi": methods.get('upi', 0)}, "generatedAt": now}

    def extend_booking(self, owner, booking_id, data, confirm=False):
        if not isinstance(data, dict) or data.get('unit') not in ('hour', 'day'):
            raise ApiError(400, 'Choose hours or days to add.')
        unit, duration = data['unit'], data.get('duration')
        if type(duration) is not int or not 1 <= duration <= (24 if unit == 'hour' else 30):
            raise ApiError(400, 'Add 1–24 whole hours or 1–30 whole days at a time.')
        fingerprint = None
        if confirm:
            if not isinstance(data.get('requestId'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', data['requestId']):
                raise ApiError(400, 'A valid extension request ID is required.')
            if any(type(data.get(key)) is not int for key in ('expectedEnd', 'expectedTotal', 'expectedRate')):
                raise ApiError(400, 'Preview the extension price before confirming.')
            fingerprint = json.dumps([unit, duration, data['expectedEnd'], data['expectedTotal'], data['expectedRate']])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE' if confirm else 'BEGIN')
            session = db.execute('SELECT * FROM sessions WHERE id=? AND expires>?', (owner, int(time.time()))).fetchone()
            if not session or session['customer_id'] is None:
                raise ApiError(401, 'Sign in to extend your booking.')
            clause, parameters = self.ownership(dict(session))
            row = db.execute(f'SELECT * FROM bookings WHERE id=? AND {clause}', (booking_id, *parameters)).fetchone()
            if not row:
                raise ApiError(404, 'Booking not found in your account.')
            if confirm:
                previous = db.execute('SELECT * FROM booking_extensions WHERE booking_id=? AND request_id=?',
                                      (booking_id, data['requestId'])).fetchone()
                if previous:
                    if previous['request_body'] != fingerprint:
                        raise ApiError(409, 'This extension request ID was already used with different details.')
                    db.commit()
                    return {'booking': self.booking(row), 'addedAmount': previous['amount'], 'newEnd': previous['new_end']}
            now = int(time.time() * 1000)
            if row['released'] or (row['end'] <= now and row['checked_in_at'] is None):
                raise ApiError(409, 'This booking has ended. Create a new booking for an available space.')
            new_end = row['end'] + duration * (3600000 if unit == 'hour' else 86400000)
            if new_end <= now:
                raise ApiError(409, 'This vehicle is overdue. Add enough time to bring the new end time into the future.')
            kind = db.execute('SELECT type FROM spaces WHERE id=?', (row['space_id'],)).fetchone()[0]
            rate = self.prices(db)['rates'][kind][unit]
            amount = duration * rate
            quote = {'unit': unit, 'duration': duration, 'rate': rate, 'addedAmount': amount,
                     'previousEnd': row['end'], 'newEnd': new_end, 'previousTotal': row['total'],
                     'newTotal': row['total'] + amount}
            if not confirm:
                db.commit()
                return {'quote': quote}
            if (data['expectedEnd'], data['expectedTotal'], data['expectedRate']) != (row['end'], row['total'], rate):
                raise ApiError(409, 'The booking or price changed. Preview the updated extension and confirm again.')
            db.execute('INSERT INTO booking_extensions(booking_id,request_id,request_body,unit,duration,rate,amount,previous_end,new_end,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (booking_id, data['requestId'], fingerprint, unit, duration, rate, amount, row['end'], new_end, now))
            db.execute('UPDATE bookings SET end=?,total=? WHERE id=?', (new_end, quote['newTotal'], booking_id))
            updated = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
            db.commit()
            return {'booking': self.booking(updated), 'addedAmount': amount, 'newEnd': new_end}

    def book(self, owner, data, require_account=False):
        if not isinstance(data, dict):
            raise ApiError(400, "Send a booking object.")
        space_id, unit, duration = data.get("spaceId"), data.get("unit"), data.get("duration")
        if not isinstance(space_id, str) or unit not in ("hour", "day"):
            raise ApiError(400, "Choose a valid space and hourly or daily billing.")
        if type(duration) is not int or not 1 <= duration <= (24 if unit == "hour" else 30):
            raise ApiError(400, "Duration must be 1–24 whole hours or 1–30 whole days.")
        values = {}
        for field, limit in [("driver", 60), ("plate", 20)]:
            value = data.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or any(ord(c) < 32 for c in value):
                raise ApiError(400, f"Enter a valid {field} (maximum {limit} characters).")
            values[field] = value.strip()
        values["plate"] = normalize_plate(values["plate"])
        request_id = data.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
            raise ApiError(400, "A valid booking request ID is required.")
        fingerprint = json.dumps([space_id, unit, duration, values["driver"], values["plate"]])
        with self.connect() as db:
            # Serialize the availability check and write, including across server processes.
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (owner, int(time.time()))).fetchone()
            if not session or (require_account and session["customer_id"] is None):
                raise ApiError(401, "Sign in or create a customer account to book a space.")
            customer_id = session["customer_id"]
            clause, parameters = self.ownership(dict(session))
            previous = db.execute(f"SELECT * FROM bookings WHERE {clause} AND request_id=?", (*parameters, request_id)).fetchone()
            if previous:
                original = json.loads(previous["request_body"])
                original[4] = normalize_plate(original[4])
                if json.dumps(original) != fingerprint:
                    raise ApiError(409, "This request ID was already used for different booking details.")
                db.commit()
                return self.booking(previous), False
            space = db.execute("SELECT * FROM spaces WHERE id=?", (space_id,)).fetchone()
            if not space:
                raise ApiError(400, "That parking space does not exist.")
            if db.execute("SELECT 1 FROM space_blocks WHERE space_id=? AND blocked=1", (space_id,)).fetchone():
                raise ApiError(409, "That space is temporarily blocked. Please choose another available space.")
            rate = self.prices(db)['rates'][space['type']][unit]
            if 'expectedRate' in data and (type(data['expectedRate']) is not int or data['expectedRate'] != rate):
                raise ApiError(409, 'The price has changed. Review the updated total, then book again.')
            now = int(time.time() * 1000)
            if db.execute("SELECT 1 FROM bookings WHERE space_id=? AND released=0 AND (end>? OR checked_in_at IS NOT NULL)", (space_id, now)).fetchone():
                raise ApiError(409, "That space was just booked. Please choose another available space.")
            if db.execute("SELECT 1 FROM bookings WHERE parking_plate_key(plate)=? AND released=0 AND (end>? OR checked_in_at IS NOT NULL)",
                          (plate_key(values["plate"]), now)).fetchone():
                raise ApiError(409, 'This vehicle already has an active booking. Release its unarrived booking or ask the admin to check it out before booking again.')
            booking = {"id": "PK-" + secrets.token_hex(8).upper(), "owner": owner, "space_id": space_id,
                       **values, "start": now, "end": now + duration * (3600000 if unit == "hour" else 86400000),
                       "total": duration * rate, "request_id": request_id, "request_body": fingerprint, "customer_id": customer_id}
            db.execute("""INSERT INTO bookings
                (id,owner,space_id,driver,plate,start,end,total,request_id,request_body,customer_id)
                VALUES (:id,:owner,:space_id,:driver,:plate,:start,:end,:total,:request_id,:request_body,:customer_id)""", booking)
            db.commit()
            return self.booking(booking), True

    def release(self, owner, booking_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (owner, int(time.time()))).fetchone()
            if not session:
                raise ApiError(401, "Your session changed. Refresh and sign in again.")
            clause, parameters = self.ownership(dict(session))
            if booking_id is None:
                db.execute(f"UPDATE bookings SET released=1 WHERE {clause} AND released=0 AND checked_in_at IS NULL", parameters)
            else:
                row = db.execute(f"SELECT * FROM bookings WHERE id=? AND {clause}", (booking_id, *parameters)).fetchone()
                if row and row["checked_in_at"] is not None and not row["released"]:
                    raise ApiError(409, "This vehicle is checked in. Ask the admin to record its departure.")
                result = db.execute(f"UPDATE bookings SET released=1 WHERE id=? AND {clause}", (booking_id, *parameters))
                if result.rowcount != 1:
                    raise ApiError(404, "This booking was not found in your browser session.")
            db.commit()

    def admin_configured(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM admin_credentials WHERE id=1").fetchone() is not None

    def set_admin_password(self, password):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ApiError(400, "Choose a password with 10–128 characters.")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO admin_credentials VALUES (1,?,?,0,0)", (salt, digest))
            db.execute("DELETE FROM admin_sessions")
            db.commit()

    def admin_session(self, token, create=False):
        identifier = hashlib.sha256(token.encode()).hexdigest() if token else ""
        with self.connect() as db:
            row = db.execute("SELECT * FROM admin_sessions WHERE id=? AND expires>?", (identifier, int(time.time()))).fetchone()
            if row:
                return dict(row), None
            if not create:
                raise ApiError(401, "Please sign in to the admin page again.")
            token = secrets.token_urlsafe(32)
            row = {"id": hashlib.sha256(token.encode()).hexdigest(), "csrf": secrets.token_urlsafe(32),
                   "expires": int(time.time()) + ADMIN_SECONDS, "authenticated": 0}
            db.execute("INSERT INTO admin_sessions VALUES (:id,:csrf,:expires,:authenticated)", row)
            return row, token

    def admin_login(self, session, password):
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise ApiError(400, "Enter your admin password.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            config = db.execute("SELECT * FROM admin_credentials WHERE id=1").fetchone()
            if not config:
                raise ApiError(503, "Set your password in VS Code first: python server.py --set-admin")
            now = int(time.time())
            if config["locked_until"] > now:
                raise ApiError(429, "Too many attempts. Wait one minute before trying again.")
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(config["salt"]), PASSWORD_ROUNDS).hex()
            if not secrets.compare_digest(digest, config["password_hash"]):
                failures = (0 if config["locked_until"] else config["failures"]) + 1
                db.execute("UPDATE admin_credentials SET failures=?,locked_until=? WHERE id=1",
                           (failures, now + 60 if failures >= 5 else 0))
                db.commit()
                raise ApiError(401, "Incorrect admin password.")
            token = secrets.token_urlsafe(32)
            row = {"id": hashlib.sha256(token.encode()).hexdigest(), "csrf": secrets.token_urlsafe(32),
                   "expires": now + ADMIN_SECONDS, "authenticated": 1}
            db.execute("DELETE FROM admin_sessions WHERE id=?", (session["id"],))
            db.execute("INSERT INTO admin_sessions VALUES (:id,:csrf,:expires,:authenticated)", row)
            db.execute("UPDATE admin_credentials SET failures=0,locked_until=0 WHERE id=1")
            db.commit()
            return row, token

    def admin_logout(self, session):
        with self.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE id=?", (session["id"],))

    @staticmethod
    def booking_status(row, now):
        if row["checked_out_at"] is not None:
            return "departed"
        if row["cancelled_at"] is not None:
            return "cancelled"
        if row["released"]:
            return "released"
        if row["checked_in_at"] is not None:
            return "overdue" if row["end"] <= now else "parked"
        return "expired" if row["end"] <= now else "reserved"

    def admin_state(self):
        now = int(time.time() * 1000)
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute("""SELECT b.*, s.type,
                COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.booking_id=b.id AND p.voided_at IS NULL),0) AS paid,
                COALESCE((SELECT SUM(w.amount) FROM waivers w WHERE w.booking_id=b.id AND w.voided_at IS NULL),0) AS waived
                FROM bookings b JOIN spaces s ON s.id=b.space_id
                ORDER BY CASE WHEN b.released=0 AND (b.end>? OR b.checked_in_at IS NOT NULL) THEN 0 ELSE 1 END,
                b.start DESC LIMIT 500""", (now,)).fetchall()
            total = db.execute("SELECT count(*) FROM bookings").fetchone()[0]
            prices = self.prices(db)
            spaces = [dict(row) for row in db.execute("""SELECT s.id, s.type,
                COALESCE(m.blocked,0) AS blocked, COALESCE(m.reason,'') AS blockReason,
                COALESCE(m.revision,0) AS blockRevision,
                EXISTS(SELECT 1 FROM bookings b WHERE b.space_id=s.id AND b.released=0
                AND (b.end>? OR b.checked_in_at IS NOT NULL)) AS occupied FROM spaces s
                LEFT JOIN space_blocks m ON m.space_id=s.id ORDER BY s.type DESC,s.id""", (now,))]
            db.commit()
        bookings = [{**self.booking(row), "type": row["type"], "status": self.booking_status(row, now),
                     "paid": row['paid'], "waived": row['waived'], "balance": row['total']-row['paid']-row['waived'],
                     "paymentStatus": self.payment_status(row['total'], row['paid'], row['waived']),
                     "checkedOutAt": row["checked_out_at"], "cancelledAt": row["cancelled_at"]} for row in rows]
        return {"bookings": bookings, "spaces": spaces, "totalRecords": total, "serverTime": now, **prices}

    def outstanding_payments(self, offset=0, search="", payment="all", status="all", export=False):
        if type(offset) is not int or not 0 <= offset <= 1000000000:
            raise ApiError(400, 'Choose a valid outstanding payments page.')
        if not isinstance(search, str) or len(search)>100 or re.search(r'[\x00-\x1f\x7f\ud800-\udfff]', search):
            raise ApiError(400, 'Search must contain up to 100 characters without control characters.')
        if payment not in ('all', 'unpaid', 'partial') or status not in ('all', 'departed'):
            raise ApiError(400, 'Choose valid payment and vehicle status filters.')
        search=search.strip()
        where=' WHERE total>paid+waived'
        parameters=[]
        if payment=='unpaid':where+=' AND paid=0'
        elif payment=='partial':where+=' AND paid>0'
        if status=='departed':where+=' AND checked_out_at IS NOT NULL'
        if search:
            conditions=['instr(casefold(driver),?)>0'];parameters.append(search.casefold())
            compact=re.sub(r'[ -]', '', search).upper()
            if compact:
                conditions.extend(["instr(upper(replace(replace(plate,' ',''),'-','')),?)>0", "instr(upper(replace(id,'-','')),?)>0"])
                parameters.extend([compact,compact])
            where+=' AND ('+' OR '.join(conditions)+')'
        now = int(time.time() * 1000)
        query = """WITH balances AS (
            SELECT b.*,s.type,COALESCE(p.paid,0) AS paid,COALESCE(w.waived,0) AS waived FROM bookings b JOIN spaces s ON s.id=b.space_id
            LEFT JOIN (SELECT booking_id,SUM(amount) AS paid FROM payments WHERE voided_at IS NULL GROUP BY booking_id) p ON p.booking_id=b.id
            LEFT JOIN (SELECT booking_id,SUM(amount) AS waived FROM waivers WHERE voided_at IS NULL GROUP BY booking_id) w ON w.booking_id=b.id
        ) """
        with self.connect() as db:
            db.execute('BEGIN')
            db.create_function('casefold', 1, lambda value: value.casefold(), deterministic=True)
            summary = db.execute(query + 'SELECT count(*) AS count,COALESCE(SUM(total-paid-waived),0) AS balance FROM balances'+where, parameters).fetchone()
            if export and summary['count'] > 1048564:
                raise ApiError(413, 'Too many matching bookings for Excel. Apply narrower filters and try again.')
            offset = 0 if export else min(offset, ((summary['count']-1)//50)*50) if summary['count'] else 0
            paging = '' if export else ' LIMIT 50 OFFSET ?'
            rows = db.execute(query + 'SELECT * FROM balances'+where+' ORDER BY start,id'+paging,
                              parameters if export else (*parameters,offset)).fetchall()
            db.commit()
        return {'bookings': [{**self.booking(row), 'type':row['type'], 'status':self.booking_status(row,now),
                             'paid':row['paid'], 'waived':row['waived'], 'balance':row['total']-row['paid']-row['waived'],
                             'paymentStatus':'Part paid' if row['paid'] else 'Unpaid'} for row in rows],
                'count':summary['count'], 'balance':summary['balance'], 'offset':offset, 'limit':50, 'serverTime':now,
                'filters':{'search':search, 'payment':payment, 'status':status}}

    def payment_details(self, booking_id):
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT id,total,plate,space_id FROM bookings WHERE id=?',(booking_id,)).fetchone()
            if not row:
                raise ApiError(404,'Booking not found.')
            entries = [dict(p) for p in db.execute('SELECT id,amount,method,created_at,voided_at,void_reason FROM payments WHERE booking_id=? ORDER BY created_at,id',(booking_id,))]
            waivers = [dict(w) for w in db.execute('SELECT id,amount,reason,created_at,voided_at,void_reason FROM waivers WHERE booking_id=? ORDER BY created_at,id',(booking_id,))]
            db.commit()
        paid = sum(p['amount'] for p in entries if p['voided_at'] is None)
        waived = sum(w['amount'] for w in waivers if w['voided_at'] is None)
        return {'bookingId':booking_id,'total':row['total'],'paid':paid,'waived':waived,'balance':row['total']-paid-waived,
                'status':self.payment_status(row['total'],paid,waived),
                'plate':row['plate'],'spaceId':row['space_id'],'entries':entries,'waivers':waivers}

    @staticmethod
    def waived_total(db, booking_id):
        return db.execute('SELECT COALESCE(SUM(amount),0) FROM waivers WHERE booking_id=? AND voided_at IS NULL',(booking_id,)).fetchone()[0]

    @staticmethod
    def payment_status(total, paid, waived):
        if total == paid + waived:
            return ('Settled' if paid else 'Waived') if waived else 'Paid'
        return 'Part paid' if paid else 'Unpaid'

    def record_waiver(self, booking_id, data):
        reason=data.get('reason') if isinstance(data,dict) else None
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=160 or re.search(r'[\x00-\x1f\x7f\ud800-\udfff]',reason):
            raise ApiError(400,'Enter a waiver reason of 1–160 characters on one line.')
        request_id=data.get('requestId')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_id):
            raise ApiError(400,'Reload the waiver form before trying again.')
        if any(type(data.get(key)) is not int for key in ('expectedTotal','expectedPaid','expectedWaived')):
            raise ApiError(400,'Reload the booking before waiving the balance.')
        amount=data.get('amount')
        if type(amount) is not int or amount<=0:
            raise ApiError(400,'Enter a positive whole-rupee amount to waive.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT * FROM waivers WHERE booking_id=? AND request_id=?',(booking_id,request_id)).fetchone()
            if previous:
                if (previous['amount'],previous['reason'])!=(amount,reason.strip()):
                    raise ApiError(409,'This waiver request was already used. Reload the balance.')
                db.commit();return {'ok':True,'waiverId':previous['id']}
            booking=db.execute('SELECT total FROM bookings WHERE id=?',(booking_id,)).fetchone()
            if not booking:
                raise ApiError(404,'Booking not found.')
            paid=db.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE booking_id=? AND voided_at IS NULL',(booking_id,)).fetchone()[0]
            waived=self.waived_total(db,booking_id)
            if (data['expectedTotal'],data['expectedPaid'],data['expectedWaived'])!=(booking['total'],paid,waived):
                raise ApiError(409,'The balance changed. Reload and review it before waiving.')
            if amount>booking['total']-paid-waived:
                raise ApiError(409,'The waiver amount exceeds the remaining balance. Reload and review the amount.')
            cursor=db.execute('INSERT INTO waivers(booking_id,amount,reason,created_at,request_id) VALUES (?,?,?,?,?)',
                              (booking_id,amount,reason.strip(),int(time.time()*1000),request_id))
            identifier=cursor.lastrowid;db.commit()
        return {'ok':True,'waiverId':identifier}

    def reverse_waiver(self, booking_id, waiver_id, data):
        reason=data.get('reason') if isinstance(data,dict) else None
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=160 or re.search(r'[\x00-\x1f\x7f\ud800-\udfff]',reason):
            raise ApiError(400,'Enter a reversal reason of 1–160 characters on one line.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM waivers WHERE id=? AND booking_id=?',(waiver_id,booking_id)).fetchone()
            if not row:
                raise ApiError(404,'Waiver entry not found.')
            if row['voided_at'] is not None:
                if row['void_reason']!=reason.strip():
                    raise ApiError(409,'This waiver was already reversed. Reload the balance.')
            else:
                db.execute('UPDATE waivers SET voided_at=?,void_reason=? WHERE id=?',(int(time.time()*1000),reason.strip(),waiver_id))
            db.commit()
        return {'ok':True}

    def record_payment(self, booking_id, data):
        if not isinstance(data,dict) or type(data.get('amount')) is not int or data['amount']<=0 or data.get('method') not in ('cash','upi'):
            raise ApiError(400,'Enter a positive whole-rupee amount and choose Cash or UPI.')
        request_id=data.get('requestId')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_id):
            raise ApiError(400,'Refresh the payment form before trying again.')
        if any(type(data.get(key)) is not int for key in ('expectedTotal','expectedPaid')) or type(data.get('expectedWaived',0)) is not int:
            raise ApiError(400,'Reload the booking before recording payment.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT * FROM payments WHERE booking_id=? AND request_id=?',(booking_id,request_id)).fetchone()
            if previous:
                if (previous['amount'],previous['method']) != (data['amount'],data['method']):
                    raise ApiError(409,'This payment request was already used. Reload the payment form.')
                db.commit();return {'ok':True,'paymentId':previous['id']}
            booking=db.execute('SELECT total FROM bookings WHERE id=?',(booking_id,)).fetchone()
            if not booking:
                raise ApiError(404,'Booking not found.')
            paid=db.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE booking_id=? AND voided_at IS NULL',(booking_id,)).fetchone()[0]
            waived=self.waived_total(db,booking_id)
            if data['expectedTotal']!=booking['total'] or data['expectedPaid']!=paid or data.get('expectedWaived',0)!=waived:
                raise ApiError(409,'The booking or payments changed. Reload payments and review the balance.')
            if data['amount']>booking['total']-paid-waived:
                raise ApiError(409,'The amount exceeds the remaining booking balance.')
            cursor=db.execute('INSERT INTO payments(booking_id,amount,method,created_at,request_id) VALUES (?,?,?,?,?)',
                (booking_id,data['amount'],data['method'],int(time.time()*1000),request_id))
            identifier=cursor.lastrowid;db.commit()
        return {'ok':True,'paymentId':identifier}

    def void_payment(self, booking_id, payment_id, data):
        reason=data.get('reason') if isinstance(data,dict) else None
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=160 or re.search(r'[\x00-\x1f\x7f\ud800-\udfff]',reason):
            raise ApiError(400,'Enter a correction reason of 1–160 characters on one line.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM payments WHERE id=? AND booking_id=?',(payment_id,booking_id)).fetchone()
            if not row:
                raise ApiError(404,'Payment entry not found.')
            if row['voided_at'] is not None:
                if row['void_reason']!=reason.strip():
                    raise ApiError(409,'This entry was already corrected. Reload payments.')
            else:
                db.execute('UPDATE payments SET voided_at=?,void_reason=? WHERE id=?',(int(time.time()*1000),reason.strip(),payment_id))
            db.commit()
        return {'ok':True}

    def set_space_block(self, space_id, data):
        if not isinstance(data, dict) or type(data.get('blocked')) is not bool:
            raise ApiError(400, "Choose whether to block or reopen the space.")
        revision, reason = data.get('blockRevision'), data.get('reason', '')
        if type(revision) is not int or revision < 0:
            raise ApiError(400, "Refresh the space list before trying again.")
        if not isinstance(reason, str) or len(reason) > 160 or re.search(r'[\x00-\x1f\x7f\ud800-\udfff]', reason):
            raise ApiError(400, "Use a reason of up to 160 characters on one line.")
        reason = reason.strip() if data['blocked'] else ''
        if data['blocked'] and not reason:
            raise ApiError(400, "Enter a reason customers can see, such as Ground repair.")
        with self.connect() as db:
            # The same write lock used by booking prevents booking/blocking races.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM spaces WHERE id=?", (space_id,)).fetchone():
                raise ApiError(404, "That parking space does not exist.")
            current = db.execute("SELECT * FROM space_blocks WHERE space_id=?", (space_id,)).fetchone()
            if revision != (current['revision'] if current else 0):
                raise ApiError(409, "This space changed in another admin window. Review its latest status and try again.")
            now = int(time.time() * 1000)
            if data['blocked'] and db.execute("""SELECT 1 FROM bookings WHERE space_id=? AND released=0
                    AND (end>? OR checked_in_at IS NOT NULL)""", (space_id, now)).fetchone():
                raise ApiError(409, "This space has an active booking. Wait until the booking ends or the vehicle is checked out.")
            db.execute("""INSERT INTO space_blocks(space_id,blocked,reason,revision) VALUES (?,?,?,?)
                ON CONFLICT(space_id) DO UPDATE SET blocked=excluded.blocked,
                reason=excluded.reason,revision=excluded.revision""",
                (space_id, int(data['blocked']), reason, revision + 1))
            db.commit()
        return {'ok': True}

    def admin_action(self, booking_id, action, data=None):
        now = int(time.time() * 1000)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if not row:
                raise ApiError(404, "Booking not found.")
            status = self.booking_status(row, now)
            if (action == "check-in" and status in ("parked", "overdue")) or (action == "check-out" and status == "departed") or (action == "cancel" and status == "cancelled"):
                db.commit()
                return
            allowed = {"check-in": ("reserved",), "check-out": ("parked", "overdue"), "cancel": ("reserved",)}
            if action not in allowed or status not in allowed[action]:
                raise ApiError(409, "Booking status changed. Refresh and choose an available action.")
            if action == "check-in":
                db.execute("UPDATE bookings SET checked_in_at=? WHERE id=?", (now, booking_id))
            elif action == "check-out":
                if not isinstance(data, dict) or any(type(data.get(key)) is not int for key in ('expectedTotal', 'expectedPaid')) or type(data.get('expectedWaived',0)) is not int:
                    raise ApiError(400, 'Open Check out and review the latest payment balance before confirming.')
                paid = db.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE booking_id=? AND voided_at IS NULL', (booking_id,)).fetchone()[0]
                waived=self.waived_total(db,booking_id)
                if data['expectedTotal'] != row['total'] or data['expectedPaid'] != paid or data.get('expectedWaived',0)!=waived:
                    raise ApiError(409, 'Booking or payments changed. Open Check out again to review the latest balance.')
                if row['total'] > paid+waived and data.get('allowUnpaid') is not True:
                    raise ApiError(409, 'Payment is still pending. Record payment or explicitly confirm check-out with the remaining balance.')
                db.execute("UPDATE bookings SET checked_out_at=?,released=1 WHERE id=?", (now, booking_id))
            else:
                db.execute("UPDATE bookings SET cancelled_at=?,released=1 WHERE id=?", (now, booking_id))
            db.execute("INSERT INTO admin_events(booking_id,action,created_at) VALUES (?,?,?)", (booking_id, action, now))
            db.commit()

    def daily_report(self, day):
        if not isinstance(day, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day):
            raise ApiError(400, 'Choose a valid report date (YYYY-MM-DD).')
        try:
            start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=REPORT_TIMEZONE)
            finish = start + timedelta(days=1)
        except (ValueError, OverflowError):
            raise ApiError(400, 'Choose a valid report date (YYYY-MM-DD).')
        low, high = int(start.timestamp()*1000), int(finish.timestamp()*1000)
        now = int(time.time()*1000)
        with self.connect() as db:
            db.execute('BEGIN')
            rows = db.execute('''
                SELECT b.*, s.type FROM bookings b JOIN spaces s ON s.id=b.space_id
                WHERE (b.start>=? AND b.start<?) OR (b.checked_in_at>=? AND b.checked_in_at<?)
                   OR (b.checked_out_at>=? AND b.checked_out_at<?)
                   OR EXISTS(SELECT 1 FROM booking_extensions e WHERE e.booking_id=b.id AND e.created_at>=? AND e.created_at<?)
            ''', (low,high)*4).fetchall()
            extensions = db.execute('''SELECT e.* FROM booking_extensions e
                WHERE (e.created_at>=? AND e.created_at<?)
                   OR EXISTS(SELECT 1 FROM bookings b WHERE b.id=e.booking_id AND b.start>=? AND b.start<?)
            ''', (low,high,low,high)).fetchall()
            payment_rows=db.execute('''SELECT p.*,b.space_id,b.driver,b.plate,s.type FROM payments p
                JOIN bookings b ON b.id=p.booking_id JOIN spaces s ON s.id=b.space_id
                WHERE (p.created_at>=? AND p.created_at<?) OR (p.voided_at>=? AND p.voided_at<?)''',(low,high,low,high)).fetchall()
            db.commit()
        collections={'cashAmount':0,'upiAmount':0,'totalAmount':0}
        payments=[]
        for p in payment_rows:
            base={'id':p['id'],'bookingId':p['booking_id'],'spaceId':p['space_id'],'driver':p['driver'],'plate':p['plate'],'method':p['method']}
            for at,amount,event,note in [(p['created_at'],p['amount'],'Payment','Staff recorded receipt'),(p['voided_at'],-p['amount'],'Correction',p['void_reason'])]:
                if at is not None and low<=at<high:
                    payments.append({**base,'time':at,'amount':amount,'event':event,'note':note})
                    collections[p['method']+'Amount']+=amount
                    collections['totalAmount']+=amount
        payments.sort(key=lambda p:(p['time'],p['id'],p['event']=='Correction'))
        extra_by_booking = {}
        for item in extensions:
            extra_by_booking.setdefault(item['booking_id'], []).append(item)
        events = []
        summary = {'bookings':0, 'extensions':0, 'arrivals':0, 'departures':0, 'originalAmount':0, 'extensionAmount':0, 'bookedAmount':0}
        for row in rows:
            base = {'bookingId':row['id'], 'spaceId':row['space_id'], 'type':row['type'],
                    'driver':row['driver'], 'plate':row['plate'], 'currentStatus':self.booking_status(row, now)}
            additions = extra_by_booking.get(row['id'], [])
            if low <= row['start'] < high:
                amount = row['total'] - sum(e['amount'] for e in additions)
                summary['bookings'] += 1; summary['originalAmount'] += amount
                events.append({**base, 'event':'Booking', 'time':row['start'], 'amount':amount})
            for addition in additions:
                if low <= addition['created_at'] < high:
                    summary['extensions'] += 1; summary['extensionAmount'] += addition['amount']
                    events.append({**base, 'event':'Extension', 'time':addition['created_at'], 'amount':addition['amount']})
            for field, event, counter in [('checked_in_at','Arrival','arrivals'), ('checked_out_at','Departure','departures')]:
                if row[field] is not None and low <= row[field] < high:
                    summary[counter] += 1
                    events.append({**base, 'event':event, 'time':row[field], 'amount':None})
        summary['bookedAmount'] = summary['originalAmount'] + summary['extensionAmount']
        events.sort(key=lambda e:(e['time'], e['bookingId'], e['event']))
        return {'date':day, 'timezone':'IST (UTC+05:30)', 'generatedAt':now, 'summary':summary, 'events':events,
                'collections':collections,'payments':payments,
                'note':'Gross amounts recorded on this date, including bookings later cancelled or released. Not payments received. Status is current when the report is generated.'}

    @staticmethod
    def report_csv(report):
        def safe(value):
            # Excel must treat customer-entered names and legacy registrations as text.
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                return "'" + value
            return value
        def local_time(value):
            return datetime.fromtimestamp(value/1000, REPORT_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['Daily parking report', report['date']])
        writer.writerow(['Timezone', report['timezone']])
        writer.writerow(['Generated at (IST)', local_time(report['generatedAt'])])
        writer.writerow(['Amounts', report['note']])
        writer.writerow([])
        writer.writerow(['Summary', 'Value'])
        for label,key in [('New bookings','bookings'),('Extensions','extensions'),('Arrivals','arrivals'),('Departures','departures'),
                          ('Original booking amounts (INR)','originalAmount'),('Extension amounts (INR)','extensionAmount'),('Gross booked amount (INR; not payments)','bookedAmount')]:
            writer.writerow([label, report['summary'][key]])
        writer.writerow([])
        writer.writerow(['Event','Event time (IST)','Booking ID','Space','Vehicle type','Driver','Registration','Recorded amount (INR)','Current status'])
        for event in report['events']:
            writer.writerow([safe(value) for value in [event['event'],local_time(event['time']),event['bookingId'],event['spaceId'],event['type'],
                              event['driver'],event['plate'],event['amount'] if event['amount'] is not None else '',event['currentStatus']]])
        writer.writerow([])
        writer.writerow(['Recorded collections (net of corrections)','Amount (INR)'])
        for label,key in [('Cash','cashAmount'),('UPI','upiAmount'),('Net recorded collections','totalAmount')]:
            writer.writerow([label,report.get('collections',{}).get(key,0)])
        writer.writerow(['Entry','Time (IST)','Booking ID','Space','Method','Driver','Registration','Amount (INR)','Note'])
        for p in report.get('payments',[]):
            writer.writerow([safe(v) for v in [f"{p['event']} #{p['id']}",local_time(p['time']),p['bookingId'],p['spaceId'],p['method'].upper(),p['driver'],p['plate'],p['amount'],p['note']]])
        return output.getvalue().encode('utf-8-sig')


class Handler(BaseHTTPRequestHandler):
    server_version = "ParkingLocal/2"

    def log_message(self, fmt, *args):
        # Avoid logging request bodies, cookies, driver names or registration numbers.
        pass

    def respond(self, status, payload, content_type="application/json; charset=utf-8", token=None, admin_token=None, filename=None):
        body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if token:
            self.send_header("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}")
        if admin_token is not None:
            self.send_header("Set-Cookie", f"{ADMIN_COOKIE}={admin_token}; Path=/api/admin; HttpOnly; SameSite=Strict; Max-Age={ADMIN_SECONDS if admin_token else 0}")
        self.end_headers()
        self.wfile.write(body)

    def get_session(self, create=False, admin=False):
        cookie = SimpleCookie()
        name = ADMIN_COOKIE if admin else COOKIE
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[name].value if name in cookie else None
        except CookieError:
            token = None
        return self.server.store.admin_session(token, create) if admin else self.server.store.session(token, create)

    @staticmethod
    def require_admin(session):
        if not session["authenticated"]:
            raise ApiError(401, "Please sign in as admin.")

    def check_host(self):
        host = self.headers.get("Host", "")
        if host not in {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}:
            raise ApiError(403, "Use the localhost address printed in your terminal.")
        return host

    def dispatch(self):
        try:
            host = self.check_host()
            path = urlsplit(self.path).path
            if self.command == "GET":
                if path == "/api/admin/session":
                    session, token = self.get_session(create=True, admin=True)
                    return self.respond(200, {"configured": self.server.store.admin_configured(),
                        "authenticated": bool(session["authenticated"]), "csrfToken": session["csrf"]}, admin_token=token)
                if path in ('/api/admin/outstanding', '/api/admin/outstanding.xlsx'):
                    session, _ = self.get_session(admin=True)
                    self.require_admin(session)
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if any(len(query.get(key, []))>1 for key in ('search','payment','status')):
                        raise ApiError(400, 'Choose one value for each filter.')
                    values = query.get('offset', ['0'])
                    if len(values) != 1 or not re.fullmatch(r'[0-9]{1,10}', values[0]):
                        raise ApiError(400, 'Choose a valid outstanding payments page.')
                    report = self.server.store.outstanding_payments(int(values[0]),
                        search=query.get('search',[''])[0], payment=query.get('payment',['all'])[0],
                        status=query.get('status',['all'])[0], export=path.endswith('.xlsx'))
                    if path.endswith('.xlsx'):
                        stamp = datetime.fromtimestamp(report['serverTime']/1000, REPORT_TIMEZONE).strftime('%Y-%m-%d-%H%M%S')
                        return self.respond(200, outstanding_xlsx(report),
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=f'parking-outstanding-{stamp}.xlsx')
                    return self.respond(200, report)
                if path == "/api/admin/state":
                    session, _ = self.get_session(admin=True)
                    self.require_admin(session)
                    return self.respond(200, self.server.store.admin_state())
                if path == '/api/admin/backup':
                    session, _ = self.get_session(admin=True)
                    self.require_admin(session)
                    return self.respond(200, download_backup(self.server.store.path), 'application/vnd.sqlite3',
                                        filename=unique_name())
                match=re.fullmatch(r'/api/admin/bookings/(PK-[A-F0-9]{16})/payments',path)
                if match:
                    session,_=self.get_session(admin=True);self.require_admin(session)
                    return self.respond(200,self.server.store.payment_details(match[1]))
                if path in ('/api/admin/reports', '/api/admin/reports.csv', '/api/admin/reports.xlsx'):
                    session, _ = self.get_session(admin=True)
                    self.require_admin(session)
                    day = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('date', [''])[0]
                    report = self.server.store.daily_report(day)
                    if path.endswith('.xlsx'):
                        return self.respond(200, report_xlsx(report),
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            filename=f'parking-report-{day}.xlsx')
                    if path.endswith('.csv'):
                        return self.respond(200, self.server.store.report_csv(report), 'text/csv; charset=utf-8', filename=f'parking-report-{day}.csv')
                    return self.respond(200, report)
                if path == "/api/state":
                    session, token = self.get_session(create=True)
                    search = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('search', [''])[0]
                    return self.respond(200, self.server.store.state(session, search), token=token)
                match = re.fullmatch(r"/api/bookings/(PK-[A-F0-9]{16})/receipt", path)
                if match:
                    session, _ = self.get_session()
                    return self.respond(200, self.server.store.receipt(session, match[1]))
                static = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/index.html": ("index.html", "text/html; charset=utf-8"),
                          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/receipt": ("receipt.html", "text/html; charset=utf-8"),
                          "/receipt.html": ("receipt.html", "text/html; charset=utf-8"),
                          "/receipt.css": ("receipt.css", "text/css; charset=utf-8"),
                          "/receipt.js": ("receipt.js", "text/javascript; charset=utf-8"),
                          "/admin": ("admin.html", "text/html; charset=utf-8"),
                          "/admin.html": ("admin.html", "text/html; charset=utf-8"),
                          "/admin.css": ("admin.css", "text/css; charset=utf-8"),
                          "/admin.js": ("admin.js", "text/javascript; charset=utf-8")}
                if path not in static:
                    raise ApiError(404, "Not found.")
                filename, content_type = static[path]
                return self.respond(200, (ROOT / filename).read_bytes(), content_type)
            if self.headers.get("Origin") != f"http://{host}":
                raise ApiError(403, "Requests must come from this parking website.")
            is_admin = path.startswith("/api/admin/")
            session, _ = self.get_session(admin=is_admin)
            supplied_csrf = self.headers.get("X-CSRF-Token", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", supplied_csrf) or not secrets.compare_digest(supplied_csrf, session["csrf"]):
                raise ApiError(403, "Refresh the page before trying again.")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "Send JSON data.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "Invalid request length.")
            if not 0 < length <= 4096 or self.headers.get("Transfer-Encoding"):
                raise ApiError(413, "Request body must be between 1 and 4096 bytes.")
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise ApiError(400, "Invalid JSON.")
            if is_admin:
                if path == "/api/admin/login":
                    if not isinstance(data, dict):
                        raise ApiError(400, "Enter your admin password.")
                    session, token = self.server.store.admin_login(session, data.get("password"))
                    return self.respond(200, {"csrfToken": session["csrf"]}, admin_token=token)
                self.require_admin(session)
                match=re.fullmatch(r'/api/admin/bookings/(PK-[A-F0-9]{16})/waivers(?:/([0-9]+)/reverse)?',path)
                if match:
                    result=(self.server.store.reverse_waiver(match[1],int(match[2]),data) if match[2]
                            else self.server.store.record_waiver(match[1],data))
                    return self.respond(200,result)
                match=re.fullmatch(r'/api/admin/bookings/(PK-[A-F0-9]{16})/payments(?:/([0-9]+)/void)?',path)
                if match:
                    result=(self.server.store.void_payment(match[1],int(match[2]),data) if match[2]
                            else self.server.store.record_payment(match[1],data))
                    return self.respond(200,result)
                if path == "/api/admin/prices":
                    return self.respond(200, self.server.store.save_prices(session, data))
                match = re.fullmatch(r"/api/admin/spaces/([TB][0-9]{2})/block", path)
                if match:
                    return self.respond(200, self.server.store.set_space_block(match[1], data))
                if path == "/api/admin/logout":
                    self.server.store.admin_logout(session)
                    return self.respond(200, {"ok": True}, admin_token="")
                match = re.fullmatch(r"/api/admin/bookings/(PK-[A-F0-9]{16})/(check-in|check-out|cancel)", path)
                if match:
                    self.server.store.admin_action(match[1], match[2], data)
                    return self.respond(200, {"ok": True})
                raise ApiError(404, "Not found.")
            if path == "/api/bookings":
                booking, created = self.server.store.book(session["id"], data, require_account=True)
                return self.respond(201 if created else 200, {"booking": booking})
            match = re.fullmatch(r'/api/bookings/(PK-[A-F0-9]{16})/(extension-quote|extend)', path)
            if match:
                return self.respond(200, self.server.store.extend_booking(session['id'], match[1], data, confirm=match[2] == 'extend'))
            if path in ("/api/customer/signup", "/api/customer/login"):
                result, token = self.server.store.customer_auth(session, data, signup=path.endswith("signup"))
                return self.respond(201 if path.endswith("signup") else 200, result, token=token)
            if path == "/api/customer/logout":
                new_session, token = self.server.store.customer_logout(session)
                return self.respond(200, {"csrfToken": new_session["csrf"]}, token=token)
            if path == "/api/bookings/release-all":
                self.server.store.release(session["id"])
                return self.respond(200, {"ok": True})
            match = re.fullmatch(r"/api/bookings/(PK-[A-F0-9]{16})/release", path)
            if match:
                self.server.store.release(session["id"], match[1])
                return self.respond(200, {"ok": True})
            raise ApiError(404, "Not found.")
        except ApiError as error:
            self.respond(error.status, {"error": error.message})
        except BackupError as error:
            self.respond(400, {'error': str(error)})
        except sqlite3.OperationalError:
            self.respond(503, {"error": "Database is busy or unavailable. Please try again shortly."})
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            self.respond(500, {"error": "The server could not complete this request."})

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    do_GET = dispatch
    do_POST = dispatch


class ParkingServer(ThreadingHTTPServer):
    # Wait for active requests before releasing the database's lifetime lock.
    daemon_threads = False

    def server_close(self):
        try:
            super().server_close()
        finally:
            if hasattr(self, 'database_lock'):
                self.database_lock.close()


def make_server(port=8000, database=None):
    path = Path(database or ROOT / 'data' / 'parking.sqlite3').resolve()
    lock = DatabaseLock(path)
    try:
        store = Store(path)
        server = ParkingServer(('127.0.0.1', port), Handler)
        server.store = store
        server.database_lock = lock
        return server
    except Exception:
        lock.close()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local smart parking website and database.")
    parser.add_argument("--port", type=int, default=8000)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--set-admin", action="store_true", help="Set or reset the local admin password, then exit.")
    maintenance.add_argument("--reset-customer", metavar="EMAIL", help="Reset a customer's password locally, then exit.")
    maintenance.add_argument('--backup', action='store_true', help='Save a database backup to data/backups, then exit.')
    maintenance.add_argument('--restore', metavar='FILE', help='Validate and restore a backup while the server is stopped.')
    args = parser.parse_args()
    database = ROOT / 'data' / 'parking.sqlite3'
    if args.backup or args.restore:
        try:
            if args.backup:
                saved = create_backup(database, database.parent / 'backups' / unique_name())
                print(f'Backup saved: {saved}\nKeep this file private; it contains customer and account data.')
            else:
                def confirm_restore(summary):
                    created = datetime.fromisoformat(summary['createdAt']).astimezone(REPORT_TIMEZONE)
                    print(f"Backup made: {created:%d %b %Y, %H:%M} IST\nBookings: {summary['bookings']} | Customers: {summary['customers']}")
                    print('Restoring replaces all current parking data, prices, blocks and passwords with this backup. Newer records will be removed from the active database.')
                    print('A safety backup of the current database will be saved first. Everyone will need to sign in again.')
                    return input('Type RESTORE to continue, or press Enter to cancel: ').strip() == 'RESTORE'
                result = restore_database(args.restore, database, Store, confirm_restore)
                if result['restored']:
                    print('Restore complete. Use the passwords from the backup. Start the server with python server.py.')
                    if result['safetyBackup']:
                        print('Previous data saved at: ' + result['safetyBackup'])
                else:
                    print('Restore cancelled. Current data is unchanged.')
            parser.exit()
        except (BackupError, OSError, sqlite3.Error) as error:
            parser.exit(1, f'Backup/restore could not complete: {error}\n')
        except (EOFError, KeyboardInterrupt):
            parser.exit(1, '\nCancelled.\n')
    if args.reset_customer:
        try:
            password = getpass.getpass("New customer password (10–128 characters): ")
            if password != getpass.getpass("Repeat password: "):
                parser.exit(1, "Passwords did not match. No changes made.\n")
            with DatabaseLock(database):
                Store(database).reset_customer_password(args.reset_customer, password)
            parser.exit(0, "Customer password updated. Previous sessions are signed out.\n")
        except (ApiError, BackupError) as error:
            parser.exit(1, (error.message if isinstance(error, ApiError) else str(error)) + "\n")
    if args.set_admin:
        try:
            password = getpass.getpass("Choose admin password (10–128 characters): ")
            confirmation = getpass.getpass("Repeat password: ")
            if password != confirmation:
                parser.exit(1, "Passwords did not match. No changes made.\n")
            with DatabaseLock(database):
                Store(database).set_admin_password(password)
            print("Admin password saved. Run python server.py, then visit http://localhost:8000/admin")
            parser.exit()
        except (ApiError, BackupError) as error:
            parser.exit(1, (error.message if isinstance(error, ApiError) else str(error)) + "\n")
    try:
        server = make_server(args.port)
    except (OSError, BackupError) as error:
        parser.exit(1, f"Could not start: {error}\nStop the old server with Ctrl+C, or use --port 8001.\n")
    print(f"Smart parking: http://localhost:{server.server_port}", flush=True)
    print(f"Database: {server.store.path}\nPress Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. Bookings remain saved.")
    finally:
        server.server_close()
