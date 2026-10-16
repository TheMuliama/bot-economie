# Inventaires

Le catalogue versionné définit les types d'objet ; les quantités SQLite appartiennent à `SYSTEM`, `SHOP` ou `USER`. La production crée officiellement des objets, le restock les déplace de l'entrepôt vers une boutique et une vente les déplace de la boutique vers le joueur.

Les objets ordinaires utilisent cette logique générique. Les effets spécialisés, durabilité et `item_instances` ne sont pas implémentés en V1.
