PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    deposit_amount NUMERIC NOT NULL DEFAULT 0 CHECK (deposit_amount >= 0),
    late_fee_per_day NUMERIC NOT NULL DEFAULT 0 CHECK (late_fee_per_day >= 0),
    image_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    asset_tag TEXT NOT NULL UNIQUE,
    condition_note TEXT NOT NULL DEFAULT 'Good',
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'maintenance', 'retired')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS borrowers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    borrower_code TEXT NOT NULL UNIQUE,
    contact TEXT NOT NULL DEFAULT '',
    club_department TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
    borrower_id INTEGER NOT NULL REFERENCES borrowers(id) ON DELETE RESTRICT,
    checkout_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    returned_at TEXT,
    status TEXT NOT NULL DEFAULT 'out' CHECK (status IN ('out', 'returned', 'overdue')),
    deposit_amount NUMERIC NOT NULL DEFAULT 0 CHECK (deposit_amount >= 0),
    deposit_status TEXT NOT NULL DEFAULT 'not_applicable' CHECK (deposit_status IN ('not_applicable', 'held', 'partially_refunded', 'fully_refunded')),
    late_fee NUMERIC NOT NULL DEFAULT 0 CHECK (late_fee >= 0),
    refund_amount NUMERIC,
    amount_due NUMERIC NOT NULL DEFAULT 0 CHECK (amount_due >= 0),
    amount_paid NUMERIC NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
    return_condition_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (due_at > checkout_at)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'operator')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
    payment_type TEXT NOT NULL CHECK (payment_type IN ('deposit', 'late_fee', 'other', 'refund')),
    direction TEXT NOT NULL CHECK (direction IN ('charge', 'refund')),
    amount NUMERIC NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('due', 'paid', 'void')),
    note TEXT NOT NULL DEFAULT '',
    paid_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id, status, direction);

CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_loans_unit_dates ON loans(unit_id, checkout_at, due_at);
CREATE INDEX IF NOT EXISTS idx_loans_open ON loans(returned_at, status);