# Data

| File | What it is |
|---|---|
| `starter/` | Supplied starter pack, unchanged (`pack-version.json` = 2026-10-02). `domain.md` is the source of truth for the rules. |
| `additions.json` | Our extension: 8 customers, 26 orders, 5 refunds (total 10 / 30 / 8, the suggested size). |
| `build_db.py` | Builds `investigation.sqlite` from `starter/schema.sql` + `starter/seed.json` + `additions.json`. Checks the data contract first and enforces foreign keys. |
| `investigation.sqlite` | Build output used by the app. Rebuild with `uv run python -m data.build_db`. |
| `expected.json` | Answer key, calculated by hand from the listed row IDs. Not produced by the application. |

## Generation method

Hand-designed with AI assistance, no randomness, so there is no random seed. Amounts are whole cents. The rows tell one story: gross sales rise slightly (+10,000) while refunds rise sharply (+18,500), so net sales fall (-8,500). Most of the September refunds belong to the `small` segment. Seed rows and the seed answer key are kept unchanged.

## Planted cases, one per rule

| Rows | Rule exercised | Correct treatment |
|---|---|---|
| O3 with RF2 + RF3 (seed) | 3: aggregate before joining | O3 counted once. A naive join gives September gross 209,000 instead of 204,000. |
| RF4 (2026-09-02) on O13 (August order) | 2: refunds by `refund_date` | Counts in September refunds, not August. |
| RF5 (2026-09-01), O17 (2026-09-01) | 2: start date included | Both in September. |
| O16 (2026-08-31), RF6 (2026-08-31) | 2: end date excluded | Both in August. |
| O30 (2026-10-01), RF8 (2026-10-01) | 2: end date excluded | Neither in September. |
| RF4 on O13 (customer C3, `small`) | 4: segment of the original order's customer | `small`. |
| RF4 = O13's full amount (7,000) | Refunds per order ≤ order amount | Valid boundary, allowed. |

Refunds are matched by `refund_date` and orders by `order_date`. Counting refunds by their order's month instead gives 11,000 for September, which is wrong. The correct figure is 21,000.

## Checks

`tests/test_data.py` checks the answer key three independent ways: by hand (`expected.json`), in Python (`investigator/calc.py`) and in plain SQL. It also confirms `calc.py` reproduces the supplied seed results, that the supplied `seed.sqlite` equals `seed.json`, and that the database rebuilds identically from the JSON files.

The five reference cases for the brief's minimum demonstration are in the check table of the
top-level README. Their expected values come from `expected.json`; the observed values come from
saved live runs. `tests/test_demo.py` replays those runs and compares them with this key.
