# AlphaForge, jeu de données étendu et outils de risque

Ce dossier complète `alphaforge_starter/` **sans le modifier**. Il contient :

- les données préparées pour le calcul du risque (en CAD, calendrier aligné, point-in-time) ;
- les outils de risque (fonctions déterministes qui renvoient des **preuves**) ;
- les règles de gouvernance et les personas ;
- des tests ;
- des scripts pour aller chercher ce qui manque (10-K, composition des FNB).

> Simulation seulement. Rien ici n'est un conseil financier. Les rendements passés ne prédisent pas les rendements futurs.

## Structure

```
alphaforge_extended/
├── README_extended.md        ← ce fichier
├── requirements.txt
├── config/
│   ├── rules.yaml            ← seuils de gouvernance (hypothèses documentées)
│   └── personas.json         ← Amira / Mateo / Robert structurés depuis l'énoncé
├── data/
│   ├── provenance.csv        ← source, date, devise, traitement, limites de chaque fichier
│   ├── external/etf_bond_duration.csv
│   ├── curated/              ← généré par src/build_curated.py
│   └── templates/            ← gabarits vides : controversies, esg_activities
├── src/
│   ├── build_curated.py      ← régénère data/curated/ depuis alphaforge_starter/
│   ├── risk_tools.py         ← outils de risque (metrics, stress, replay, max_position…)
│   └── governance_check.py   ← vérification de référence contre rules.yaml
├── scripts/                  ← à lancer AVEC accès réseau (sec.gov)
│   ├── fetch_10k.py          ← 10-K ou 40-F (EDGAR) → risques / litiges / note des éventualités (+ métadonnées KB)
│   ├── extract_controversies.py ← extraits 10-K → controversies.csv (Claude sur Bedrock + vérification des citations)
│   ├── etf_holdings.py       ← composition SPY / XIU / QQQ / ESGV → curated/etf_holdings.csv
│   ├── esg_index_screen.py   ← SPY vs ESGV, XIU vs XCSR/XESG → curated/esg_index_screen.csv
│   ├── nbim_exclusions.py    ← liste d'exclusion du fonds norvégien → controversies.csv
│   └── nport_holdings.py     ← zip N-PORT trimestriel → external/nport_holdings.csv + nport_esg_screen.csv (optionnel)
└── tests/test_risk_tools.py
```

## Démarrage

```bash
cd alphaforge_extended
pip install -r requirements.txt
python src/build_curated.py --starter ../alphaforge_starter --out data/curated
python -m pytest -q tests/
```

```python
import sys, json; sys.path.insert(0, "src")
from risk_tools import RiskTools
from governance_check import check

rt = RiskTools("data/curated")
rt.metrics(["TLT", "ZST.TO"], start="2022-01-01", end="2022-12-31")
rt.stress({"XIU.TO": .3, "XBB.TO": .3, "NVDA": .2, "ZST.TO": .2}, "2022", capital=400_000)
rt.max_position(base_weights, "NVDA", max_drawdown_limit=0.15)
```

## Décisions de traitement

| Sujet | Décision |
|---|---|
| **Calendrier** | Union des jours de cotation TSX et US. Crypto échantillonnée sur ce calendrier : le rendement de fin de semaine est inclus le lundi, ce qui sous-estime légèrement sa volatilité quotidienne. |
| **Jours fériés propres à un marché** | Dernier prix reporté, donc rendement nul ce jour-là. Indicateur dans `filled_flags.csv`. |
| **Devise** | Tout est converti en **CAD** (USD/CAD de la Banque du Canada, dernier taux reporté les jours fériés canadiens). Les rendements en devise locale sont aussi fournis. |
| **Rendements** | Rendements simples sur `adj_close` : **rendement total**, dividendes réinvestis. |
| **CNQ, 2022-08-22** | `adj_close` conservé. L'écart avec `close` (~2 %) est cohérent avec une distribution spéciale traitée comme un dividende réinvesti. Le facteur 1,021 n'est **pas** réappliqué. À confirmer auprès des relations investisseurs de CNQ. |
| **Macro point-in-time** | `available_from` = date de disponibilité **estimée** : IPC américain fin de mois + 15 j, chômage américain + 7 j, IPC canadien + 21 j ; séries quotidiennes : jour ouvrable suivant. **À remplacer** par les vraies dates (ALFRED, calendrier de StatCan). Les valeurs FRED sont révisées. |
| **Biais du survivant** | L'univers a été choisi en 2026. Ne jamais présenter les rendements passés comme attendus. |

Détails et contrôles : `data/curated/quality_report.json`.

## Formules (`risk_tools.py`)

- **Volatilité annualisée** = écart-type des rendements quotidiens × √252 ; **volatilité à la baisse** = même calcul sur les rendements négatifs seulement.
- **Perte maximale** = pire baisse d'un sommet à un creux, avec les dates du sommet, du creux et du retour au sommet (durée de récupération).
- **VaR / CVaR 95 %** historiques, quotidiennes (et mensuelles dans `asset_risk_metrics.csv`).
- **Bêta** vs SPY et XIU.
- **Contribution au risque** = wᵢ(Σw)ᵢ / wᵀΣw ; **ratio de diversification** = volatilité moyenne pondérée / volatilité du portefeuille.
- **Concentration** : indice de Herfindahl Σw² et nombre effectif de positions 1/HHI. Décomposition des FNB si `holdings` est fourni.
- **Stress / replay** : allocation fixe, rééquilibrée chaque jour, rejouée sur la fenêtre (sans frais ni impôts).
- **max_position** : part maximale d'un actif (les autres positions réduites au prorata) qui garde la perte maximale du scénario sous la limite du profil.

