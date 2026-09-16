import logging
import os
import sqlite3
import uuid
from functools import wraps
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity, jwt_required
from flask_jwt_extended import JWTManager
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
DEFAULT_LOAN_DAYS = 3
DEFAULT_LOAN_LIMIT = 3


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-me"),
        DATABASE=os.path.join(BASE_DIR, "lending_desk.sqlite3"),
        LOAN_LIMIT=DEFAULT_LOAN_LIMIT,
        AUTO_SEED=True,
        JWT_SECRET_KEY=os.environ.get("JWT_SECRET_KEY", os.environ.get("SECRET_KEY", "dev-jwt-key-change-me-please-32-bytes")),
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin123"),
        HANDLER_USERNAME=os.environ.get("HANDLER_USERNAME", "handler"),
        HANDLER_PASSWORD=os.environ.get("HANDLER_PASSWORD", "handler123"),
    )
    if test_config:
        app.config.update(test_config)
    JWTManager(app)

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    app.get_db = get_db

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db():
        db = get_db()
        with app.open_resource("schema.sql") as schema:
            db.executescript(schema.read().decode("utf-8"))
        loan_columns = {row[1] for row in db.execute("PRAGMA table_info(loans)").fetchall()}
        for column, definition in (("amount_due", "NUMERIC NOT NULL DEFAULT 0"), ("amount_paid", "NUMERIC NOT NULL DEFAULT 0")):
            if column not in loan_columns:
                db.execute(f"ALTER TABLE loans ADD COLUMN {column} {definition}")
        item_columns = {row[1] for row in db.execute("PRAGMA table_info(items)").fetchall()}
        if "image_url" not in item_columns:
            db.execute("ALTER TABLE items ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        transfer_columns = {row[1] for row in db.execute("PRAGMA table_info(loan_transfers)").fetchall()}
        for column, definition in (("status", "TEXT NOT NULL DEFAULT 'approved'"), ("requested_by_user_id", "INTEGER"), ("decided_by_user_id", "INTEGER"), ("decided_at", "TEXT"), ("decision_note", "TEXT NOT NULL DEFAULT ''")):
            if column not in transfer_columns:
                db.execute(f"ALTER TABLE loan_transfers ADD COLUMN {column} {definition}")
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "borrower_id" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN borrower_id INTEGER REFERENCES borrowers(id) ON DELETE SET NULL")
        if app.config["AUTO_SEED"] and not app.config.get("TESTING"):
            from seed import expand_demo_data, seed_database

            seed_database(db)
            expand_demo_data(db)
        if not db.execute("SELECT 1 FROM users WHERE username = ?", (app.config["ADMIN_USERNAME"],)).fetchone():
            db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                       (app.config["ADMIN_USERNAME"], generate_password_hash(app.config["ADMIN_PASSWORD"])))
        if not db.execute("SELECT 1 FROM users WHERE username = ?", (app.config["HANDLER_USERNAME"],)).fetchone():
            db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'operator')",
                       (app.config["HANDLER_USERNAME"], generate_password_hash(app.config["HANDLER_PASSWORD"])))
        db.commit()

    app.init_db = init_db

    def now():
        return datetime.now().replace(microsecond=0)

    def parse_datetime(value, default=None):
        if not value:
            return default
        return datetime.fromisoformat(value)

    def money(value):
        try:
            amount = Decimal(str(value or "0"))
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a valid non-negative amount.")
        if amount < 0:
            raise ValueError("Amounts cannot be negative.")
        return amount.quantize(Decimal("0.01"))

    def save_item_image(upload):
        if not upload or not upload.filename:
            return ""
        extension = os.path.splitext(secure_filename(upload.filename))[1].lower()
        if extension not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            raise ValueError("Use a JPG, PNG, WEBP, or GIF image.")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        upload.save(os.path.join(UPLOAD_DIR, filename))
        return url_for("static", filename=f"uploads/{filename}")

    def payment_summary(db, loan_id):
        loan = db.execute("SELECT deposit_amount, late_fee, refund_amount FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if not loan:
            return {"amount_due": Decimal("0.00"), "amount_paid": Decimal("0.00"), "remaining_amount": Decimal("0.00"), "refund_due": Decimal("0.00")}
        charge_row = db.execute("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count FROM payments WHERE loan_id = ? AND direction = 'charge' AND status = 'paid'", (loan_id,)).fetchone()
        deposit_payment = db.execute("SELECT 1 FROM payments WHERE loan_id = ? AND payment_type = 'deposit' AND direction = 'charge' AND status = 'paid' LIMIT 1", (loan_id,)).fetchone()
        refunds = db.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE loan_id = ? AND direction = 'refund' AND status = 'paid'", (loan_id,)).fetchone()["total"]
        amount_due = Decimal(str(loan["deposit_amount"] or 0)) + Decimal(str(loan["late_fee"] or 0))
        charges = Decimal(str(charge_row["total"] or 0))
        legacy_deposit = Decimal(str(loan["deposit_amount"] or 0)) if not deposit_payment and loan["deposit_amount"] else Decimal("0.00")
        amount_paid = charges + legacy_deposit
        refund_due = max(Decimal("0.00"), Decimal(str(loan["refund_amount"] or 0)) - Decimal(str(refunds or 0)))
        return {"amount_due": amount_due, "amount_paid": amount_paid, "remaining_amount": max(Decimal("0.00"), amount_due - amount_paid), "refund_due": refund_due}

    def sync_payment_totals(db, loan_id):
        summary = payment_summary(db, loan_id)
        db.execute("UPDATE loans SET amount_due = ?, amount_paid = ? WHERE id = ?", (str(summary["amount_due"]), str(summary["amount_paid"]), loan_id))
        return summary

    def finance_totals(db):
        late_fee = db.execute("SELECT COALESCE(SUM(late_fee), 0) AS total FROM loans").fetchone()["total"]
        late_fee_due = db.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE payment_type = 'late_fee' AND direction = 'charge' AND status = 'due'").fetchone()["total"]
        refund_due = Decimal("0.00")
        for loan in db.execute("SELECT id FROM loans WHERE returned_at IS NOT NULL AND refund_amount > 0").fetchall():
            refund_due += payment_summary(db, loan["id"])["refund_due"]
        return {"late_fee_total": Decimal(str(late_fee or 0)), "late_fee_due_total": Decimal(str(late_fee_due or 0)), "refund_due_total": refund_due}

    def reconcile_refund_statuses(db):
        changed = False
        loans = db.execute("SELECT id, deposit_status FROM loans WHERE returned_at IS NOT NULL AND refund_amount > 0").fetchall()
        for loan in loans:
            if loan["deposit_status"] != "fully_refunded" and payment_summary(db, loan["id"])["refund_due"] <= 0:
                db.execute("UPDATE loans SET deposit_status = 'fully_refunded' WHERE id = ?", (loan["id"],))
                changed = True
        if changed:
            db.commit()

    def late_fee_payment(db, loan_id):
        return db.execute("SELECT * FROM payments WHERE loan_id = ? AND payment_type = 'late_fee' ORDER BY id DESC LIMIT 1", (loan_id,)).fetchone()

    def json_value(value):
        if isinstance(value, Decimal):
            return f"{value:.2f}"
        return value

    def row_json(row):
        return {key: json_value(row[key]) for key in row.keys()}

    def admin_api_required():
        claims = get_jwt()
        if claims.get("role") not in {"admin", "operator"}:
            abort(403, description="Admin or handler role required")

    def operator_api_required():
        if get_jwt().get("role") not in {"admin", "operator"}:
            abort(403, description="Admin or handler role required")

    def active_overlap(db, unit_id, start_at, end_at, exclude_loan_id=None):
        query = """SELECT id FROM loans
            WHERE unit_id = ? AND returned_at IS NULL
              AND checkout_at < ? AND due_at > ?"""
        params = [unit_id, end_at.isoformat(sep=" "), start_at.isoformat(sep=" ")]
        if exclude_loan_id is not None:
            query += " AND id != ?"
            params.append(exclude_loan_id)
        return db.execute(query, params).fetchone()

    def transfer_loan(db, loan_id, borrower_id, changed_by_user_id=None):
        loan = db.execute("SELECT id, borrower_id, returned_at FROM loans WHERE id = ?", (loan_id,)).fetchone()
        borrower = db.execute("SELECT id, name FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
        if not loan or loan["returned_at"]:
            raise ValueError("Only an active loan can be transferred.")
        if not borrower:
            raise ValueError("Choose a valid borrower.")
        if loan["borrower_id"] == borrower_id:
            raise ValueError("The loan already belongs to this borrower.")
        db.execute("INSERT INTO loan_transfers (loan_id, from_borrower_id, to_borrower_id, changed_by_user_id) VALUES (?, ?, ?, ?)",
                   (loan_id, loan["borrower_id"], borrower_id, changed_by_user_id))
        db.execute("UPDATE loans SET borrower_id = ? WHERE id = ?", (borrower_id, loan_id))
        return borrower["name"]

    def request_transfer(db, loan_id, borrower_id, requested_by_user_id):
        loan = db.execute("SELECT id, borrower_id, returned_at FROM loans WHERE id = ?", (loan_id,)).fetchone()
        borrower = db.execute("SELECT id, name FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
        if not loan or loan["returned_at"]:
            raise ValueError("Only an active loan can be transferred.")
        if not borrower or loan["borrower_id"] == borrower_id:
            raise ValueError("Choose a different valid borrower.")
        pending = db.execute("SELECT id FROM loan_transfers WHERE loan_id = ? AND status = 'pending'", (loan_id,)).fetchone()
        if pending:
            raise ValueError("This loan already has a transfer waiting for approval.")
        db.execute("INSERT INTO loan_transfers (loan_id, from_borrower_id, to_borrower_id, requested_by_user_id, changed_by_user_id, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                   (loan_id, loan["borrower_id"], borrower_id, requested_by_user_id, requested_by_user_id))
        return borrower["name"]

    def latest_transfer(db, loan_id):
        return db.execute("""SELECT loan_transfers.*, from_borrower.name AS from_name,
            to_borrower.name AS to_name, users.username AS changed_by
            FROM loan_transfers
            JOIN borrowers AS from_borrower ON from_borrower.id = loan_transfers.from_borrower_id
            JOIN borrowers AS to_borrower ON to_borrower.id = loan_transfers.to_borrower_id
            LEFT JOIN users ON users.id = loan_transfers.changed_by_user_id
            WHERE loan_transfers.loan_id = ? AND loan_transfers.status = 'approved' ORDER BY loan_transfers.transferred_at DESC LIMIT 1""", (loan_id,)).fetchone()

    def pending_transfer(db, loan_id):
        return db.execute("""SELECT loan_transfers.*, from_borrower.name AS from_name,
            to_borrower.name AS to_name, users.username AS requested_by
            FROM loan_transfers
            JOIN borrowers AS from_borrower ON from_borrower.id = loan_transfers.from_borrower_id
            JOIN borrowers AS to_borrower ON to_borrower.id = loan_transfers.to_borrower_id
            LEFT JOIN users ON users.id = loan_transfers.requested_by_user_id
            WHERE loan_transfers.loan_id = ? AND loan_transfers.status = 'pending'
            ORDER BY loan_transfers.transferred_at DESC LIMIT 1""", (loan_id,)).fetchone()

    def transfer_history(db, loan_id):
        return db.execute("""SELECT loan_transfers.*, from_borrower.name AS from_name,
            to_borrower.name AS to_name, users.username AS changed_by
            FROM loan_transfers
            JOIN borrowers AS from_borrower ON from_borrower.id = loan_transfers.from_borrower_id
            JOIN borrowers AS to_borrower ON to_borrower.id = loan_transfers.to_borrower_id
            LEFT JOIN users ON users.id = loan_transfers.changed_by_user_id
            WHERE loan_transfers.loan_id = ? ORDER BY loan_transfers.transferred_at""", (loan_id,)).fetchall()

    def operator_required():
        if app.config.get("TESTING") or session.get("user_role") in {"admin", "operator"}:
            return None
        flash("Sign in as admin or handler before changing lending data.", "error")
        return redirect(url_for("login", next=request.path))

    def current_borrower(db):
        """The borrower profile linked to the signed-in account, if any.

        Once an account is linked to a borrower, checkout is locked to that
        borrower only — nobody else can be picked for that login."""
        user_id = session.get("user_id")
        if not user_id:
            return None
        return db.execute(
            "SELECT borrowers.* FROM users JOIN borrowers ON borrowers.id = users.borrower_id WHERE users.id = ?",
            (user_id,),
        ).fetchone()

    def refresh_overdue(db):
        db.execute("UPDATE loans SET status = 'overdue' WHERE returned_at IS NULL AND due_at < ?", (now().isoformat(sep=" "),))
        db.commit()

    @app.template_filter("money")
    def money_filter(value):
        return f"INR {Decimal(str(value or 0)):,.2f}"

    @app.template_filter("pretty_date")
    def pretty_date(value):
        if not value:
            return "-"
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed.strftime("%d %b %Y, %I:%M %p")

    @app.context_processor
    def inject_helpers():
        return {"today": now().date().isoformat(), "default_loan_days": DEFAULT_LOAN_DAYS, "payment_summary": lambda loan_id: payment_summary(get_db(), loan_id), "late_fee_payment": lambda loan_id: late_fee_payment(get_db(), loan_id), "latest_transfer": lambda loan_id: latest_transfer(get_db(), loan_id), "pending_transfer": lambda loan_id: pending_transfer(get_db(), loan_id), "transfer_history": lambda loan_id: transfer_history(get_db(), loan_id)}

    @app.route("/")
    def dashboard():
        db = get_db()
        refresh_overdue(db)
        overdue = db.execute("""SELECT loans.*, units.asset_tag, items.name AS item_name,
            borrowers.name AS borrower_name, borrowers.borrower_code
            FROM loans JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id
            JOIN borrowers ON borrowers.id = loans.borrower_id
            WHERE loans.returned_at IS NULL AND loans.due_at < ? ORDER BY loans.due_at""", (now().isoformat(sep=" "),)).fetchall()
        active = db.execute("""SELECT loans.*, units.asset_tag, items.name AS item_name,
            borrowers.name AS borrower_name FROM loans JOIN units ON units.id = loans.unit_id
            JOIN items ON items.id = units.item_id JOIN borrowers ON borrowers.id = loans.borrower_id
            WHERE loans.returned_at IS NULL ORDER BY loans.due_at""").fetchall()
        borrowers = db.execute("SELECT * FROM borrowers ORDER BY name").fetchall()
        current = now().isoformat(sep=" ")
        stats = db.execute("""SELECT COUNT(*) AS units,
            SUM(CASE WHEN units.status = 'available' AND NOT EXISTS (
                SELECT 1 FROM loans WHERE loans.unit_id = units.id AND loans.returned_at IS NULL
                AND loans.checkout_at <= ? AND loans.due_at > ?
            ) THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN status = 'maintenance' THEN 1 ELSE 0 END) AS maintenance FROM units""", (current, current)).fetchone()
        items = db.execute("SELECT * FROM items ORDER BY name").fetchall()
        return render_template("dashboard.html", overdue=overdue, active=active, stats=stats, items=items, borrowers=borrowers)

    @app.post("/items")
    def add_item():
        if request.method == "POST":
            denied = operator_required()
            if denied:
                return denied
        db = get_db()
        try:
            name = request.form["name"].strip()
            if not name:
                raise ValueError("Item name is required.")
            count = int(request.form.get("unit_count", 0))
            if count < 1 or count > 100:
                raise ValueError("Add between 1 and 100 units.")
            deposit = money(request.form.get("deposit_amount"))
            late_fee = money(request.form.get("late_fee_per_day"))
            image_url = request.form.get("image_url", "").strip() or save_item_image(request.files.get("image"))
            item = db.execute("INSERT INTO items (name, description, deposit_amount, late_fee_per_day, image_url) VALUES (?, ?, ?, ?, ?)",
                              (name, request.form.get("description", "").strip(), str(deposit), str(late_fee), image_url))
            item_id = item.lastrowid
            for index in range(1, count + 1):
                tag = f"{name[:3].upper().replace(' ', '')}-{item_id:03d}-{index:02d}"
                db.execute("INSERT INTO units (item_id, asset_tag, condition_note) VALUES (?, ?, ?)",
                           (item_id, tag, request.form.get("condition_note", "Good").strip() or "Good"))
            db.commit()
            flash(f"Added {name} with {count} physical unit(s).", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "That item already exists.", "error")
        return redirect(url_for("dashboard"))

    @app.post("/borrowers")
    def add_borrower():
        denied = operator_required()
        if denied:
            return denied
        db = get_db()
        try:
            new_borrower_id = db.execute("INSERT INTO borrowers (name, borrower_code, contact, club_department) VALUES (?, ?, ?, ?)",
                       (request.form["name"].strip(), request.form["borrower_code"].strip(),
                        request.form.get("contact", "").strip(), request.form.get("club_department", "").strip())).lastrowid
            user_id = session.get("user_id")
            if user_id and request.form.get("link_to_me") and current_borrower(db) is None:
                # Only link when explicitly requested, so admin/handler staff
                # adding borrowers on someone else's behalf stay unaffected.
                db.execute("UPDATE users SET borrower_id = ? WHERE id = ?", (new_borrower_id, user_id))
            db.commit()
            flash("Borrower added.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Borrower name and ID are required, and the ID must be unique.", "error")
        return redirect(url_for("checkout"))

    @app.route("/checkout", methods=["GET", "POST"])
    def checkout():
        if request.method == "POST":
            denied = operator_required()
            if denied:
                return denied
        db = get_db()
        locked_borrower = current_borrower(db)
        if request.method == "POST":
            try:
                unit_id = int(request.form["unit_id"])
                if locked_borrower is not None:
                    # Signed-in account is linked to a borrower profile: that
                    # profile is the only one this login can ever check out
                    # for, so the submitted value is ignored rather than trusted.
                    borrower_id = locked_borrower["id"]
                else:
                    borrower_id = int(request.form["borrower_id"])
                start_at = parse_datetime(request.form.get("checkout_at"), now())
                due_at = parse_datetime(request.form.get("due_at"))
                if due_at is None:
                    due_at = start_at + timedelta(days=DEFAULT_LOAN_DAYS)
                if due_at <= start_at:
                    raise ValueError("Due date must be after checkout time.")
                unit = db.execute("SELECT units.*, items.deposit_amount, items.name FROM units JOIN items ON items.id = units.item_id WHERE units.id = ?", (unit_id,)).fetchone()
                borrower = db.execute("SELECT * FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
                if not unit or not borrower:
                    raise ValueError("Choose a valid unit and borrower.")
                if unit["status"] != "available":
                    raise ValueError("That unit is not available for lending.")
                if active_overlap(db, unit_id, start_at, due_at):
                    raise ValueError("That physical unit is already booked during this period.")
                if start_at <= now():
                    current_loans = db.execute("SELECT COUNT(*) AS total FROM loans WHERE borrower_id = ? AND returned_at IS NULL AND checkout_at <= ?", (borrower_id, now().isoformat(sep=" "))).fetchone()["total"]
                    if current_loans >= app.config["LOAN_LIMIT"]:
                        raise ValueError(f"Checkout refused: {borrower['name']} already has {current_loans} active units (limit {app.config['LOAN_LIMIT']}).")
                deposit = money(request.form.get("deposit_amount", unit["deposit_amount"]))
                if not request.form.get("deposit_amount"):
                    deposit = Decimal(str(unit["deposit_amount"])).quantize(Decimal("0.01"))
                loan_insert = db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, ?)",
                           (unit_id, borrower_id, start_at.isoformat(sep=" "), due_at.isoformat(sep=" "), str(deposit), "held" if deposit else "not_applicable"))
                loan_id = loan_insert.lastrowid
                if deposit:
                    db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at, note) VALUES (?, 'deposit', 'charge', ?, 'paid', ?, 'Deposit collected at checkout')",
                               (loan_id, str(deposit), now().isoformat(sep=" ")))
                sync_payment_totals(db, loan_id)
                db.commit()
                flash(f"{unit['name']} / {unit['asset_tag']} checked out to {borrower['name']}.", "success")
                return redirect(url_for("dashboard"))
            except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
                db.rollback()
                flash(str(exc), "error")
        units = db.execute("SELECT units.*, items.name AS item_name FROM units JOIN items ON items.id = units.item_id WHERE units.status = 'available' ORDER BY items.name, units.asset_tag").fetchall()
        borrowers = db.execute("SELECT * FROM borrowers ORDER BY name").fetchall()
        return render_template("checkout.html", units=units, borrowers=borrowers, locked_borrower=locked_borrower)

    @app.post("/loans/<int:loan_id>/return")
    def return_loan(loan_id):
        denied = operator_required()
        if denied:
            return denied
        db = get_db()
        loan = db.execute("SELECT loans.*, items.late_fee_per_day, units.asset_tag FROM loans JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id WHERE loans.id = ?", (loan_id,)).fetchone()
        if not loan or loan["returned_at"]:
            flash("That loan is already returned or does not exist.", "error")
            return redirect(url_for("dashboard"))
        returned_at = now()
        due_at = datetime.fromisoformat(loan["due_at"])
        late_days = max(0, (returned_at.date() - due_at.date()).days)
        late_fee = (Decimal(str(loan["late_fee_per_day"])) * late_days).quantize(Decimal("0.01"))
        deposit = Decimal(str(loan["deposit_amount"]))
        refund = max(Decimal("0"), deposit - late_fee)
        status = "fully_refunded" if not deposit or refund == deposit else "partially_refunded"
        db.execute("UPDATE loans SET returned_at = ?, status = 'returned', late_fee = ?, refund_amount = ?, deposit_status = ?, return_condition_note = ? WHERE id = ?",
                    (returned_at.isoformat(sep=" "), str(late_fee), str(refund), status, request.form.get("condition_note", "").strip(), loan_id))
        if late_fee:
            db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, note) VALUES (?, 'late_fee', 'charge', ?, 'due', 'Late fee calculated at return')",
                       (loan_id, str(late_fee)))
        sync_payment_totals(db, loan_id)
        db.commit()
        flash(f"Returned {loan['asset_tag']}. Late fee: {money_filter(late_fee)}. Refund due: {money_filter(refund)}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/loans/<int:loan_id>/transfer")
    def transfer_web_loan(loan_id):
        denied = operator_required()
        if denied:
            return denied
        db = get_db()
        try:
            borrower_id = int(request.form["borrower_id"])
            borrower_name = request_transfer(db, loan_id, borrower_id, session.get("user_id"))
            db.commit()
            flash(f"Transfer request for {borrower_name} is waiting for admin approval. The current borrower and due date remain unchanged.", "success")
        except (ValueError, KeyError):
            db.rollback()
            flash("The active loan could not be transferred.", "error")
        return redirect(url_for("dashboard"))

    @app.post("/loans/<int:loan_id>/nudge")
    def nudge(loan_id):
        denied = operator_required()
        if denied:
            return denied
        db = get_db()
        loan = db.execute("SELECT loans.*, units.asset_tag, items.name AS item_name, borrowers.name AS borrower_name FROM loans JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id JOIN borrowers ON borrowers.id = loans.borrower_id WHERE loans.id = ? AND loans.returned_at IS NULL", (loan_id,)).fetchone()
        if loan:
            message = f"Reminder: {loan['borrower_name']}, please return {loan['item_name']} ({loan['asset_tag']}). It was due {loan['due_at']}."
            logging.getLogger("lending_desk").warning(message)
            db.execute("INSERT INTO nudge_log (loan_id, message) VALUES (?, ?)", (loan_id, message))
            db.commit()
            flash("Nudge logged and printed to the application log.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/availability")
    def availability():
        db = get_db()
        start = parse_datetime(request.args.get("start"), now())
        end = parse_datetime(request.args.get("end"), start + timedelta(days=3))
        if end <= start:
            end = start + timedelta(days=1)
        rows = db.execute("SELECT items.*, COUNT(units.id) AS total_units FROM items LEFT JOIN units ON units.item_id = items.id AND units.status = 'available' GROUP BY items.id ORDER BY items.name").fetchall()
        result = []
        for item in rows:
            booked = db.execute("SELECT COUNT(*) AS total FROM loans JOIN units ON units.id = loans.unit_id WHERE units.item_id = ? AND units.status = 'available' AND loans.returned_at IS NULL AND loans.checkout_at < ? AND loans.due_at > ?", (item["id"], end.isoformat(sep=" "), start.isoformat(sep=" "))).fetchone()["total"]
            result.append({"item": item, "total": item["total_units"], "booked": booked, "free": max(0, item["total_units"] - booked)})
        return render_template("availability.html", result=result, start=start, end=end)

    @app.route("/history")
    def history():
        db = get_db()
        loans = db.execute("""SELECT loans.*, units.asset_tag, items.name AS item_name,
            borrowers.name AS borrower_name, borrowers.borrower_code
            FROM loans JOIN units ON units.id = loans.unit_id
            JOIN items ON items.id = units.item_id
            JOIN borrowers ON borrowers.id = loans.borrower_id
            ORDER BY loans.checkout_at DESC, loans.id DESC""").fetchall()
        return render_template("history.html", loans=loans)

    @app.route("/deposits")
    def deposits():
        db = get_db()
        reconcile_refund_statuses(db)
        loans = db.execute("""SELECT loans.*, units.asset_tag, items.name AS item_name,
            borrowers.name AS borrower_name
            FROM loans JOIN units ON units.id = loans.unit_id
            JOIN items ON items.id = units.item_id
            JOIN borrowers ON borrowers.id = loans.borrower_id
            WHERE loans.deposit_amount > 0
            ORDER BY loans.returned_at IS NULL DESC, loans.due_at""").fetchall()
        return render_template("deposits.html", loans=loans, totals=finance_totals(db))

    @app.post("/api/auth/login")
    def api_login():
        data = request.get_json(silent=True) or request.form
        user = get_db().execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (data.get("username", "").strip(),)).fetchone()
        if not user or not check_password_hash(user["password_hash"], data.get("password", "")):
            return jsonify({"error": "Invalid username or password"}), 401
        token = create_access_token(identity=str(user["id"]), additional_claims={"role": user["role"], "username": user["username"]})
        return jsonify({"access_token": token, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}})

    def browser_login():
        data = request.form
        user = get_db().execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (data.get("username", "").strip(),)).fetchone()
        if not user or not check_password_hash(user["password_hash"], data.get("password", "")):
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        session["user_role"] = user["role"]
        session["username"] = user["username"]
        if user["role"] == "admin":
            session["admin_user_id"] = user["id"]
        return redirect(url_for("admin"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            return browser_login()
        return render_template("admin_login.html", handler_login=True)

    @app.post("/admin/login")
    def admin_login():
        return browser_login()

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin_user_id", None)
        session.pop("user_id", None)
        session.pop("user_role", None)
        session.pop("username", None)
        return redirect(url_for("admin"))

    @app.route("/admin")
    def admin():
        db = get_db()
        reconcile_refund_statuses(db)
        if session.get("user_role") not in {"admin", "operator"}:
            return render_template("admin_login.html")
        units = db.execute("SELECT units.*, items.name AS item_name FROM units JOIN items ON items.id = units.item_id ORDER BY items.name, units.asset_tag").fetchall()
        items = db.execute("SELECT items.*, COUNT(units.id) AS unit_count FROM items LEFT JOIN units ON units.item_id = items.id GROUP BY items.id ORDER BY items.name").fetchall()
        borrowers = db.execute("SELECT borrowers.*, COUNT(loans.id) AS loan_count FROM borrowers LEFT JOIN loans ON loans.borrower_id = borrowers.id GROUP BY borrowers.id ORDER BY borrowers.name").fetchall()
        payments = db.execute("""SELECT payments.*, loans.due_at, units.asset_tag,
            items.name AS item_name, borrowers.name AS borrower_name
            FROM payments JOIN loans ON loans.id = payments.loan_id
            JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id
            JOIN borrowers ON borrowers.id = loans.borrower_id ORDER BY payments.created_at DESC LIMIT 50""").fetchall()
        refunds = db.execute("""SELECT loans.*, units.asset_tag, items.name AS item_name,
            borrowers.name AS borrower_name
            FROM loans JOIN units ON units.id = loans.unit_id
            JOIN items ON items.id = units.item_id JOIN borrowers ON borrowers.id = loans.borrower_id
            WHERE loans.returned_at IS NOT NULL AND loans.refund_amount > 0
            ORDER BY loans.returned_at DESC""").fetchall()
        transfer_requests = db.execute("""SELECT loan_transfers.*, units.asset_tag, items.name AS item_name,
            from_borrower.name AS from_name, to_borrower.name AS to_name, users.username AS requested_by
            FROM loan_transfers JOIN loans ON loans.id = loan_transfers.loan_id
            JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id
            JOIN borrowers AS from_borrower ON from_borrower.id = loan_transfers.from_borrower_id
            JOIN borrowers AS to_borrower ON to_borrower.id = loan_transfers.to_borrower_id
            LEFT JOIN users ON users.id = loan_transfers.requested_by_user_id
            WHERE loan_transfers.status = 'pending' ORDER BY loan_transfers.transferred_at""").fetchall()
        return render_template("admin.html", units=units, items=items, borrowers=borrowers, payments=payments, refunds=refunds, transfer_requests=transfer_requests, totals=finance_totals(db))

    @app.post("/admin/units/<int:unit_id>")
    def admin_update_unit(unit_id):
        if session.get("user_role") not in {"admin", "operator"}:
            return redirect(url_for("admin"))
        status = request.form.get("status")
        if status not in {"available", "maintenance", "retired"}:
            flash("Invalid unit status.", "error")
        else:
            db = get_db()
            db.execute("UPDATE units SET status = ?, condition_note = ? WHERE id = ?", (status, request.form.get("condition_note", "").strip() or "Good", unit_id))
            db.commit()
            flash("Unit updated.", "success")
        return redirect(url_for("admin"))

    @app.post("/admin/transfers/<int:transfer_id>/<action>")
    def admin_decide_transfer(transfer_id, action):
        if session.get("user_role") != "admin":
            return redirect(url_for("admin"))
        if action not in {"approve", "reject"}:
            flash("Invalid transfer decision.", "error")
            return redirect(url_for("admin"))
        db = get_db()
        transfer = db.execute("SELECT * FROM loan_transfers WHERE id = ? AND status = 'pending'", (transfer_id,)).fetchone()
        if not transfer:
            flash("Transfer request is no longer pending.", "error")
            return redirect(url_for("admin"))
        status = "approved" if action == "approve" else "rejected"
        if status == "approved":
            db.execute("UPDATE loans SET borrower_id = ? WHERE id = ? AND returned_at IS NULL", (transfer["to_borrower_id"], transfer["loan_id"]))
        db.execute("UPDATE loan_transfers SET status = ?, decided_by_user_id = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                   (status, session["user_id"], now().isoformat(sep=" "), request.form.get("note", "").strip(), transfer_id))
        db.commit()
        flash(f"Transfer request {status}.", "success")
        return redirect(url_for("admin"))

    @app.post("/admin/items/<int:item_id>/delete")
    def admin_delete_item(item_id):
        if session.get("user_role") not in {"admin", "operator"}:
            return redirect(url_for("admin"))
        db = get_db()
        try:
            item = db.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
            if not item:
                raise ValueError("Item was not found.")
            db.execute("DELETE FROM items WHERE id = ?", (item_id,))
            db.commit()
            flash(f"Removed {item['name']} from inventory.", "success")
        except (ValueError, sqlite3.IntegrityError):
            db.rollback()
            flash("This item cannot be removed while it has units or loan history. Retire its units instead.", "error")
        return redirect(url_for("admin"))

    @app.post("/admin/borrowers/<int:borrower_id>/delete")
    def admin_delete_borrower(borrower_id):
        if session.get("user_role") not in {"admin", "operator"}:
            return redirect(url_for("admin"))
        db = get_db()
        try:
            borrower = db.execute("SELECT name FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
            if not borrower:
                raise ValueError("Borrower was not found.")
            db.execute("DELETE FROM borrowers WHERE id = ?", (borrower_id,))
            db.commit()
            flash(f"Removed {borrower['name']} from borrowers.", "success")
        except (ValueError, sqlite3.IntegrityError):
            db.rollback()
            flash("This borrower cannot be removed while loan history exists.", "error")
        return redirect(url_for("admin"))

    @app.post("/admin/loans/<int:loan_id>/refund")
    def admin_record_refund(loan_id):
        if session.get("user_role") not in {"admin", "operator"}:
            return redirect(url_for("admin"))
        db = get_db()
        loan = db.execute("SELECT id, returned_at, refund_amount FROM loans WHERE id = ?", (loan_id,)).fetchone()
        try:
            if not loan or not loan["returned_at"]:
                raise ValueError("A refund can only be recorded after the unit is returned.")
            amount = money(request.form.get("amount"))
            balance = payment_summary(db, loan_id)["refund_due"]
            if amount <= 0 or amount > balance:
                raise ValueError(f"Refund must be between INR 0.01 and INR {balance:.2f}.")
            db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at, note) VALUES (?, 'refund', 'refund', ?, 'paid', ?, ?)",
                       (loan_id, str(amount), now().isoformat(sep=" "), request.form.get("note", "Admin refund settlement").strip()))
            remaining = payment_summary(db, loan_id)["refund_due"]
            status = "fully_refunded" if remaining <= 0 else "partially_refunded"
            db.execute("UPDATE loans SET deposit_status = ? WHERE id = ?", (status, loan_id))
            db.commit()
            flash(f"Recorded {money_filter(amount)} refund. Deposit is now {status.replace('_', ' ')}.", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("admin"))

    @app.post("/admin/payments/<int:payment_id>/settle")
    def admin_settle_late_fee(payment_id):
        if session.get("user_role") not in {"admin", "operator"}:
            return redirect(url_for("admin"))
        db = get_db()
        payment = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not payment or payment["payment_type"] != "late_fee" or payment["status"] != "due":
            flash("Only a due late-fee payment can be settled.", "error")
            return redirect(url_for("admin"))
        db.execute("UPDATE payments SET status = 'paid', paid_at = ?, note = ? WHERE id = ?",
                   (now().isoformat(sep=" "), request.form.get("note", "Late fee settled by staff").strip(), payment_id))
        loan = db.execute("SELECT deposit_status FROM loans WHERE id = ?", (payment["loan_id"],)).fetchone()
        if loan and loan["deposit_status"] == "partially_refunded":
            db.execute("UPDATE loans SET deposit_status = 'fully_refunded' WHERE id = ?", (payment["loan_id"],))
        sync_payment_totals(db, payment["loan_id"])
        db.commit()
        flash(f"Settled {money_filter(payment['amount'])} late fee.", "success")
        return redirect(url_for("admin"))

    @app.errorhandler(403)
    def forbidden(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), 403
        return error.description, 403

    @app.get("/api/items")
    @jwt_required()
    def api_items():
        rows = get_db().execute("SELECT * FROM items ORDER BY name").fetchall()
        return jsonify([row_json(row) for row in rows])

    @app.post("/api/items")
    @jwt_required()
    def api_create_item():
        admin_api_required()
        data = request.get_json() or {}
        db = get_db()
        try:
            name = data["name"].strip()
            count = int(data.get("unit_count", 1))
            if not name or count < 1 or count > 100:
                raise ValueError("name and a unit_count from 1 to 100 are required")
            item_id = db.execute("INSERT INTO items (name, description, deposit_amount, late_fee_per_day, image_url) VALUES (?, ?, ?, ?, ?)", (name, data.get("description", ""), str(money(data.get("deposit_amount"))), str(money(data.get("late_fee_per_day"))), data.get("image_url", ""))).lastrowid
            for index in range(1, count + 1):
                db.execute("INSERT INTO units (item_id, asset_tag, condition_note) VALUES (?, ?, ?)", (item_id, f"{name[:3].upper()}-{item_id:03d}-{index:02d}", data.get("condition_note", "Good")))
            db.commit()
            return jsonify({"id": item_id}), 201
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/items/<int:item_id>", methods=["GET", "PUT", "DELETE"])
    @jwt_required()
    def api_item(item_id):
        db = get_db()
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404
        if request.method == "GET":
            return jsonify(row_json(item))
        admin_api_required()
        if request.method == "DELETE":
            try:
                db.execute("DELETE FROM items WHERE id = ?", (item_id,))
                db.commit()
                return "", 204
            except sqlite3.IntegrityError:
                db.rollback()
                return jsonify({"error": "Cannot delete an item with units or loan history"}), 409
        data = request.get_json() or {}
        db.execute("UPDATE items SET name = ?, description = ?, deposit_amount = ?, late_fee_per_day = ?, image_url = ? WHERE id = ?", (data.get("name", item["name"]), data.get("description", item["description"]), str(money(data.get("deposit_amount", item["deposit_amount"]))), str(money(data.get("late_fee_per_day", item["late_fee_per_day"]))), data.get("image_url", item["image_url"]), item_id))
        db.commit()
        return jsonify(row_json(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()))

    @app.get("/api/units")
    @jwt_required()
    def api_units():
        rows = get_db().execute("SELECT units.*, items.name AS item_name FROM units JOIN items ON items.id = units.item_id ORDER BY items.name, units.asset_tag").fetchall()
        return jsonify([row_json(row) for row in rows])

    @app.route("/api/units/<int:unit_id>", methods=["GET", "PUT", "DELETE"])
    @jwt_required()
    def api_unit(unit_id):
        admin_api_required() if request.method != "GET" else None
        db = get_db()
        unit = db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        if not unit:
            return jsonify({"error": "Unit not found"}), 404
        if request.method == "GET":
            return jsonify(row_json(unit))
        if request.method == "DELETE":
            try:
                db.execute("DELETE FROM units WHERE id = ?", (unit_id,))
                db.commit()
                return "", 204
            except sqlite3.IntegrityError:
                db.rollback()
                return jsonify({"error": "Cannot delete a unit with loan history"}), 409
        data = request.get_json() or {}
        status = data.get("status", unit["status"])
        if status not in {"available", "maintenance", "retired"}:
            return jsonify({"error": "Invalid unit status"}), 400
        db.execute("UPDATE units SET status = ?, condition_note = ? WHERE id = ?", (status, data.get("condition_note", unit["condition_note"]), unit_id))
        db.commit()
        return jsonify(row_json(db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()))

    @app.route("/api/borrowers", methods=["GET", "POST"])
    @jwt_required()
    def api_borrowers():
        db = get_db()
        if request.method == "GET":
            return jsonify([row_json(row) for row in db.execute("SELECT * FROM borrowers ORDER BY name").fetchall()])
        admin_api_required()
        data = request.get_json() or {}
        try:
            borrower_id = db.execute("INSERT INTO borrowers (name, borrower_code, contact, club_department) VALUES (?, ?, ?, ?)", (data["name"].strip(), data["borrower_code"].strip(), data.get("contact", ""), data.get("club_department", ""))).lastrowid
            db.commit()
            return jsonify({"id": borrower_id}), 201
        except (KeyError, sqlite3.IntegrityError) as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/borrowers/<int:borrower_id>", methods=["GET", "PUT", "DELETE"])
    @jwt_required()
    def api_borrower(borrower_id):
        db = get_db()
        borrower = db.execute("SELECT * FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
        if not borrower:
            return jsonify({"error": "Borrower not found"}), 404
        if request.method == "GET":
            return jsonify(row_json(borrower))
        admin_api_required()
        if request.method == "DELETE":
            try:
                db.execute("DELETE FROM borrowers WHERE id = ?", (borrower_id,))
                db.commit()
                return "", 204
            except sqlite3.IntegrityError:
                db.rollback()
                return jsonify({"error": "Cannot delete a borrower with loan history"}), 409
        data = request.get_json() or {}
        db.execute("UPDATE borrowers SET name = ?, borrower_code = ?, contact = ?, club_department = ? WHERE id = ?", (data.get("name", borrower["name"]), data.get("borrower_code", borrower["borrower_code"]), data.get("contact", borrower["contact"]), data.get("club_department", borrower["club_department"]), borrower_id))
        db.commit()
        return jsonify(row_json(db.execute("SELECT * FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()))

    @app.route("/api/loans", methods=["GET", "POST"])
    @jwt_required()
    def api_loans():
        db = get_db()
        rows = db.execute("SELECT loans.*, units.asset_tag, items.name AS item_name, borrowers.name AS borrower_name FROM loans JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id JOIN borrowers ON borrowers.id = loans.borrower_id ORDER BY loans.checkout_at DESC").fetchall()
        if request.method == "POST":
            operator_api_required()
            data = request.get_json() or {}
            try:
                unit_id = int(data["unit_id"])
                borrower_id = int(data["borrower_id"])
                start_at = parse_datetime(data.get("checkout_at"), now())
                due_at = parse_datetime(data.get("due_at"), start_at + timedelta(days=DEFAULT_LOAN_DAYS))
                if due_at <= start_at or active_overlap(db, unit_id, start_at, due_at):
                    raise ValueError("Invalid dates or the unit is already booked")
                unit = db.execute("SELECT units.*, items.deposit_amount FROM units JOIN items ON items.id = units.item_id WHERE units.id = ?", (unit_id,)).fetchone()
                if not unit or unit["status"] != "available" or not db.execute("SELECT 1 FROM borrowers WHERE id = ?", (borrower_id,)).fetchone():
                    raise ValueError("Choose a valid available unit and borrower")
                deposit = money(data.get("deposit_amount", unit["deposit_amount"]))
                loan_id = db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, ?)", (unit_id, borrower_id, start_at.isoformat(sep=" "), due_at.isoformat(sep=" "), str(deposit), "held" if deposit else "not_applicable")).lastrowid
                if deposit:
                    db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at, note) VALUES (?, 'deposit', 'charge', ?, 'paid', ?, 'Deposit collected through API')", (loan_id, str(deposit), now().isoformat(sep=" ")))
                sync_payment_totals(db, loan_id)
                db.commit()
                return jsonify({"id": loan_id}), 201
            except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
                db.rollback()
                return jsonify({"error": str(exc)}), 400
        result = []
        for row in rows:
            data = row_json(row)
            data.update({key: json_value(value) for key, value in payment_summary(db, row["id"]).items()})
            result.append(data)
        return jsonify(result)

    @app.route("/api/loans/<int:loan_id>", methods=["GET", "PUT", "DELETE"])
    @jwt_required()
    def api_loan(loan_id):
        db = get_db()
        loan = db.execute("SELECT loans.*, units.asset_tag, items.name AS item_name, borrowers.name AS borrower_name FROM loans JOIN units ON units.id = loans.unit_id JOIN items ON items.id = units.item_id JOIN borrowers ON borrowers.id = loans.borrower_id WHERE loans.id = ?", (loan_id,)).fetchone()
        if not loan:
            return jsonify({"error": "Loan not found"}), 404
        if request.method != "GET":
            admin_api_required()
            if request.method == "DELETE":
                if loan["returned_at"] is None:
                    return jsonify({"error": "Return the loan before deleting its history"}), 409
                db.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
                db.commit()
                return "", 204
            data = request.get_json() or {}
            due_at = parse_datetime(data.get("due_at"), datetime.fromisoformat(loan["due_at"]))
            if due_at <= datetime.fromisoformat(loan["checkout_at"]):
                return jsonify({"error": "Due date must be after checkout"}), 400
            if active_overlap(db, loan["unit_id"], datetime.fromisoformat(loan["checkout_at"]), due_at, loan_id):
                return jsonify({"error": "The updated loan overlaps another booking"}), 409
            db.execute("UPDATE loans SET due_at = ?, return_condition_note = ? WHERE id = ?", (due_at.isoformat(sep=" "), data.get("return_condition_note", loan["return_condition_note"]), loan_id))
            db.commit()
            return jsonify(row_json(db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()))
        data = row_json(loan)
        data.update({key: json_value(value) for key, value in payment_summary(db, loan_id).items()})
        return jsonify(data)

    @app.patch("/api/loans/<int:loan_id>/transfer")
    @jwt_required()
    def api_transfer_loan(loan_id):
        operator_api_required()
        db = get_db()
        data = request.get_json() or {}
        try:
            borrower_name = request_transfer(db, loan_id, int(data["borrower_id"]), int(get_jwt_identity()))
            db.commit()
            loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
            result = row_json(loan)
            result["transferred_to"] = borrower_name
            result["transfer_status"] = "pending"
            return jsonify(result)
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/transfer-requests/<int:transfer_id>/<action>")
    @jwt_required()
    def api_decide_transfer(transfer_id, action):
        if get_jwt().get("role") != "admin":
            abort(403, description="Admin role required to approve transfers")
        if action not in {"approve", "reject"}:
            return jsonify({"error": "Action must be approve or reject"}), 400
        db = get_db()
        transfer = db.execute("SELECT * FROM loan_transfers WHERE id = ? AND status = 'pending'", (transfer_id,)).fetchone()
        if not transfer:
            return jsonify({"error": "Pending transfer request not found"}), 404
        status = "approved" if action == "approve" else "rejected"
        if status == "approved":
            db.execute("UPDATE loans SET borrower_id = ? WHERE id = ? AND returned_at IS NULL", (transfer["to_borrower_id"], transfer["loan_id"]))
        db.execute("UPDATE loan_transfers SET status = ?, decided_by_user_id = ?, decided_at = ?, decision_note = ? WHERE id = ?", (status, int(get_jwt_identity()), now().isoformat(sep=" "), data.get("note", "") if isinstance(data := (request.get_json(silent=True) or {}), dict) else "", transfer_id))
        db.commit()
        return jsonify({"id": transfer_id, "status": status, "loan_id": transfer["loan_id"]})

    @app.route("/api/payments", methods=["GET", "POST"])
    @jwt_required()
    def api_payments():
        db = get_db()
        if request.method == "GET":
            query = "SELECT * FROM payments"
            params = ()
            if request.args.get("loan_id"):
                query += " WHERE loan_id = ?"
                params = (request.args["loan_id"],)
            return jsonify([row_json(row) for row in db.execute(query + " ORDER BY created_at DESC", params).fetchall()])
        admin_api_required()
        data = request.get_json() or {}
        try:
            amount = money(data["amount"])
            payment_id = db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)", (int(data["loan_id"]), data["payment_type"], data.get("direction", "charge"), str(amount), data.get("status", "paid"), now().isoformat(sep=" ") if data.get("status", "paid") == "paid" else None, data.get("note", ""))).lastrowid
            sync_payment_totals(db, int(data["loan_id"]))
            db.commit()
            return jsonify({"id": payment_id}), 201
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/payments/<int:payment_id>", methods=["GET", "PUT", "DELETE"])
    @jwt_required()
    def api_payment(payment_id):
        admin_api_required() if request.method != "GET" else None
        db = get_db()
        payment = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not payment:
            return jsonify({"error": "Payment not found"}), 404
        if request.method == "GET":
            return jsonify(row_json(payment))
        loan_id = payment["loan_id"]
        if request.method == "DELETE":
            db.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
            sync_payment_totals(db, loan_id)
            db.commit()
            return "", 204
        data = request.get_json() or {}
        amount = money(data.get("amount", payment["amount"]))
        status = data.get("status", payment["status"])
        db.execute("UPDATE payments SET amount = ?, status = ?, note = ?, paid_at = ? WHERE id = ?", (str(amount), status, data.get("note", payment["note"]), now().isoformat(sep=" ") if status == "paid" else payment["paid_at"], payment_id))
        sync_payment_totals(db, loan_id)
        db.commit()
        return jsonify(row_json(db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()))

    return app


app = create_app()
with app.app_context():
    app.init_db()

if __name__ == "__main__":
    app.run(debug=True)