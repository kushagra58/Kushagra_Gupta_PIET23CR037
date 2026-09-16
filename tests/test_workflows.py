from datetime import datetime, timedelta
from io import BytesIO

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    application = create_app({"TESTING": True, "AUTO_SEED": False, "DATABASE": str(tmp_path / "test.sqlite3"), "LOAN_LIMIT": 1})
    with application.app_context():
        application.init_db()
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def seed(client, units=1, deposit="0", late_fee="0"):
    client.post("/items", data={"name": "Projector", "unit_count": str(units), "deposit_amount": deposit, "late_fee_per_day": late_fee})
    client.post("/borrowers", data={"name": "Aarav Sharma", "borrower_code": "B001"})


def ids(app):
    with app.app_context():
        db = app.get_db()
        return (db.execute("SELECT id FROM units ORDER BY id LIMIT 1").fetchone()["id"], db.execute("SELECT id FROM borrowers LIMIT 1").fetchone()["id"])


def test_overlapping_same_unit_is_rejected_and_future_availability_is_real(client, app):
    seed(client)
    unit_id, borrower_id = ids(app)
    first = client.post("/checkout", data={"unit_id": unit_id, "borrower_id": borrower_id, "checkout_at": "2030-06-01T09:00", "due_at": "2030-06-03T09:00"}, follow_redirects=True)
    second = client.post("/checkout", data={"unit_id": unit_id, "borrower_id": borrower_id, "checkout_at": "2030-06-02T09:00", "due_at": "2030-06-04T09:00"}, follow_redirects=True)
    availability = client.get("/availability?start=2030-06-02T00:00&end=2030-06-03T23:00")
    assert b"checked out" in first.data
    assert b"already booked" in second.data
    assert b"0</strong><span>free in this window" in availability.data


def test_return_records_late_fee_and_refund(client, app):
    seed(client, deposit="100", late_fee="10")
    unit_id, borrower_id = ids(app)
    due = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
    checkout = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat(sep=" ")
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, 'held')", (unit_id, borrower_id, checkout, due, "100.00"))
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    response = client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    with app.app_context():
        loan = app.get_db().execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    assert b"Late fee: INR 10.00" in response.data
    assert loan["status"] == "returned"
    assert loan["deposit_status"] == "partially_refunded"
    assert loan["refund_amount"] == 90


def test_borrower_limit_rejects_second_current_loan(client, app):
    seed(client, units=2)
    unit_id, borrower_id = ids(app)
    with app.app_context():
        second_unit = app.get_db().execute("SELECT id FROM units ORDER BY id DESC LIMIT 1").fetchone()["id"]
    first = client.post("/checkout", data={"unit_id": unit_id, "borrower_id": borrower_id, "due_at": "2030-06-03T09:00"}, follow_redirects=True)
    second = client.post("/checkout", data={"unit_id": second_unit, "borrower_id": borrower_id, "due_at": "2030-06-04T09:00"}, follow_redirects=True)
    assert b"checked out" in first.data
    assert b"already has 1 active units" in second.data


def test_history_keeps_returned_loans(client, app):
    seed(client)
    unit_id, borrower_id = ids(app)
    checkout = client.post("/checkout", data={"unit_id": unit_id, "borrower_id": borrower_id, "due_at": "2030-06-03T09:00"}, follow_redirects=True)
    with app.app_context():
        loan_id = app.get_db().execute("SELECT id FROM loans LIMIT 1").fetchone()["id"]
    client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    history = client.get("/history")
    assert checkout.status_code == 200
    assert b"Loan history" in history.data
    assert b"Returned" in history.data


