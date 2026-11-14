"""
Remplit data/curated/controversies.csv à partir des extraits 10-K de fetch_10k.py, avec Claude
sur Amazon Bedrock. Le LLM PROPOSE des lignes ; le code les VÉRIFIE ; un humain les VALIDE.

    pip install "anthropic[bedrock]" pandas
    # identifiants AWS : profil, variables d'environnement ou rôle SageMaker (accès Bedrock requis)
    python scripts/fetch_10k.py --tickers XOM ENB TSLA --as-of 2026-11-12 --email ... --out data/external/kb_filings
    python scripts/extract_controversies.py --kb data/external/kb_filings --tickers XOM ENB TSLA \
        --ticker-map ENB=ENB.TO --region us-east-1

Contrôles déterministes sur chaque ligne proposée (sinon la ligne est rejetée et affichée) :
  - citation_en présente MOT POUR MOT dans le texte source (espaces/guillemets normalisés) ;
  - un montant non vide doit figurer dans la citation ;
  - categorie / gravite / section dans les listes permises ;
  - date_publication = date de DÉPÔT du 10-K (point-in-time), jamais une date choisie par le LLM.
Toutes les lignes écrites ont statut = "a_verifier" et reviseur vide. governance_check n'utilise
que les lignes "verifie" pour bloquer : un humain relit la citation, puis remplit
statut=verifie, reviseur, date_revision.

Test sans AWS : --from-json reponse.json valide une réponse déjà obtenue (format {"controversies": [...]}).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

MODEL = "anthropic.claude-opus-5-5"
FALLBACK_MODEL = "anthropic.claude-opus-4-8"
COLUMNS = ["ticker", "date_publication", "categorie", "description_fr", "citation_en", "gravite", "statut",
           "devise_montant", "montant", "source_doc", "section", "source_url", "reviseur", "date_revision"]
CATEGORIES = ["environnement", "social", "gouvernance", "securite_produit", "reglementaire", "autre"]
GRAVITES = ["mineure", "moderee", "majeure"]
SECTIONS = ["contingencies", "mdna_legal", "item3", "item1a"]

SCHEMA = {
    "type": "object",
    "properties": {
        "controversies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "categorie": {"type": "string", "enum": CATEGORIES},
                    "description_fr": {"type": "string"},
                    "citation_en": {"type": "string"},
                    "gravite": {"type": "string", "enum": GRAVITES},
                    "justification_gravite": {"type": "string"},
                    "devise_montant": {"type": "string", "enum": ["USD", "CAD", ""]},
                    "montant": {"type": ["number", "null"]},
                    "section": {"type": "string", "enum": SECTIONS},
                },
                "required": ["categorie", "description_fr", "citation_en", "gravite", "justification_gravite",
                             "devise_montant", "montant", "section"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["controversies"],
    "additionalProperties": False,
}

SYSTEM = """You extract ESG controversies from SEC annual report excerpts (10-K or 40-F) for an investment-suitability tool.
A controversy is a specific, named legal proceeding, government investigation, enforcement action,
verdict, settlement or allegation involving the company's conduct (environmental, social, governance,
product safety, regulatory). Generic risk-factor language ("we may be subject to litigation") is NOT a
controversy. One entry per distinct matter; merge follow-up developments of the same matter.

Rules:
- citation_en: copy ONE contiguous passage from the source, character for character (no ellipses, no
  paraphrase, no added words), 1-3 sentences, that best supports the entry. It is checked by exact match;
  entries whose quote is not found verbatim are discarded.
- montant: only an amount that appears inside citation_en (in full units, e.g. "$ 345 million" -> 345000000);
  otherwise null and devise_montant "".
- description_fr: one or two neutral sentences in French: who, what, current status. No speculation.
- gravite:
  * majeure: death or serious injury linked to the company's products/operations; a government agency
    alleging systemic misconduct (discrimination, fraud, environmental violations); a verdict, fine or
    settlement of USD 100 million or more; or allegations of misleading investors about core products.
  * moderee: other active lawsuits or investigations with a specific named matter, or amounts between
    USD 10 and 100 million.
  * mineure: resolved/dismissed matters with no lasting finding, or smaller amounts.
  justification_gravite: one short English sentence citing which criterion applies.
