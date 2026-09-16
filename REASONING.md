# Design reasoning

## Item and unit are separate

An `Item` is a category such as “Canon 200D DSLR”; a `Unit` is one physical
camera with its own asset tag, condition, and status. A quantity column could
say that three cameras exist, but it could not identify which camera is already
promised, damaged, or held by a borrower. Availability is therefore calculated
by subtracting overlapping loans from individually trackable units.

## A loan is also a reservation

There is one `loans` table for both current checkouts and future bookings. A
future row has a checkout and due time but no return time, so the same overlap
query can answer “can this unit be booked?” and “how many units are free?” A
separate reservation table would duplicate the most important business rule.

The overlap condition is:

```text
existing.checkout_at < requested_due_at
AND existing.due_at > requested_checkout_at
AND existing.returned_at IS NULL
```

The strict inequalities allow one booking to begin exactly when another ends,
while rejecting every real overlap. Checkout uses this predicate before insert;
availability uses it while counting busy units.

## Status and dates

The authoritative state is the timestamps: a returned loan has `returned_at`,
and an open loan whose `due_at` is in the past is overdue. The schema keeps a
small `status` value as a queryable/materialized label for the requested
`out`/`returned`/`overdue` model; the dashboard refreshes overdue labels from
dates before displaying them. This avoids trusting a stale status as the source
of truth.

## Deposits and late fees

Each loan records the amount held and an explicit deposit status. At return,
late days are calculated from the due date and return date, then:

```text
late_fee = late_days * item.late_fee_per_day
refund = max(0, deposit_amount - late_fee)
```

The return confirmation shows both numbers. A zero-deposit loan is marked
`not_applicable`; a deposit with a deduction becomes `partially_refunded`, and
a full refund becomes `fully_refunded`.

Payments are separate ledger rows rather than a single mutable number. Deposit
charges, late-fee charges, and refund events can therefore be audited. The
loan API derives `amount_due`, `amount_paid`, `remaining_amount`, and
`refund_due` from those rows. An admin refund action validates that a new
refund does not exceed `refund_due` before changing the deposit status.

## Authentication and administration

JWT protects the JSON API. Authenticated users may read records, while tokens
with the `handler` or `admin` role may mutate all records, including payments
and configuration. They intentionally share the same permission boundary.
The browser admin area uses an operator session for practical form workflows.
It can update unit condition, safely remove unused records, and settle partial
or complete refunds. Foreign keys intentionally block deletion when a record
is needed by units or historical loans.

Unauthenticated browser visitors are read-only. The `handler` account is an
alternate staff account with the same control-room permissions as `admin` for
checkouts, returns, nudges, transfers, refunds, deletions, and unit
administration. Finance views aggregate late fees and outstanding refunds so
staff can see what was charged and what still needs to be returned.

## Item imagery and themes

Item images are stored as URLs in the item record. Browser uploads are saved
under `static/uploads` with generated filenames, while API clients can provide
an image URL directly. The dashboard and availability view use fixed image
frames and `object-fit: cover`, so varied source dimensions cannot change the
layout. The light/dark theme is a presentation preference stored in browser
local storage and does not affect lending data.

## Borrow limits and nudges

Current loans are counted by borrower at checkout. If that count reaches the
configured limit, the request is rejected with a clear message. Overdue loans
are placed at the top of the dashboard, and a nudge action writes an auditable
message to `nudge_log` and the application log.

## Loan transfer

Transferring an active loan updates only `loans.borrower_id`. The unit, loan
start, due date, payments, and return history remain on the same loan row. This
means the item continues to occupy exactly the same availability interval; a
transfer cannot accidentally create a second booking or make the unit appear
free. The browser dashboard offers the action directly, while the admin JWT
API exposes `PATCH /api/loans/<id>/transfer`.

## Deliberate omissions

There is no SMS/email provider, payment gateway, or background scheduler. This
keeps the lending workflow demonstrable in a local SQLite app. The app surfaces
overdue work and logs nudges manually; a production deployment could attach a
scheduled worker and messaging provider later.