def admin_token(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    return response.get_json()["access_token"]


def handler_token(client):
    response = client.post("/api/auth/login", json={"username": "handler", "password": "handler123"})
    assert response.status_code == 200
    return response.get_json()["access_token"]


def test_jwt_protects_api_and_admin_can_create_item(client):
    assert client.get("/api/items").status_code == 401
    token = admin_token(client)
    response = client.post("/api/items", json={"name": "Tripod", "unit_count": 2}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201
    assert client.get("/api/items", headers={"Authorization": f"Bearer {token}"}).get_json()[-1]["name"] == "Tripod"


def test_api_payment_settlement_updates_remaining_balance(client, app):
    seed(client, deposit="100", late_fee="10")
    unit_id, borrower_id = ids(app)
    due = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
    checkout = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat(sep=" ")
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, 'held')", (unit_id, borrower_id, checkout, due, "100.00"))
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    token = admin_token(client)
    detail = client.get(f"/api/loans/{loan_id}", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert detail["remaining_amount"] == "10.00"
    late_payment = client.get(f"/api/payments?loan_id={loan_id}", headers={"Authorization": f"Bearer {token}"}).get_json()
    late_payment_id = next(payment["id"] for payment in late_payment if payment["payment_type"] == "late_fee")
    settled = client.put(f"/api/payments/{late_payment_id}", json={"status": "paid"}, headers={"Authorization": f"Bearer {token}"})
    assert settled.status_code == 200
    assert client.get(f"/api/loans/{loan_id}", headers={"Authorization": f"Bearer {token}"}).get_json()["remaining_amount"] == "0.00"


def test_uploaded_item_image_is_listed(client, app):
    response = client.post("/items", data={
        "name": "Studio Light",
        "unit_count": "1",
        "image": (BytesIO(b"fake-png-data"), "studio-light.png"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"Studio Light" in response.data
    with app.app_context():
        image_url = app.get_db().execute("SELECT image_url FROM items WHERE name = 'Studio Light'").fetchone()["image_url"]
    assert image_url.startswith("/static/uploads/")


def test_admin_can_remove_unused_records_but_protects_item_units(client, app):
    with app.app_context():
        db = app.get_db()
        unused_item = db.execute("INSERT INTO items (name) VALUES ('Unused Item')").lastrowid
        unused_borrower = db.execute("INSERT INTO borrowers (name, borrower_code) VALUES ('Unused Borrower', 'UNUSED')").lastrowid
    client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    client.post(f"/admin/items/{unused_item}/delete", follow_redirects=True)
    client.post(f"/admin/borrowers/{unused_borrower}/delete", follow_redirects=True)
    with app.app_context():
        db = app.get_db()
        assert db.execute("SELECT 1 FROM items WHERE id = ?", (unused_item,)).fetchone() is None
        assert db.execute("SELECT 1 FROM borrowers WHERE id = ?", (unused_borrower,)).fetchone() is None

    seed(client)
    item_id, _ = ids(app)
    protected = client.post(f"/admin/items/{item_id}/delete", follow_redirects=True)
    assert b"cannot be removed" in protected.data


def test_admin_can_settle_partial_then_full_refund(client, app):
    seed(client, deposit="100", late_fee="0")
    unit_id, borrower_id = ids(app)
    due = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
    checkout = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat(sep=" ")
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, 'held')", (unit_id, borrower_id, checkout, due, "100.00"))
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    partial = client.post(f"/admin/loans/{loan_id}/refund", data={"amount": "40"}, follow_redirects=True)
    with app.app_context():
        status = app.get_db().execute("SELECT deposit_status FROM loans WHERE id = ?", (loan_id,)).fetchone()["deposit_status"]
    assert b"partially refunded" in partial.data
    assert status == "partially_refunded"
    client.post(f"/admin/loans/{loan_id}/refund", data={"amount": "60"}, follow_redirects=True)
    with app.app_context():
        status = app.get_db().execute("SELECT deposit_status FROM loans WHERE id = ?", (loan_id,)).fetchone()["deposit_status"]
    assert status == "fully_refunded"


def test_active_loan_can_transfer_borrower_without_changing_due_or_availability(client, app):
    seed(client)
    client.post("/borrowers", data={"name": "Bob Verma", "borrower_code": "B002"})
    handler_login = client.post("/login", data={"username": "handler", "password": "handler123"})
    unit_id, original_borrower_id = ids(app)
    with app.app_context():
        second_borrower_id = app.get_db().execute("SELECT id FROM borrowers WHERE borrower_code = 'B002'").fetchone()["id"]
    checkout = client.post("/checkout", data={"unit_id": unit_id, "borrower_id": original_borrower_id, "checkout_at": "2030-06-01T09:00", "due_at": "2030-06-04T09:00"}, follow_redirects=True)
    with app.app_context():
        db = app.get_db()
        loan = db.execute("SELECT id, due_at, unit_id FROM loans LIMIT 1").fetchone()
        before = client.get("/availability?start=2030-06-02T00:00&end=2030-06-03T23:00").data
    transferred = client.post(f"/loans/{loan['id']}/transfer", data={"borrower_id": second_borrower_id}, follow_redirects=True)
    after = client.get("/availability?start=2030-06-02T00:00&end=2030-06-03T23:00").data
    history = client.get("/history")
    with app.app_context():
        updated = app.get_db().execute("SELECT * FROM loans WHERE id = ?", (loan["id"],)).fetchone()
    assert checkout.status_code == 200
    assert handler_login.status_code == 302
    assert b"waiting for admin approval" in transferred.data
    assert b"Waiting for approval" in history.data
    assert b"04 Jun 2030" in history.data
    assert b"Aarav Sharma" in history.data
    assert updated["borrower_id"] == original_borrower_id
    assert updated["due_at"] == loan["due_at"]
    assert updated["unit_id"] == loan["unit_id"]
    assert before == after
    with app.app_context():
        transfer = app.get_db().execute("SELECT changed_by_user_id, status FROM loan_transfers WHERE loan_id = ?", (loan["id"],)).fetchone()
        handler = app.get_db().execute("SELECT id FROM users WHERE username = 'handler'").fetchone()
    assert transfer["changed_by_user_id"] == handler["id"]
    assert transfer["status"] == "pending"


def test_admin_api_can_transfer_active_loan(client, app):
    seed(client)
    client.post("/borrowers", data={"name": "Meera Iyer", "borrower_code": "B003"})
    unit_id, borrower_id = ids(app)
    with app.app_context():
        target_id = app.get_db().execute("SELECT id FROM borrowers WHERE borrower_code = 'B003'").fetchone()["id"]
    client.post("/checkout", data={"unit_id": unit_id, "borrower_id": borrower_id, "due_at": "2030-06-04T09:00"})
    with app.app_context():
        loan = app.get_db().execute("SELECT id, due_at FROM loans LIMIT 1").fetchone()
    response = client.patch(f"/api/loans/{loan['id']}/transfer", json={"borrower_id": target_id}, headers={"Authorization": f"Bearer {admin_token(client)}"})
    assert response.status_code == 200
    assert response.get_json()["borrower_id"] == borrower_id
    assert response.get_json()["transfer_status"] == "pending"
    assert response.get_json()["due_at"] == loan["due_at"]
    with app.app_context():
        transfer_id = app.get_db().execute("SELECT id FROM loan_transfers WHERE loan_id = ?", (loan["id"],)).fetchone()["id"]
    approved = client.patch(f"/api/transfer-requests/{transfer_id}/approve", headers={"Authorization": f"Bearer {admin_token(client)}"})
    assert approved.status_code == 200
    assert client.get(f"/api/loans/{loan['id']}", headers={"Authorization": f"Bearer {admin_token(client)}"}).get_json()["borrower_id"] == target_id


def test_anonymous_browser_cannot_change_lending_data(tmp_path):
    application = create_app({"TESTING": False, "AUTO_SEED": False, "DATABASE": str(tmp_path / "anonymous.sqlite3")})
    with application.app_context():
        application.init_db()
    response = application.test_client().post("/items", data={"name": "Blocked Item", "unit_count": "1"})
    assert response.status_code == 302
    assert response.location.endswith("/login?next=/items")


def test_handler_has_admin_control_room_and_mutation_access(client):
    browser_login = client.post("/login", data={"username": "handler", "password": "handler123"}, follow_redirects=True)
    assert b"Control room" in browser_login.data
    token = handler_token(client)
    response = client.post("/api/items", json={"name": "Handler Item", "unit_count": 1}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201


def test_staff_can_settle_due_late_fee(client, app):
    seed(client, deposit="100", late_fee="10")
    unit_id, borrower_id = ids(app)
    due = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
    checkout = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat(sep=" ")
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, 'held')", (unit_id, borrower_id, checkout, due, "100.00"))
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    client.post("/login", data={"username": "handler", "password": "handler123"})
    with app.app_context():
        payment_id = app.get_db().execute("SELECT id FROM payments WHERE payment_type = 'late_fee'").fetchone()["id"]
    response = client.post(f"/admin/payments/{payment_id}/settle", follow_redirects=True)
    with app.app_context():
        db = app.get_db()
        payment = db.execute("SELECT status FROM payments WHERE id = ?", (payment_id,)).fetchone()
        loan = db.execute("SELECT deposit_status FROM loans WHERE id = ?", (loan_id,)).fetchone()
    assert b"Settled INR 10.00 late fee" in response.data
    assert payment["status"] == "paid"
    assert loan["deposit_status"] == "fully_refunded"
    deposits = client.get("/deposits")
    assert b"Fee payment" in deposits.data
    assert b">Paid</span>" in deposits.data
    assert b"Fully Refunded" in deposits.data


def test_zero_refund_balance_reconciles_to_fully_refunded(client, app):
    seed(client, deposit="100", late_fee="0")
    unit_id, borrower_id = ids(app)
    due = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
    checkout = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat(sep=" ")
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status) VALUES (?, ?, ?, ?, ?, 'held')", (unit_id, borrower_id, checkout, due, "100.00"))
        db.commit()
        loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    client.post(f"/loans/{loan_id}/return", follow_redirects=True)
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at) VALUES (?, 'refund', 'refund', '100.00', 'paid', CURRENT_TIMESTAMP)", (loan_id,))
        db.execute("UPDATE loans SET deposit_status = 'partially_refunded' WHERE id = ?", (loan_id,))
        db.commit()
    client.get("/deposits")
    with app.app_context():
        status = app.get_db().execute("SELECT deposit_status FROM loans WHERE id = ?", (loan_id,)).fetchone()["deposit_status"]
    assert status == "fully_refunded"


def test_checkout_locks_to_the_signed_in_users_own_linked_borrower(client, app):
    client.post("/items", data={"name": "Camera", "unit_count": "1", "deposit_amount": "0", "late_fee_per_day": "0"})
    client.post("/login", data={"username": "handler", "password": "handler123"})
    client.post("/borrowers", data={"name": "Aarav Sharma", "borrower_code": "B001"})
    client.post("/borrowers", data={"name": "Riya Kapoor", "borrower_code": "B002", "link_to_me": "1"})
    with app.app_context():
        db = app.get_db()
        unit_id = db.execute("SELECT id FROM units LIMIT 1").fetchone()["id"]
        other_borrower_id = db.execute("SELECT id FROM borrowers WHERE borrower_code = 'B001'").fetchone()["id"]
        own_borrower_id = db.execute("SELECT id FROM borrowers WHERE borrower_code = 'B002'").fetchone()["id"]

    page = client.get("/checkout")
    assert b"Riya Kapoor / B002" in page.data
    assert b"Aarav Sharma / B001" not in page.data

    # Even if a different borrower_id is submitted (e.g. a tampered request),
    # the checkout is forced onto the signed-in user's own linked borrower.
    client.post("/checkout", data={"unit_id": unit_id, "borrower_id": other_borrower_id, "due_at": "2031-01-01T09:00"}, follow_redirects=True)
    with app.app_context():
        loan = app.get_db().execute("SELECT borrower_id FROM loans ORDER BY id DESC LIMIT 1").fetchone()
    assert loan["borrower_id"] == own_borrower_id
    assert loan["borrower_id"] != other_borrower_id