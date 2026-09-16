from datetime import datetime, timedelta
import os
from werkzeug.security import generate_password_hash


IMAGE_URLS = {
    "Canon 200D DSLR": "https://images.unsplash.com/photo-1516035069371-29a1b244cc32?auto=format&fit=crop&w=900&q=85",
    "Epson Projector": "https://images.unsplash.com/photo-1497366754035-f200968a6e72?auto=format&fit=crop&w=900&q=85",
    "Wireless Microphone": "https://images.unsplash.com/photo-1590602847861-f357a9332bbc?auto=format&fit=crop&w=900&q=85",
    "Carbon Tripod": "https://images.unsplash.com/photo-1500534623283-312aade485b7?auto=format&fit=crop&w=900&q=85",
}


def add_item(db, name, description, deposit, late_fee, prefix, count):
    item = db.execute(
        "INSERT INTO items (name, description, deposit_amount, late_fee_per_day, image_url) VALUES (?, ?, ?, ?, ?)",
        (name, description, deposit, late_fee, IMAGE_URLS[name]),
    )
    for index in range(1, count + 1):
        db.execute(
            "INSERT INTO units (item_id, asset_tag, condition_note) VALUES (?, ?, ?)",
            (item.lastrowid, f"{prefix}-{index:02d}", "Good"),
        )
    return item.lastrowid


def seed_database(db):
    """Insert a small demo inventory into an empty database."""
    if db.execute("SELECT 1 FROM items LIMIT 1").fetchone():
        return False

    camera_id = add_item(db, "Canon 200D DSLR", "24MP camera body for photo and video work", "2000.00", "50.00", "CAN", 3)
    add_item(db, "Epson Projector", "HD projector with HDMI input", "1500.00", "40.00", "EPS", 2)
    add_item(db, "Wireless Microphone", "Handheld wireless microphone set", "1000.00", "25.00", "MIC", 2)
    add_item(db, "Carbon Tripod", "Lightweight tripod for cameras and phones", "800.00", "20.00", "TRI", 3)

    borrower_id = db.execute(
        "INSERT INTO borrowers (name, borrower_code, contact, club_department) VALUES (?, ?, ?, ?)",
        ("Alice Sharma", "DEMO001", "alice@college.edu", "Film Club"),
    ).lastrowid
    for borrower in (
        ("Bob Verma", "DEMO002", "bob@college.edu", "Robotics Club"),
        ("Meera Iyer", "DEMO003", "meera@college.edu", "Media Cell"),
        ("Kabir Singh", "DEMO004", "kabir@college.edu", "Drama Society"),
    ):
        db.execute("INSERT INTO borrowers (name, borrower_code, contact, club_department) VALUES (?, ?, ?, ?)", borrower)
    db.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (os.environ.get("ADMIN_USERNAME", "admin"), generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123"))),
    )
    unit_id = db.execute("SELECT id FROM units WHERE asset_tag = 'CAN-01'").fetchone()[0]
    checkout = datetime.now().replace(microsecond=0) - timedelta(days=1)
    due = checkout + timedelta(days=3)
    db.execute(
        "INSERT INTO loans (unit_id, borrower_id, checkout_at, due_at, deposit_amount, deposit_status, amount_due, amount_paid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (unit_id, borrower_id, checkout.isoformat(sep=" "), due.isoformat(sep=" "), "2000.00", "held", "0.00", "2000.00"),
    )
    loan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO payments (loan_id, payment_type, direction, amount, status, paid_at, note) VALUES (?, 'deposit', 'charge', ?, 'paid', ?, 'Demo deposit')",
        (loan_id, "2000.00", checkout.isoformat(sep=" ")),
    )
    db.commit()
    return True


def expand_demo_data(db):
    """Bring an older local demo database up to the richer sample inventory."""
    image_updates = {
        "Canon": IMAGE_URLS["Canon 200D DSLR"],
        "Epson Projector": IMAGE_URLS["Epson Projector"],
        "Wireless Microphone": IMAGE_URLS["Wireless Microphone"],
        "Carbon Tripod": IMAGE_URLS["Carbon Tripod"],
    }
    for name, image_url in image_updates.items():
        db.execute("UPDATE items SET image_url = ? WHERE name LIKE ?", (image_url, name if name == "Canon" else name))
    existing = {row[0] for row in db.execute("SELECT name FROM items")}
    missing = [
        ("Epson Projector", "HD projector with HDMI input", "1500.00", "40.00", "EPS", 2),
        ("Wireless Microphone", "Handheld wireless microphone set", "1000.00", "25.00", "MIC", 2),
        ("Carbon Tripod", "Lightweight tripod for cameras and phones", "800.00", "20.00", "TRI", 3),
    ]
    for item in missing:
        if item[0] not in existing:
            add_item(db, *item)
    target_units = {"Canon": ("CAN", 3), "Canon 200D DSLR": ("CAN", 3), "Epson Projector": ("EPS", 2), "Wireless Microphone": ("MIC", 2), "Carbon Tripod": ("TRI", 3)}
    for name, (prefix, target) in target_units.items():
        item = db.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
        if not item:
            continue
        current_count = db.execute("SELECT COUNT(*) FROM units WHERE item_id = ?", (item[0],)).fetchone()[0]
        for index in range(current_count + 1, target + 1):
            db.execute("INSERT INTO units (item_id, asset_tag, condition_note) VALUES (?, ?, ?)", (item[0], f"{prefix}-{index:02d}", "Good"))
    borrower_count = db.execute("SELECT COUNT(*) FROM borrowers").fetchone()[0]
    borrowers = (
        ("Bob Verma", "DEMO002", "bob@college.edu", "Robotics Club"),
        ("Meera Iyer", "DEMO003", "meera@college.edu", "Media Cell"),
        ("Kabir Singh", "DEMO004", "kabir@college.edu", "Drama Society"),
    )
    if borrower_count < 4:
        for borrower in borrowers:
            db.execute("INSERT OR IGNORE INTO borrowers (name, borrower_code, contact, club_department) VALUES (?, ?, ?, ?)", borrower)
    db.commit()


if __name__ == "__main__":
    from app import app

    with app.app_context():
        print("Demo data already exists or was inserted.") if not seed_database(app.get_db()) else print("Inserted demo lending data.")