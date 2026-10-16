# RTC, cash et M Bank

Les RTC liquides vivent dans Red Bank. M Bank est un ensemble de comptes couverts dans le ledger ; le coffre Red de M Corp doit couvrir exactement leur total.

Un dépôt déplace du cash vers le coffre et crédite M Bank. Un retrait fait l'inverse. Un paiement BANK déplace seulement des engagements couverts : il ne crée pas de RTC. Lors d'un achat CASH, le cash rejoint d'abord le coffre puis le ledger crédite exactement le même total entre boutique et trésorerie.

`m!bankcheck` sert à constater l'invariant. Un statut `RECOVERY_REQUIRED` signifie qu'une compensation a échoué : ne pas poursuivre les opérations concernées avant analyse.
