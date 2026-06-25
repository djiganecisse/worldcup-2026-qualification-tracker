# Contribuer

Merci de passer ! Ce projet est volontairement **simple** : un seul fichier Python,
**zéro dépendance** (bibliothèque standard uniquement). Gardons-le comme ça autant que
possible.

## Lancer en local

```bash
git clone https://github.com/djiganecisse-debug/worldcup-2026-qualification-tracker.git
cd worldcup-2026-qualification-tracker
python3 senegal_wc_tracker.py --dry-run        # calcule et affiche, n'envoie rien
```

Aucune clé n'est nécessaire pour le mode après-match. Pour le live et les notifications,
voir le `README` (variables `FOOTBALL_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

## Lancer les tests

```bash
python3 -m unittest discover -s tests -v
```

Les tests couvrent la logique pure (classement, départages FIFA, seuils) sans réseau.

## Proposer une contribution

1. Ouvre une issue (ou prends une [`good first issue`](../../issues)) pour qu'on cale
   l'idée avant que tu codes.
2. Fork → branche → PR vers `main`.
3. Garde les tests verts (`python3 -m unittest discover -s tests`) et ajoute-en si tu
   touches à la logique.
4. Décris dans la PR **ce que ça change et pourquoi**, avec un avant/après si pertinent.

## Style

- **Une seule dépendance autorisée : la bibliothèque standard.** Pas de `pip install`.
- Garde le tout en un fichier tant que c'est lisible.
- Commentaires en français ou anglais, au choix.
- Pas de secret en dur. Jamais. Tout passe par variable d'environnement.

## Précision avant tout

C'est un modèle, pas un oracle. Si une PR change les probabilités, explique l'hypothèse
et, si possible, montre que c'est mieux calibré (pas juste « ça me semble plus juste »).
