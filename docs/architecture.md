# Architecture V1

L'interface Discord (`mcorp.py`) appelle les services métier, qui appellent les stores SQLite. Elle ne modifie jamais directement les tables.

`bootstrap.py` construit les services partagés. `banking_service.py` coordonne Red Bank et le ledger ; `ledger.py` conserve les engagements couverts ; `inventory_service.py` applique les règles catalogue ; `shop_store.py` conserve boutiques, prix et audit ; `purchase_service.py` rend une vente atomique dans SQLite. Le dashboard FastAPI est une seconde interface sur ces mêmes services.
