# Auriga Drive

Auriga Drive is a general-purpose lending desk for equipment rooms, libraries,
tool libraries, and maker spaces. It tracks item categories, individually
tagged physical units, borrowers, date-bounded loans, overdue work, and loan
history in SQLite.

## Run locally

Prerequisite: Python 3.10 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. The database file `lending_desk.sqlite3` and its
schema are created automatically on first startup, along with demo cameras,
projectors, microphones, a borrower, and one active loan. To use Flask's
debug reloader instead, run `.venv/bin/flask --app app run --debug`.

## Main workflows

- Add an item with one or more physical units and optional deposit/late-fee defaults.
- Add borrowers from the checkout screen.
- Check out now or reserve a unit for a future date range.
- Use Availability to ask how many units are free across any date window.
- Return gear to close the loan and record late fees, refunds, and condition notes.
- Review overdue loans on the dashboard and log a nudge message to the application log.
- Open History to inspect returned and active loans without deleting the audit trail.
- Use the theme button in the top navigation to switch between light and dark mode; the choice is remembered in the browser.
- Add an item image by uploading JPG, PNG, WEBP, or GIF files; image URLs are also supported for API clients.
- The local demo seed includes DSLR, projector, microphone, and tripod imagery, multiple physical units, and four borrowers.

The default concurrent-loan limit is three units per borrower. Change
`LOAN_LIMIT` in `app.py` or provide it in application configuration for a
deployment-specific policy.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests cover physical-unit overlap prevention, future availability,
late-fee/refund accounting, concurrent-loan limits, JWT protection, payment
settlement, image upload/listing, and admin deletion/refund controls.

## API and admin access

The API is available under `/api`. Obtain a JWT with:

```bash
curl -X POST http://127.0.0.1:5000/api/auth/login \
	-H 'Content-Type: application/json' \
	-d '{"username":"admin","password":"admin123"}'
```

Send the returned token as `Authorization: Bearer <token>`. Authenticated
users can read records; the seeded admin can create, update, and delete items,
units, borrowers, loans, and payments. Loan responses include `amount_due`,
`amount_paid`, `remaining_amount`, and `refund_due`. Payment writes immediately
refresh the loan totals.

The browser admin page is `/admin`. Set `ADMIN_PASSWORD` and
`JWT_SECRET_KEY` in the environment before any real deployment; the local
`admin` / `admin123` account is demo-only.

Admin controls include unit condition/status updates, safe deletion of unused
items and borrowers, and partial or full refund settlement. Records with units
or loan history are protected so the audit trail is not destroyed.

## Debugging

- `ModuleNotFoundError: flask`: activate the environment or run `.venv/bin/pip install -r requirements.txt`.
- A stale demo database is confusing: stop the app, delete `lending_desk.sqlite3`, and run `python app.py` again.
- Port 5000 is occupied: run `.venv/bin/flask --app app run --port 5001` and open port 5001.
- Tests cannot import the app: run `.venv/bin/python -m pytest -q` from the repository root.
- Uploaded images are not appearing: check that the file is JPG, PNG, WEBP, or GIF and refresh the page after upload.
