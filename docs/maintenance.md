# Maintenance et dépannage

Arrêtez Red et le dashboard avant une restauration. Une sauvegarde SQLite cohérente se fait avec l'API SQLite (`.backup`), pas par copie brute d'une base active. Vérifiez ensuite `PRAGMA integrity_check` et `PRAGMA foreign_key_check`.

Red Bank et `ledger.sqlite3` sont deux stockages distincts : restaurer uniquement le ledger ne garantit pas la cohérence monétaire. Restaurez-les comme un ensemble cohérent ou réconciliez le coffre avec `m!bankcheck` avant reprise. Lancez les tests et `python -m pip check` après une mise à jour ; pour rollback, arrêtez les services, restaurez la sauvegarde cohérente, puis contrôlez les soldes avant de rouvrir les achats.
