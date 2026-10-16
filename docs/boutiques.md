# Boutiques joueur

`m!boutique` liste les boutiques publiques actives ; `m!boutique SHOP_ID` affiche les objets vendables, leur prix RTC et le stock. L'achat V1 est `m!acheter SHOP_ID objet quantité BANK|CASH`.

Un propriétaire configure d'abord l'autorisation de l'objet puis son prix joueur avec `m!shopsetsaleprice SHOP_ID objet prix`. Ce prix est distinct du prix grossiste, qui ne concerne que le restock.

Une vente vérifie à nouveau boutique, paiement accepté, prix, quantité et stock. BANK répartit le total entre boutique et commission M Corp ; CASH transfère le même total au coffre avant de créer les crédits couverts. Chaque achat a une référence, est idempotent et produit un mouvement d'inventaire ainsi qu'un événement d'audit.