## Format de preuve (contrat avec l'équipe agents)

Chaque outil renvoie :

```json
{"evidence_id": "ev-…", "tool": "stress", "inputs": {…}, "output": {…},
 "say": "En 2022 (…), ce portefeuille aurait fait -12,9 % (pire moment -21,1 %, en CAD)…",
 "currency": "CAD", "as_of": "…", "period": "2022-01-03/2022-12-30",
 "method": "…", "assumptions": ["…"], "data_sources": ["…"]}
```

- `say` est une phrase courte, prête à être dite par l'agent, en texte ou en voix.
- `as_of` coupe toutes les données postérieures.
- `evidence_id` est stable : mêmes entrées, même identifiant.

## Résultats déjà vérifiables

**Validation des données : duration empirique vs officielle** (`rate_sensitivity.csv`). La sensibilité des FNB obligataires aux variations de taux, mesurée sur nos données de 2021 à 2026, concorde avec la duration publiée par les émetteurs :

| FNB | Duration empirique | Duration officielle (nov. 2026) | R² |
|---|---|---|---|
| XBB.TO | 6,6 | 6,74 | 0,93 |
| AGG | 5,9 | 5,70 | 0,91 |
| TLT | 14,2 | 14,75 | 0,83 |
| ZST.TO | 0,2 | 0,38 | 0,17 |

Le R² faible de ZST est attendu : sa duration est presque nulle, donc le bruit domine.

**Scénario de démo, Robert** (portefeuille fictif dans `personas.json`, capital fictif de 400 000 $) :

- Portefeuille actuel en 2022 : -4,6 % (pire moment -10,1 %, soit -40 264 $). Gouvernance : **WARN** une fois les FNB décomposés : RY.TO = 10 % en direct + 2,9 % via XIU = 12,9 % (plafond 10 %). Sans `etf_holdings.csv` : PASS.
- Avec 20 % de NVDA : -12,9 % (pire moment -21,1 %, soit **-84 232 $**). Gouvernance : **BLOCK** (perte maximale et règle d'horizon court).
- `max_position` : avec sa limite de -15 %, NVDA peut aller jusqu'à environ **9 %**. Mais la règle d'horizon court (actifs à volatilité > 40 % ≤ 5 %) plafonne à **5 %**.

## Ce qui manque encore, et comment l'obtenir

| Besoin | Statut | Comment |
|---|---|---|
| Composition des FNB (SPY, XIU, QQQ, ESGV) | ✅ | `scripts/etf_holdings.py` → `curated/etf_holdings.csv` (SPY : State Street ; XIU : iShares Canada ; QQQ, ESGV : N-PORT SEC ; secteurs GICS via l'univers, iShares et la liste S&P 500). Utilisé automatiquement par `concentration`, `lookthrough` et `governance_check`. Obligations et GLD non décomposés. |
| Filtre des indices ESG | ✅ | `scripts/esg_index_screen.py` → `curated/esg_index_screen.csv` : SPY vs ESGV, XIU vs XCSR (et XESG). Comparaison par ticker (classes d'actions normalisées). Dit QUI est exclu, pas POURQUOI : formuler « exclue de l'indice X ». Utilisé par `governance_check` (contrôle `PREFERENCE_ESG_INDICE`, **WARN seulement**, si une préférence du profil mentionne « ESG »). |
| Liste d'exclusion NBIM (Norvège) | ✅ | `scripts/nbim_exclusions.py` → `external/nbim_exclusions.csv` + critères de conduite dans `curated/controversies.csv` (a_verifier). |
| Controverses (XOM, ENB, CNQ) | ⚠️ outil prêt | `scripts/fetch_10k.py`, puis `scripts/extract_controversies.py` (Claude sur Bedrock) → `curated/controversies.csv`. Chaque citation est vérifiée mot pour mot par le code ; les lignes arrivent en `a_verifier` et **seules les lignes relues (`statut=verifie`, `reviseur`) peuvent bloquer** dans `governance_check`. |
| Activités ESG (revenus par segment) | ⚠️ contrôle prêt, données à saisir | 10-K / AIF → `templates/esg_activities_template.csv` → `curated/esg_screen.csv` (`part_revenus` en fraction, `reviseur` obligatoire pour compter). Activer par profil : `{"type": "esg_activity", "value": "combustibles_fossiles"}`. Règle dans `rules.yaml` → `esg_activity` : BLOCK en direct si ≥ 10 % des revenus, WARN via FNB au-delà de 1 %. Aucun profil ne l'active pour l'instant. |
| Historique plus long (2008, 2020) | ❌ | Modifier `START` dans `alphaforge_starter/build_dataset.py` (par ex. 2007-01-01). Seuls les FNB anciens (SPY, XIU, TLT, AGG, GLD, XBB) auront cet historique. |
| Vraies dates de publication macro | ⚠️ hypothèses | ALFRED (FRED) et calendrier de StatCan → remplacer `available_from`. |

Tant que les fichiers ESG et de controverses sont absents, `governance_check` renvoie **`NON_VERIFIE`**, jamais `PASS`.

## Limites à afficher dans la démo

- Un seul scénario de crise dans les données actuelles (2022).
- Durations actuelles, pas historiques.
- Délais de publication macro estimés.
- Biais du survivant.
- Composition des FNB ACTUELLE (nov. 2026 pour SPY/XIU, juin et août 2026 pour QQQ/ESGV), appliquée à toutes les dates ; secteurs GICS actuels. Sans `etf_holdings.csv`, un FNB compte comme une seule position.