- Return an empty list if the excerpts contain no specific controversy."""


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[–—−]", "-", s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:%)])", r"\1", s)        # « $ 345 million , » ~ « $345 million, »
    s = re.sub(r"([$(])\s+", r"\1", s)
    return s.strip().casefold()


SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


def amounts_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion)?", text, flags=re.I):
        v = float(m.group(1).replace(",", ""))
        out.append(v * SCALE.get((m.group(2) or "").lower(), 1))
    return out


def validate(rows: list[dict], sources: dict[str, str]) -> tuple[list[dict], list[tuple[dict, str]]]:
    ok, bad = [], []
    normed = {k: norm(v) for k, v in sources.items()}
    for r in rows:
        q = norm(r.get("citation_en", ""))
        if r.get("categorie") not in CATEGORIES or r.get("gravite") not in GRAVITES:
            bad.append((r, "categorie/gravite hors liste")); continue
        if len(q) < 40:
            bad.append((r, "citation trop courte")); continue
        found = [s for s, t in normed.items() if q in t]
        if not found:
            bad.append((r, "citation introuvable mot pour mot dans la source")); continue
        r["section"] = r["section"] if r.get("section") in found else found[0]
        m = r.get("montant")
        if m not in (None, "", 0):
            if not any(abs(a - float(m)) <= 0.005 * float(m) for a in amounts_in(r["citation_en"])):
                bad.append((r, f"montant {m} absent de la citation")); continue
        else:
            r["montant"], r["devise_montant"] = None, ""
        ok.append(r)
    return ok, bad


def load_sources(kb: Path, ticker: str, include_item1a: bool):
    """Dernier exercice disponible pour ce ticker : {section: texte}, métadonnées."""
    files = sorted(kb.glob(f"{ticker}_*_contingencies.txt"), key=lambda p: p.name.split("_")[-2])  # 10K ou 40F
    if not files:
        return None, None
    stem = files[-1].name.replace("_contingencies.txt", "")
    secs = ["contingencies", "mdna_legal", "item3"] + (["item1a"] if include_item1a else [])
    sources = {s: (kb / f"{stem}_{s}.txt").read_text(encoding="utf-8") for s in secs if (kb / f"{stem}_{s}.txt").exists()}
    meta = json.loads((kb / f"{stem}_contingencies.txt.metadata.json").read_text(encoding="utf-8"))["metadataAttributes"]
    return {k: v for k, v in sources.items() if v.strip()}, meta


def call_claude(client, ticker: str, sources: dict[str, str]) -> list[dict]:
    doc = "\n\n".join(f'<excerpt section="{s}">\n{t}\n</excerpt>' for s, t in sources.items())
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=SYSTEM,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Company ticker: {ticker}\n\n{doc}\n\nExtract the controversies."}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        cat = resp.stop_details.category if resp.stop_details else None
        raise RuntimeError(f"refus du modèle (catégorie : {cat})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("réponse tronquée (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["controversies"]


def make_client(region: str):
    from anthropic import AnthropicBedrockMantle
    try:  # repli automatique sur un autre modèle si Claude refuse (non disponible côté serveur sur Bedrock)
        from anthropic import BetaRefusalFallbackMiddleware
        return AnthropicBedrockMantle(aws_region=region,
                                      middleware=[BetaRefusalFallbackMiddleware([{"model": FALLBACK_MODEL}])])
    except (ImportError, TypeError):
        print("(SDK sans BetaRefusalFallbackMiddleware : pas de repli en cas de refus)")
        return AnthropicBedrockMantle(aws_region=region)


def to_rows(items: list[dict], ticker_out: str, meta: dict) -> list[dict]:
    pub = str(meta["published_date"])
    return [{
        "ticker": ticker_out, "date_publication": f"{pub[:4]}-{pub[4:6]}-{pub[6:]}",
        "categorie": it["categorie"], "description_fr": it["description_fr"], "citation_en": it["citation_en"],
        "gravite": it["gravite"], "statut": "a_verifier", "devise_montant": it.get("devise_montant") or "",
        "montant": it.get("montant"), "source_doc": f"{meta.get('form', '10-K')} {meta['fiscal_period_end'][:4]} ({meta['accession']})",
        "section": it["section"], "source_url": meta["source_url"], "reviseur": "", "date_revision": "",
    } for it in items]


def merge(out: Path, new: pd.DataFrame) -> pd.DataFrame:
    """Remplace les lignes NON révisées du même ticker et du même document ; garde tout ce qu'un humain a validé."""
    if out.exists():
        old = pd.read_csv(out, dtype=str).fillna("")
        key = set(zip(new["ticker"], new["source_doc"]))
        stale = old.apply(lambda r: (r["ticker"], r["source_doc"]) in key and r["statut"] == "a_verifier", axis=1)
        reviewed = old[~stale]
        new = new[~new["citation_en"].isin(reviewed["citation_en"])]
        return pd.concat([reviewed, new], ignore_index=True)[COLUMNS]
    return new[COLUMNS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default="data/external/kb_filings", help="dossier de sortie de fetch_10k.py")
    ap.add_argument("--tickers", nargs="+", required=True, help="tickers SEC (ceux de fetch_10k.py)")
    ap.add_argument("--ticker-map", nargs="*", default=[], help="SEC=univers, ex. ENB=ENB.TO CNQ=CNQ.TO")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--out", default="data/curated/controversies.csv")
    ap.add_argument("--include-item1a", action="store_true", help="ajoute les facteurs de risque (long, souvent générique)")
    ap.add_argument("--from-json", help="valider une réponse JSON déjà obtenue (1 seul ticker), sans appeler Bedrock")
    a = ap.parse_args()
    tmap = dict(m.split("=", 1) for m in a.ticker_map)
    kb, out = Path(a.kb), Path(a.out)
    client = None if a.from_json else make_client(a.region)

    all_rows = []
    for t in a.tickers:
        sources, meta = load_sources(kb, t, a.include_item1a)
        if not sources:
            print(f"[{t}] aucun extrait dans {kb} : lancer fetch_10k.py d'abord"); continue
        try:
            items = (json.loads(Path(a.from_json).read_text(encoding="utf-8"))["controversies"] if a.from_json
                     else call_claude(client, t, sources))
        except Exception as e:  # un ticker en échec n'arrête pas les autres
            print(f"[{t}] ÉCHEC : {e}"); continue
        ok, bad = validate(items, sources)
        for r, why in bad:
            print(f"[{t}] REJETÉ ({why}) : {r.get('citation_en', '')[:100]!r}")
        rows = to_rows(ok, tmap.get(t, t), meta)
        for r in rows:
            print(f"[{t}] {r['gravite']:<8} {r['categorie']:<17} {r['description_fr'][:90]}")
        print(f"[{t}] {len(rows)} ligne(s) retenue(s), {len(bad)} rejetée(s)  ({meta.get('form', '10-K')} déposé {rows[0]['date_publication'] if rows else meta['published_date']})")
        all_rows += rows

    if not all_rows:
        print("Rien à écrire."); sys.exit(0 if a.from_json else 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = merge(out, pd.DataFrame(all_rows))
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"\n{out} : {len(df)} lignes. À FAIRE : relire chaque citation, puis statut=verifie, reviseur, date_revision.")


if __name__ == "__main__":
    main()
