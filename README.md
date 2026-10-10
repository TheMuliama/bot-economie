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
`institution_id`, `account_type`, `balance`, `status`, `is_cash_backed`) et un
journal immutable. L'invariant est calculé exclusivement sur les comptes
actifs marqués `is_cash_backed = 1`, et non plus implicitement sur tous les
comptes actifs.

La V1 crée des comptes `USER` / `CHECKING`. La Phase 2 ajoute un unique compte
`SYSTEM` / `TREASURY` par institution, identifié par `M_CORP_TREASURY`. Il est
couvert, dépensable et créé à zéro lors de `m!bankinit`. La trésorerie ne crée
pas de monnaie : elle reçoit ou verse uniquement des crédits déjà présents dans
le ledger couvert.

## Commandes

- `m!solde` : portefeuille Red Bank.
- `m!compte` : portefeuille Red et compte bancaire M Corp.
- `m!depot <montant>` : transfert du portefeuille Red vers le coffre et crédit
  du ledger.
- `m!retrait <montant>` : opération inverse.
- `m!payer @membre <montant>` : paiement direct en R-crédits via Red Bank.
- `m!virement @membre <montant>` : transfert entre comptes M Corp, sans
  mouvement du coffre.
- `m!payermcorp <montant>` : transfert bancaire interne vers la trésorerie.
- `m!tresorerie` : solde de trésorerie et réconciliation publique minimale.
- `m!prime @membre <montant>` : versement owner-only depuis la trésorerie.
- `m!transactions` : dix dernières opérations pertinentes.
- `m!crediter`, `m!debiter` : opérations Red Bank réservées au propriétaire,
  explicitement hors ledger.
- `m!bankinit [neutraliser]` : initialisation unique du coffre, réservée au
  propriétaire.
- `m!bankcheck` : réconciliation propriétaire : coffre Red, total du ledger,
  différence et état.
- `m!economycheck` : statistiques owner-only des flux institutionnels.

## Trésorerie institutionnelle — Phase 2

Les opérations institutionnelles sont exclusivement des transferts SQLite
atomiques entre comptes couverts. Elles ne modifient jamais le coffre Red :

```text
joueur -> trésorerie : PLAYER_TO_TREASURY
trésorerie -> joueur : TREASURY_TO_PLAYER
```

Ainsi, si un joueur possède 250 crédits et la trésorerie 50, le coffre est de
300 R et le total couvert du ledger est de 300. Un paiement de 50 vers la
trésorerie produit 200 / 100 sans modifier ni le coffre ni le total couvert.

`m!economycheck` affiche le solde de trésorerie, les entrées et sorties
cumulées, le flux net, le nombre d'opérations institutionnelles et la
réconciliation. Les statistiques comptent uniquement les transactions
institutionnelles `COMPLETED`.

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

Les migrations SQLite sont additives et idempotentes. Au chargement, le cog :

1. crée `institution_initializations` si elle n'existe pas ;
2. ajoute `accounts.is_cash_backed INTEGER NOT NULL DEFAULT 1` aux bases
   Banking MVP existantes ;
3. crée une trésorerie couverte à zéro pour chaque institution déjà initialisée.

Les comptes V1 existants conservent leurs données et reçoivent la valeur
`is_cash_backed = 1`, ce qui préserve l'invariant déjà établi. Aucune donnée
existante n'est supprimée ni marquée automatiquement comme initialisée.

## Tests

Depuis l'environnement Red :

```bash
~/.venvs/redbot/bin/python -m unittest discover -s tests -v
```
