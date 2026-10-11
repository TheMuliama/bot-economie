# M Corp Admin Dashboard V1 — Phase 3.6

## Objet et architecture

Le dashboard est une interface interne, rendue côté serveur, réservée au
propriétaire du serveur Discord et aux rôles de modération configurés. Discord
reste l'interface de jeu.

```text
Navigateur
  -> FastAPI / routes minces
  -> session, CSRF et RBAC
  -> DashboardService
  -> EconomyControlService
  -> LedgerStore / InventoryService / ShopStore
  -> ledger.sqlite3
```

Les pages utilisent Jinja2 et le CSS livré dans `mcorp/dashboard/static`.
Aucune logique métier n'est exécutée dans le navigateur et aucun build Node
n'est nécessaire. Les routes web n'exécutent pas de SQL. Les écritures
économiques passent par `DashboardService`, puis `EconomyControlService`. Le
seul dépôt propre au dashboard est celui des planifications hebdomadaires ;
l'exécution d'une planification appelle toujours
`EconomyControlService.produce_item()`.

## Prérequis et installation

- Python 3.11 ;
- Red-DiscordBot 3.5.24 et son environnement virtuel ;
- accès en lecture/écriture au `ledger.sqlite3` persistant du cog ;
- une application Discord OAuth2.

Le paquet `mcorp` importe actuellement le cog Red depuis `mcorp/__init__.py`.
Le dashboard doit donc être lancé dans l'environnement Red existant :

```bash
cd /chemin/vers/bot-economie-admin-dashboard
source "$HOME/.venvs/redbot/bin/activate"
python -m pip install -r requirements.txt
python -m pip check
```

Sur l'instance locale `mcorp`, la base détectée est :

```text
/home/mlm/.local/share/Red-DiscordBot/data/mcorp/cogs/MCorp/ledger.sqlite3
```

Pour retrouver le chemin au lieu de le supposer :

```bash
find "$HOME/.local/share/Red-DiscordBot/data" -path '*/cogs/MCorp/ledger.sqlite3' -print
```

## Configuration Discord OAuth2

