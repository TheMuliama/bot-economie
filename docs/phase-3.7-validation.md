# Phase 3.7 — Final architecture validation

Validated on 2026-10-12 from branch `refactor/v1-architecture`.

## Database safety

The active database was opened read-only and copied with SQLite's backup API.
All Phase 3.7 initializers, including the dashboard scheduler initializer, were
then executed against the disposable copy only.

- `PRAGMA integrity_check`: `ok`
- `PRAGMA foreign_key_check`: zero violations
- existing application tables: all rows exactly unchanged
- accounts: 4 before, 4 after
- institutions: 1 before, 1 after
- shops: 1 before, 1 after
- control events: 1 before, 1 after
- inventory positions: 2 before, 2 after
- ledger transactions: 7 before, 7 after
- production schedules/runs: 0/0 before and after
- only additive schema change: empty `discord_identities` table created

Covered balances remain unchanged: `SHOP=0`, `SYSTEM=25`, `USER=375`, for a
total of 400. Inventory remains two USER positions totaling three `immunity`
items. No `DROP TABLE`, `TRUNCATE`, or data deletion statement is present in
the application initializers.

The active database itself retained its original size and modification time;
it was not initialized or otherwise mutated during validation.

## Application checks

- full suite: 136 tests passing (baseline: 134)
- dashboard import: succeeds without loading `mcorp.mcorp` or `redbot`
- Red Cog command boundary tests: passing
- dashboard OAuth, RBAC, CSRF and identity tests: passing
- ADMIN and MODERATOR dashboard access tests: passing
- scheduler additive migration and anti-double-claim tests: passing
- banking invariant, compensation and recovery tests: passing
- inventory, production, licence, restock and audit tests: passing
- `pip check`: no broken requirements

The production recommendation remains one Uvicorn worker for the V1
scheduler. `spent_in_period` remains cumulative because no functional period
or reset rule has been approved.
