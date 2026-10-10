# M Corp Bot

Cog Red-DiscordBot pour les mécaniques économiques de M Corp.

## Banking MVP V1

Red Bank est l'unique source de vérité des **R-crédits**. Le compte Red Bank
du bot est le coffre M Corp. Le ledger M Corp représente exclusivement les
passifs couverts par ce coffre : un crédit bancaire doit toujours correspondre
à un R-crédit détenu dans le coffre.

Le ledger et son journal sont stockés dans `ledger.sqlite3`, sous le répertoire
de données de Red pour le cog (`cog_data_path(MCorp)`). SQLite a été retenu
pour ses transactions locales et ses requêtes d'historique. Le fichier
historique `mcorp.db` du dépôt est un prototype d'inventaire : il n'est ni lu,
ni modifié, ni supprimé par ce cog.

Chaque institution possède des comptes typés (`owner_id`, `owner_type`,
`institution_id`, `account_type`, `balance`, `status`) et un journal immutable.
La V1 crée des comptes `USER` / `CHECKING`, mais le schéma prévoit les futurs
propriétaires `COMPANY`, `BANK`, `NPC` et `SYSTEM` sans les activer.

## Commandes

- `m!solde` : portefeuille Red Bank.
- `m!compte` : portefeuille Red et compte bancaire M Corp.
- `m!depot <montant>` : transfert du portefeuille Red vers le coffre et crédit
  du ledger.
- `m!retrait <montant>` : opération inverse.
- `m!payer @membre <montant>` : paiement direct en R-crédits via Red Bank.
- `m!virement @membre <montant>` : transfert entre comptes M Corp, sans
  mouvement du coffre.
- `m!transactions` : dix dernières opérations pertinentes.
- `m!crediter`, `m!debiter` : opérations Red Bank réservées au propriétaire,
  explicitement hors ledger.
- `m!bankcheck` : réconciliation propriétaire : coffre Red, total du ledger,
  différence et état.

Les opérations mixtes sont sérialisées par le cog. Elles valident d'abord les
soldes, tentent une compensation en cas d'échec partiel, puis signalent une
réconciliation manuelle lorsque celle-ci échoue. Ce mécanisme ne prétend pas
remplacer une transaction distribuée entre Red Bank et SQLite.

Avant la première opération, le coffre Red du bot doit être vide et le ledger
doit totaliser zéro. Si ce n'est pas le cas, `m!bankcheck` signalera l'écart et
les opérations couvertes seront refusées jusqu'à résolution explicite : le cog
ne crée jamais une écriture de correction automatiquement.

## Tests

Depuis l'environnement Red :

```bash
~/.venvs/redbot/bin/python -m unittest discover -s tests -v
```