1. Ouvrir le [Discord Developer Portal](https://discord.com/developers/applications).
2. Créer ou sélectionner l'application destinée au dashboard.
3. Dans **OAuth2**, relever le Client ID et créer/régénérer le Client Secret.
4. Ajouter exactement cette redirection pour le développement local :
   `http://127.0.0.1:8000/auth/callback`.
5. Relever l'identifiant du serveur Discord cible (mode développeur Discord,
   clic droit sur le serveur, **Copier l'identifiant**).
6. Pour chaque rôle modérateur autorisé, copier son identifiant. Plusieurs IDs
   sont séparés par des virgules.

Le serveur construit lui-même l'URL d'autorisation avec les scopes :
`identify`, `guilds` et `guilds.members.read`. Au callback, il récupère
l'identité, confirme l'appartenance au serveur, détermine si l'utilisateur en
est le propriétaire, puis lit ses rôles. Le jeton d'accès Discord est utilisé
uniquement pendant le callback et n'est pas stocké dans la session.

## Variables d'environnement

Variables obligatoires :

```bash
export MCORP_DISCORD_CLIENT_ID='client-id-public'
export MCORP_DISCORD_CLIENT_SECRET='secret-oauth-discord'
export MCORP_DISCORD_REDIRECT_URI='http://127.0.0.1:8000/auth/callback'
export MCORP_DISCORD_GUILD_ID='identifiant-du-serveur'
export MCORP_MODERATOR_ROLE_IDS='role-id-1,role-id-2'
export MCORP_SESSION_SECRET="$(openssl rand -hex 32)"
export MCORP_DATABASE_PATH='/home/mlm/.local/share/Red-DiscordBot/data/mcorp/cogs/MCorp/ledger.sqlite3'
export MCORP_INSTITUTION_ID='mcorp:guild:IDENTIFIANT_DU_SERVEUR'
```

`MCORP_MODERATOR_ROLE_IDS` peut être vide si aucun modérateur ne doit entrer.
Ne jamais committer ces valeurs dans le dépôt.

Variables facultatives :

| Variable | Défaut | Rôle |
|---|---:|---|
| `MCORP_COOKIE_SECURE` | `true` | exige HTTPS pour envoyer le cookie |
| `MCORP_SCHEDULER_ENABLED` | `true` | active le worker hebdomadaire |
| `MCORP_SCHEDULER_INTERVAL_SECONDS` | `30` | intervalle de vérification |
| `MCORP_SESSION_MAX_AGE_SECONDS` | `3600` | durée maximale d'une session |

En HTTP local uniquement :

```bash
export MCORP_COOKIE_SECURE='false'
```

En production, conserver `MCORP_COOKIE_SECURE=true` et placer le service
derrière HTTPS (Caddy ou Nginx, par exemple).

## Lancement

Terminal Red :

```bash
source "$HOME/.venvs/redbot/bin/activate"
redbot mcorp
```

Terminal dashboard :

```bash
cd ~/Code/bot-economie
source "$HOME/.venvs/redbot/bin/activate"
uvicorn mcorp.dashboard.app:app --host 127.0.0.1 --port 8000
```

Un fichier `.env` placé à la racine du dépôt est chargé automatiquement s'il
existe. Il reste facultatif et ignoré par Git. Une variable déjà exportée dans
l'environnement garde toujours priorité sur la valeur du `.env`.

Ouvrir ensuite `http://127.0.0.1:8000`. Le healthcheck minimal est disponible
sur `http://127.0.0.1:8000/health`.

## Rôles et sécurité

- `ADMIN` est attribué uniquement lorsque Discord indique que l'utilisateur
  connecté est le propriétaire réel du serveur.
- `MODERATOR` est attribué lorsqu'au moins un rôle Discord de l'utilisateur
  figure dans `MCORP_MODERATOR_ROLE_IDS`.
- un membre ordinaire ou extérieur au serveur est refusé.
- `MODERATOR` dispose d'un accès en lecture aux métriques, boutiques, stocks,
  grossiste et audit. Les licences, permissions et politiques de restock sont
  consultables dans les pages Boutiques et Grossiste.
- toutes les mutations sont réservées à `ADMIN` dans la V1.

Les contrôles sont répétés côté serveur sur chaque route. La session est
signée, `HttpOnly`, `SameSite=Lax`, limitée dans le temps, et `Secure` par
défaut. Les formulaires mutatifs vérifient un jeton CSRF. L'état OAuth protège
le callback contre la substitution de session. Les pages d'administration
n'affichent jamais le client secret ni le secret de session.

## Pages et routes

| Page | Route principale | Accès |
|---|---|---|
| Connexion | `GET /login`, `GET /auth/discord`, `GET /auth/callback` | public/OAuth |
| Vue générale | `GET /` | ADMIN, MODERATOR |
| Boutiques | `GET /shops`, `GET /shops/{shop_id}` | ADMIN, MODERATOR |
| Stocks | `GET /stocks` | ADMIN, MODERATOR |
| Grossiste | `GET/POST /wholesale` | lecture tous, écriture ADMIN |
| Audit | `GET /audit` | ADMIN, MODERATOR |
| Administration | `GET /admin` | ADMIN |
| Santé | `GET /health` | public, données minimales |

Les actions boutique supplémentaires utilisent `POST /shops`,
`POST /shops/{shop_id}/configure` et
`POST /shops/{shop_id}/suspension`. La fiche boutique expose également le
paiement de licence, les droits d'achat et les politiques de restock. La page
Grossiste regroupe production manuelle, planifications hebdomadaires,
catalogue, approvisionnement et historique. Les anciennes URL de lecture
`/production`, `/permissions`, `/restock`, `/licenses` et `/alerts` redirigent
vers leur page fusionnée ; les routes POST historiques restent compatibles.
La déconnexion est un `POST /logout` protégé par CSRF.

## Réconciliation Red Bank

Le dashboard est un processus séparé et ne prétend pas pouvoir appeler Red
Bank directement. Les commandes Discord `m!tresorerie`, `m!bankcheck` et
`m!economycheck` enregistrent un snapshot du coffre Red et du ledger. La vue
générale montre le dernier snapshot, sa date et sa différence. En l'absence de
snapshot, une alerte l'indique explicitement.

Avant une validation web, exécuter :

```text
m!bankcheck
```

## Production hebdomadaire

Une règle contient un objet, une quantité, un jour `0..6`, une heure UTC, son
état, son créateur et les dates de dernière/prochaine exécution. Seule la
récurrence hebdomadaire est acceptée.

Le worker :

1. ouvre une transaction SQLite `BEGIN IMMEDIATE` ;
2. sélectionne les règles dues ;
3. crée un identifiant UUIDv5 déterministe à partir de
   `(schedule_id, scheduled_for)` ;
4. insère le run sous contrainte unique `(schedule_id, scheduled_for)` ;
5. avance `next_run_at` dans la même transaction ;
6. appelle `EconomyControlService.produce_item()` ;
7. journalise `SUCCEEDED` ou `FAILED`.

Deux workers ne peuvent donc pas revendiquer la même occurrence. Après une
indisponibilité de plusieurs semaines, une seule occurrence de rattrapage est
exécutée, puis la prochaine date future est calculée.

## Persistance et migrations

La Phase 3.6 ajoute sans destruction :

- `reconciliation_snapshots` ;
- `production_schedules` ;
- `production_schedule_runs`.
- `discord_identities`.

`discord_identities` mémorise le surnom Discord connu lors d'une connexion
OAuth. Les colonnes `imposed_nickname`, `previous_nickname` et
`rename_expires_at` réservent le minimum nécessaire aux futurs bons de
renommage, sans en implémenter le fonctionnement dans cette V1. En l'absence
de nom connu, l'interface affiche `Discord <ID>`.

Les tables de la Phase 3.5 (`shops`, licences, grossiste, permissions,
restock, historique et `control_events`) sont également initialisées si la
base déployée est plus ancienne. Toutes les créations utilisent
`CREATE TABLE IF NOT EXISTS`; les initialisations peuvent être rejouées. Aucun
`DROP TABLE`, vidage ou remplacement de base n'est effectué.

Une migration testée sur une copie de la base réelle a conservé tous les
comptages de lignes existants, retourné `PRAGMA integrity_check = ok` et zéro
violation à `PRAGMA foreign_key_check`.

Le bot et le dashboard écrivent dans le même fichier. SQLite sérialise les
écritures, mais une charge élevée peut produire `database is locked`. Cette
V1 doit rester sur un seul worker Uvicorn : ne pas ajouter `--workers`.

## Audit et alertes

Les actions dashboard sensibles ajoutent à `control_events` l'acteur Discord,
son rôle, l'action, la cible, les anciennes/nouvelles valeurs, l'horodatage et
la raison. La page Audit fusionne en lecture seule `control_events`,
`ledger_transactions` et `inventory_transactions`, puis filtre la vue unifiée
par acteur, utilisateur, boutique, type, date, statut ou identifiant de
transaction.

Les alertes calculées couvrent :

- snapshot de réconciliation absent ou coffre/ledger déséquilibré ;
- licence expirée ;
- stock M Corp inférieur au seuil statique de 5 ;
- restock bloqué avec raison métier ;
- événement `RECOVERY_REQUIRED`.

La structure retournée (`severity`, `kind`, `message`) pourra être réutilisée
plus tard pour des notifications Discord.

## Sauvegarde et restauration

Sauvegarde cohérente avec SQLite :

```bash
sqlite3 "$MCORP_DATABASE_PATH" ".backup '/chemin/sauvegardes/ledger-$(date +%F).sqlite3'"
```

Tester régulièrement la copie :

```bash
sqlite3 /chemin/sauvegardes/ledger-AAAA-MM-JJ.sqlite3 'PRAGMA integrity_check;'
```

Pour restaurer, arrêter Red et le dashboard, conserver une copie du fichier
endommagé, remettre la sauvegarde à son emplacement exact, vérifier les droits,
puis redémarrer Red avant le dashboard.

## Validation manuelle

1. Sauvegarder `ledger.sqlite3` avec `.backup`.
2. Pousser/déployer `feat/admin-dashboard` et installer les dépendances.
3. Redémarrer Red, recharger `mcorp`, puis lancer `m!bankcheck`.
4. Exporter la configuration et lancer Uvicorn sur `127.0.0.1:8000`.
5. Vérifier `/health`, puis la redirection Discord.
6. Confirmer qu'un membre normal est refusé.
7. Confirmer qu'un rôle configuré obtient `MODERATOR` et ne peut rien modifier.
8. Confirmer que le propriétaire obtient `ADMIN`.
9. Vérifier les six entrées Vue générale, Boutiques, Stocks, Grossiste, Audit
   et Administration.
10. Ouvrir une fiche boutique et vérifier licence, stock et approvisionnement.
11. Produire une petite quantité d'un objet et contrôler le stock et l'audit.
12. Créer puis modifier/désactiver une règle hebdomadaire et vérifier sa persistance après
    redémarrage.
13. Exécuter à nouveau `m!bankcheck` et confirmer une différence nulle.

## Limites connues de la V1

- Le dashboard doit actuellement utiliser l'environnement Python de Red, car
  `mcorp/__init__.py` importe le cog au chargement du paquet.
- La section Licence modifie les paramètres via la fiche boutique et déclenche le
  paiement, mais ne propose pas encore d'éditeur HTML du stock inclus.
- Seuls les utilisateurs qui se sont connectés au dashboard disposent d'un
  surnom connu stocké ; les autres propriétaires joueurs utilisent le fallback
  lisible `Discord <ID>`.
- Un arrêt brutal après la revendication d'un run et avant sa production peut
  laisser le run `RUNNING` et reporter la production à la semaine suivante.
  La clé unique empêche en priorité une double production.
- Les alertes sont calculées sur la Vue générale et ne sont pas encore envoyées
  vers Discord.
- Un seul worker Uvicorn est recommandé pour cette V1, même si la revendication
  des occurrences est protégée contre deux workers.
- HTTPS et le reverse proxy restent à configurer avant toute exposition réseau.

## Hors scope volontaire

- espace web pour les propriétaires de boutique ;
- `m!boutique`, `m!acheter`, boutons Discord et checkout joueur ;
- vente complète boutique vers joueur ;
- effets gameplay des objets ;
- frontend SPA ou build Node ;
- suppression de l'ancienne architecture et refactorisation Phase 3.7.
