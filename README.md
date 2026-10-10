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
- `m!bankinit [neutraliser]` : initialisation unique du coffre, réservée au
  propriétaire.
- `m!bankcheck` : réconciliation propriétaire : coffre Red, total du ledger,
  différence et état.

Les opérations mixtes sont sérialisées par le cog. Elles valident d'abord les
soldes, tentent une compensation en cas d'échec partiel, puis signalent une
réconciliation manuelle lorsque celle-ci échoue. Ce mécanisme ne prétend pas
remplacer une transaction distribuée entre Red Bank et SQLite.

## Initialisation d’une nouvelle institution M Corp

Les comptes Red Bank ne commencent pas nécessairement à zéro. Red configure un
`default_balance` (100 par défaut) et la lecture du compte du bot via l'API peut
créer/obtenir ce compte avec ce solde. Le coffre M Corp ne doit donc jamais être
présumé vide.

Avant tout dépôt, retrait ou virement, le propriétaire doit :

1. Exécuter `m!bankcheck` pour lire les valeurs Red réelles.
2. Exécuter `m!bankinit`.
3. Si le coffre contient exactement le `default_balance` Red et que le ledger
   est totalement vierge, vérifier qu'il s'agit bien du crédit technique Red,
   puis confirmer explicitement avec `m!bankinit neutraliser`.
4. Exécuter à nouveau `m!bankcheck` ; il doit afficher `Coffre Red : 0`,
   `Ledger total : 0` et `État : OK` avant la première opération.

`m!bankinit` est owner-only et inscrit durablement l'initialisation. Il refuse
une deuxième tentative, un ledger avec soldes ou la moindre transaction, ainsi
que tout solde de coffre qui ne correspond pas exactement au solde par défaut
Red. En cas de refus, ne modifiez ni le fichier SQLite ni les données internes
de Red, et ne relancez pas le cog dans l'espoir de remettre le coffre à zéro :
ces actions pourraient détruire des fonds légitimes. Corrigez l'ambiguïté
manuellement, puis vérifiez avec `m!bankcheck`.

Les opérations couvertes (`m!depot`, `m!retrait`, `m!virement`) sont refusées
tant que cette procédure n'est pas terminée. `m!bankcheck` reste toujours
disponible pour le diagnostic et affiche les montants réels, sans masquer un
solde initial.

La migration SQLite est additive : au chargement, le cog crée seulement la
table `institution_initializations` si elle n'existe pas. Les bases Banking MVP
V1 existantes ne sont ni réécrites ni marquées comme initialisées
automatiquement ; elles doivent suivre la procédure ci-dessus.

## Tests

Depuis l'environnement Red :

```bash
~/.venvs/redbot/bin/python -m unittest discover -s tests -v
```
