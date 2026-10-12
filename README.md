# M Corp Bot

Cog Red-DiscordBot pour les mécaniques économiques de M Corp.

## Banking MVP V1

Red Bank est l'unique source de vérité des **R-crédits**. Le compte Red Bank
du bot est le coffre M Corp. Le ledger M Corp représente exclusivement les
passifs couverts par ce coffre : un crédit bancaire doit toujours correspondre
à un R-crédit détenu dans le coffre.

Le ledger et son journal sont stockés dans `ledger.sqlite3`, sous le répertoire
de données de Red pour le cog (`cog_data_path(MCorp)`). SQLite a été retenu
pour ses transactions locales et ses requêtes d'historique. L'ancienne base
standalone `mcorp.db` a été archivée avant son retrait du dépôt de travail.

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

## Interface d'argent — Phase 3

L'interface utilisateur distingue deux concepts simples :

- **argent sur soi** : le solde Red Bank ; Red reste son unique source de
  vérité ;
- **compte bancaire** : le solde du ledger couvert M Corp.

`m!argent` les affiche ensemble. `m!inventaire` rappelle aussi le liquide pour
l'expérience utilisateur, mais ne l'enregistre jamais dans les tables
d'inventaire.

Les transferts ont deux significations volontairement distinctes :

- `m!donner @membre 100R` est un don direct de liquide Red Bank ;
- `m!donner @membre immunity [quantité]` est un transfert d'objet ;
- `m!payer @membre 100` est un paiement entre comptes bancaires M Corp ;
- `m!payer mcorp 100` est un paiement bancaire vers la trésorerie M Corp.

La forme liquide est strictement `100R` (sans espace) afin de ne pas être
ambiguë avec un identifiant d'objet. Les dons de liquide ne figurent pas dans
l'historique bancaire utilisateur : ils sont toutefois consignés dans le
journal interne du ledger (`PLAYER_CASH_TRANSFER`). Les transferts d'objets
disposent de leur propre journal d'inventaire.

## Commandes

- `m!argent` : argent sur soi et compte bancaire.
- `m!inventaire` : liquide Red affiché et objets possédés.
- `m!depot <montant>` : transfert du portefeuille Red vers le coffre et crédit
  du ledger.
- `m!retrait <montant>` : opération inverse.
- `m!donner @membre <100R|objet> [quantité]` : don direct de liquide ou objet.
- `m!payer @membre <montant>` : transfert entre comptes M Corp, sans mouvement
  du coffre.
- `m!payer mcorp <montant>` : transfert bancaire interne vers la trésorerie.
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
- `m!itemgrant @membre <objet> [quantité]`, `m!itemremove @membre <objet>
  [quantité]`, `m!iteminspect @membre` : administration owner-only minimale de
  l'inventaire.

Les commandes historiques `m!solde`, `m!compte`, `m!virement` et
`m!payermcorp` restent temporairement disponibles et indiquent la commande de
remplacement. **Breaking change :** `m!payer` ne transfère plus de liquide ;
utilisez désormais `m!donner @membre 100R`.

## Inventaire générique — Phase 3

Le catalogue versionné dans `mcorp/catalog.py` répond à « quels objets
existent ? ». Il définit aujourd'hui `ticket_silence`, `immunity`, `maro` et
`rename`, avec `display_name`, description, transférabilité, empilabilité et
consommabilité. Aucun effet gameplay n'est implémenté.

L'inventaire répond séparément à « qui possède combien ? ». Chaque ligne est
identifiée par `(owner_type, owner_id, item_id)`. Les types utilisés sont
`USER`, `SYSTEM` et `SHOP`; le schéma accepte de futurs types tels que
`COMPANY` ou `NPC` sans migration. Par exemple, le backend peut gérer
`SYSTEM/M_CORP` et `SHOP/GENERAL_STORE`, mais aucune boutique ou prix n'est
encore exposé.

Les tables `inventory_items` et `inventory_transactions` vivent dans le même
`ledger.sqlite3` persistant du cog : il n'y a donc pas de second système de
persistance. Elles sont indépendantes des tables de compte bancaire. Un
transfert exécute débit source, crédit destination et écriture `ITEM_TRANSFER`
dans une unique transaction SQLite. Les opérations administratives écrivent
`ITEM_ADMIN_GRANT` ou `ITEM_ADMIN_REMOVE`; les types futurs
`ITEM_PURCHASE`, `ITEM_USE` et `ITEM_RESTOCK` sont réservés au journal.

Les quantités sont strictement positives à l'entrée et protégées par
`CHECK (quantity >= 0)` en base. Un objet non transférable est refusé ; un
objet non empilable ne peut exister qu'en un exemplaire par inventaire. Les
migrations sont additives via `CREATE TABLE IF NOT EXISTS` : les données du
ledger existant sont préservées. L'ancien bot standalone et sa base prototype
ont été archivés puis retirés pendant le nettoyage architectural de la V1.

