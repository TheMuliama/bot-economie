# M Corp Bot — client API MVP

Cog Red-DiscordBot léger pour l'économie M Corp. PostgreSQL et les règles
économiques appartiennent exclusivement à [MCorp API](../mcorp-api) ; ce dépôt
ne contient ni banque Red, ni SQLite, ni ledger actif.

## Installation et configuration

Utiliser Python 3.11 : `Red-DiscordBot==3.5.24` ne prend pas en charge Python
3.12. L'API MCorp peut, elle, rester exécutée avec Python 3.12.

```bash
pip install -r requirements.txt
```

Configurer les secrets du processus Red, jamais dans Git :

```bash
export MCORP_API_URL="http://127.0.0.1:8000"
export MCORP_BOT_API_KEY="…"
export MCORP_API_TIMEOUT_SECONDS="10"  # optionnel
```

`MCORP_BOT_API_KEY` est la clé dédiée à l'identité API `mcorp-official`. Ne
jamais utiliser `ADMIN_API_KEY` dans ce cog, ni afficher une clé dans Discord,
les logs ou un fichier versionné. La clé reste un secret de confiance élevé :
elle appartient uniquement au déploiement du bot officiel.

Installez ensuite le dossier comme cog Red normalement et chargez `mcorp`.
L'API doit être démarrée et son catalogue doit contenir `potion`; le stock de
la boutique doit appartenir à `BOT/mcorp-shop`.

## Commandes MVP

- `m!argent [@membre]` — portefeuille RTC API.
- `m!donner @membre <montant>` — transfert RTC, positif et jamais vers soi.
- `m!inventaire [@membre]` — inventaire API.
- `m!boutique` — stock réel et prix de la boutique officielle.
- `m!acheter <objet> [quantité]` — échange atomique avec `BOT/mcorp-shop`.
- `m!utiliser <objet>` — consomme un objet après confirmation API.

La boutique MVP propose `potion` à **200 RTC**. Les prix sont une petite
configuration de présentation du bot ; l'API vérifie toujours fonds, stock,
transfert et consommation dans ses propres transactions.

## Fiabilité et limites

Chaque mutation utilise un `operation_id` unique. En cas de timeout ou erreur
serveur, le client effectue une seule nouvelle tentative avec le même
identifiant; l'API décide si elle rejoue le résultat ou refuse l'opération.
Une commande Discord relancée manuellement est une nouvelle intention et crée
un nouvel identifiant.

`m!prime` est volontairement absent : la clé officielle ne dispose pas de
permission de crédit administratif. Sont retirés du cog actif : Red Bank,
comptes couverts/ledger SQLite, dépôts/retraits, prêts, grossiste, boutiques
joueur, dashboard, OAuth et scheduler. Leur historique reste disponible dans
Git, mais leur code n'est plus présent dans le cog MVP.

## Tests

Les tests unitaires simulent l'API HTTP :

```bash
python -m unittest discover -s tests -v
python -m pip check
```

## Checklist Discord manuelle

1. Démarrer l'API avec `MCORP_BOT_API_KEY` et provisionner `potion` pour
   `BOT/mcorp-shop`.
2. Charger le cog avec les trois variables `MCORP_*` ci-dessus.
3. Vérifier `m!argent`, `m!inventaire` et `m!boutique`.
4. Effectuer un petit `m!donner`, puis `m!acheter potion` et `m!utiliser potion`.
5. Vérifier les soldes et inventaires uniquement via l'API ; ne pas tester avec
   une clé administrative dans Red.
