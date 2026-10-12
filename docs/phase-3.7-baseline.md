# Phase 3.7 — Baseline before architecture refactor

Recorded on 2026-10-12 before changing application code.

## Git and runtime baseline

- Source branch: `feat/admin-dashboard`
- Source commit: `1749e5e70be04d2ef0a515cdb424f6418bff707c`
- Source commit date: 2026-10-11 17:55:48 +04:00
- Worktree: clean
- Tests: 134 passing
- `pip check`: no broken requirements

## Active database

Both `.env` and the Red data directory identify the active database as:

```text
/home/mlm/.local/share/Red-DiscordBot/data/mcorp/cogs/MCorp/ledger.sqlite3
```

No Red or Uvicorn process was running while the baseline and backups were
created. The database was last modified on 2026-10-11 at 13:36 +04:00, before
the source commit above.

Consistent SQLite backups were created locally and are ignored by Git:

```text
backups/phase-3.7-baseline/ledger-before-1749e5e.sqlite3
backups/phase-3.7-baseline/legacy-mcorp-before-phase-3.7.db
```

Both backups returned `PRAGMA integrity_check = ok`.

## Integrity and schema baseline

- `PRAGMA integrity_check`: `ok`
- `PRAGMA foreign_key_check`: zero violations
- Application tables: 17, excluding `sqlite_sequence`
- `discord_identities` was absent

The table is absent because this database predates the commit that initializes
it and neither the updated cog nor dashboard had subsequently started against
this file. The table remains an additive `CREATE TABLE IF NOT EXISTS` migration;
no corrective or destructive migration is needed.

| Table | Rows |
|---|---:|
| accounts | 4 |
| control_events | 1 |
| institution_initializations | 1 |
| institutions | 1 |
| inventory_items | 2 |
| inventory_transactions | 2 |
| ledger_transactions | 7 |
| production_schedule_runs | 0 |
| production_schedules | 0 |
| reconciliation_snapshots | 1 |
| shop_item_permissions | 0 |
| shop_license_stock_rules | 0 |
| shop_restock_policies | 0 |
| shops | 1 |
| transaction_participants | 10 |
| wholesale_items | 0 |
| wholesale_price_history | 0 |

## Economic baseline

For institution `mcorp:guild:1433691083807658075`, every active account is
cash-backed:

| Owner type | Accounts | Total balance |
|---|---:|---:|
| SHOP | 1 | 0 |
| SYSTEM | 1 | 25 |
| USER | 2 | 375 |

The covered ledger total is therefore 400. The inventory contains two USER
positions for `immunity`, totaling 3 units.

The legacy `mcorp.db` contained one row in its standalone `inventory` table at
the time it was archived.