## Control plane économique — Phase 3.5

La répartition des rôles est désormais explicite :

- **M Corp** produit les objets communs, tient l'entrepôt `SYSTEM/M_CORP`,
  exploite le catalogue grossiste et reçoit licences, commissions et frais ;
- une **boutique** est une entité économique indépendante avec compte bancaire
  couvert `SHOP/CHECKING`, inventaire `SHOP`, licence et politiques propres ;
- le **joueur** reste le futur consommateur. Aucun parcours `m!boutique` ou
  `m!acheter` n'est encore exposé.

Le module `mcorp/shop_store.py` ajoute, dans le même `ledger.sqlite3`, des tables
additives pour les boutiques, stocks inclus dans les licences, prix grossistes
et leur historique, permissions par objet, politiques de restock et événements
de contrôle. `mcorp/control.py` fournit `EconomyControlService`, façade métier
destinée aux commandes Discord puis au dashboard web. Un futur dashboard doit
appeler cette façade et ne jamais écrire directement dans SQLite.

### Boutiques, licences et paiement

Une boutique conserve son propriétaire, son statut (`ACTIVE`, `SUSPENDED`,
`LICENSE_EXPIRED`, `CLOSED`), sa visibilité, ses méthodes de paiement
(`BANK`/`CASH`), ses frais en points de base et une politique d'accès JSON
extensible. Une licence peut avoir un prix, une période et un stock inclus.
Le paiement transfère le coût du compte couvert de la boutique vers la
trésorerie, puis déplace réellement le stock de l'entrepôt vers la boutique.
Un manque de permission ou de stock empêche l'opération ; un échec intermédiaire
déclenche une compensation et peut produire `RECOVERY_REQUIRED`.

La boutique officielle `M_CORP_STORE` utilise exactement le même modèle avec
licence, commission et frais de restock à zéro. Sa licence est
`NOT_REQUIRED`.

### Production, grossiste et restock

`produce_item()` crédite l'entrepôt et journalise `ITEM_PRODUCTION`, ce qui
reste distinct d'un `ITEM_ADMIN_GRANT`. Le catalogue grossiste persistant
indique prix, disponibilité et limites de commande ; chaque changement de prix
est historisé avec acteur et raison. Les permissions `SHOP_ITEM_ALLOWED` et
`SHOP_ITEM_DENIED` sont spécifiques à chaque boutique.

Une règle de restock contient seuil, stock cible, prix unitaire maximal, budget
maximal de période et dépense courante. Le devis calcule :

```text
coût du stock + frais de restock = coût total boutique
```

Dans la V1, `spent_in_period` est un compteur cumulatif : aucun calendrier ni
reset de période n'est encore défini. La Phase 3.7 conserve ce comportement ;
définir la période et sa remise à zéro reste une décision fonctionnelle future.

Le moteur vérifie statut/licence, activation, seuil, permission, disponibilité,
limites, prix maximal, budget, compte boutique et stock M Corp. L'argent est
ensuite transféré vers la trésorerie et le stock vers la boutique. SQLite ne
fournissant pas une transaction distribuée entre les deux dépôts, le second
échec rembourse le compte boutique ; un échec de compensation est audité.

La future vente dispose déjà de `calculate_sale_split()` et
`apply_sale_split()`. Cette dernière ventile atomiquement dans le ledger un
paiement couvert entre compte boutique et trésorerie, sans implémenter le
checkout joueur ni le transfert de marchandise de Phase 4.

### Commandes administratives temporaires

- `m!shopcreate SHOP_ID @propriétaire Nom de la boutique`
- `m!shopinfo SHOP_ID`
- `m!shoplicense SHOP_ID`
- `m!shopfund SHOP_ID montant` (allocation couverte depuis la trésorerie)
- `m!shopsetprice item prix [raison]`
- `m!shopallowitem SHOP_ID item`
- `m!shopdenyitem SHOP_ID item`
- `m!shopsetrestock SHOP_ID item seuil cible prix_max budget_max`
- `m!shoprestock SHOP_ID item`
- `m!produce item quantité`

Toutes sont owner-only. Les rapports Python exposent les montants couverts par
type de propriétaire, la trésorerie, les stocks/productions par objet, ainsi
que le compte, la licence, le stock, les accès grossistes et les politiques de
chaque boutique. Les événements de contrôle sont récupérables via
`ShopStore.recent_events()` derrière le service administratif.

Les migrations utilisent exclusivement `CREATE TABLE IF NOT EXISTS` et ne
suppriment ni ne réécrivent les données Phase 3. Le prototype autonome reste
hors circuit.

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

Les opérations couvertes (`m!depot`, `m!retrait`, `m!payer`) sont refusées
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
