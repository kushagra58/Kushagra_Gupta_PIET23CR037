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