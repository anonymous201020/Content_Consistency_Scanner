#!/usr/bin/env python3
"""
site_consistency_crawl.py

LAYER 3. Applies product-registry.yaml to *live* pages (www, docs, llms.txt)
instead of repo content files. Produces a consistency_score per surface,
per the formula in the plan (section 6.2):

    name_adherence     = canonical mentions / total product mentions   (0.40)
    forbidden_free      = 1 - (weighted forbidden hits / total mentions) (0.30)
    desc_consistency    = LLM-checked description match                (0.20)  <- Phase 3, PENDING
    schema_consistency  = JSON-LD name/description == registry          (0.10)

    consistency_score = 100 * sum(weight * metric)

IMPORTANT: desc_consistency requires the Phase 3 semantic checker (an
Anthropic API call) which is not wired up yet pending your CEO's decision on
API key ownership. Until then, this script:
  - computes the other 3 metrics for real
  - marks desc_consistency as PENDING in the output (does not fake a score)
  - reports an "interim_score" that redistributes desc_consistency's weight
    proportionally across the other 3, clearly labeled as interim

Accepts either live URLs (http/https, fetched with requests) or local HTML
files (for testing without hitting a real site) - anything not starting
with http is treated as a local file path.

Usage:
    python3 site_consistency_crawl.py --registry product-registry.yaml --urls https://example.com/products/ztna https://example.com/products/sdwan
    python3 site_consistency_crawl.py --registry product-registry.yaml --url-file urls.txt
    python3 site_consistency_crawl.py --registry product-registry.yaml --urls ./test_pages/page1.html   (local test file)
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

WEIGHTS = {
    "name_adherence": 0.40,
    "forbidden_free": 0.30,
    "desc_consistency": 0.20,
    "schema_consistency": 0.10,
}

HEAVY_WEIGHT_TERMS = {"ReBaaNC", "VPN", "movpn"}  # weighted 3x per the plan
HEAVY_MULTIPLIER = 3


def load_registry(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_term_maps(registry):
    canonical_terms = []       # list of canonical strings
    forbidden_terms = {}       # forbidden_string -> canonical it should be
    canonical_lookup = {}      # product_key -> canonical

    for key, entry in registry.get("products", {}).items():
        canonical = entry.get("canonical")
        if canonical:
            canonical_terms.append(canonical)
            canonical_lookup[key] = canonical
        for forbidden in entry.get("forbidden", []) or []:
            forbidden_terms[forbidden] = canonical

    brand = registry.get("brand", {})
    for rule in brand.get("global_rules", []) or []:
        for forbidden in rule.get("forbid", []) or []:
            forbidden_terms[forbidden] = None  # no direct canonical swap (e.g. VPN)

    return canonical_terms, forbidden_terms, canonical_lookup


def fetch_html(source):
    if source.startswith("http://") or source.startswith("https://"):
        resp = requests.get(source, timeout=15, headers={"User-Agent": "COSGrid-consistency-crawler/1.0"})
        resp.raise_for_status()
        return resp.text
    else:
        return Path(source).read_text(encoding="utf-8", errors="ignore")


def extract_jsonld(soup):
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
            blocks.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return blocks


def count_term_occurrences(text, term):
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")
    return len(pattern.findall(text))


def score_page(source, registry, canonical_terms, forbidden_terms, canonical_lookup):
    html = fetch_html(source)
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(separator=" ")

    # --- name_adherence ---
    canonical_hits = 0
    forbidden_hits_weighted = 0
    forbidden_hits_raw = 0
    findings = []

    for canonical in canonical_terms:
        n = count_term_occurrences(visible_text, canonical)
        canonical_hits += n

    for forbidden, canonical in forbidden_terms.items():
        n = count_term_occurrences(visible_text, forbidden)
        if n > 0:
            weight = HEAVY_MULTIPLIER if forbidden in HEAVY_WEIGHT_TERMS else 1
            forbidden_hits_weighted += n * weight
            forbidden_hits_raw += n
            findings.append({
                "term": forbidden,
                "count": n,
                "suggested_canonical": canonical,
            })

    total_mentions = canonical_hits + forbidden_hits_raw
    if total_mentions == 0:
        name_adherence = 1.0  # no product mentions found on this page - not a penalty
    else:
        name_adherence = canonical_hits / total_mentions

    if total_mentions == 0:
        forbidden_free = 1.0
    else:
        forbidden_free = max(0.0, 1 - (forbidden_hits_weighted / total_mentions))

    # --- schema_consistency (JSON-LD) ---
    jsonld_blocks = extract_jsonld(soup)
    schema_checks = 0
    schema_matches = 0
    schema_findings = []

    for block in jsonld_blocks:
        candidates = block if isinstance(block, list) else [block]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            schema_checks += 1
            if name in canonical_terms:
                schema_matches += 1
            else:
                # is it a known forbidden variant?
                suggestion = forbidden_terms.get(name)
                schema_findings.append({
                    "jsonld_name": name,
                    "matches_canonical": False,
                    "suggested_canonical": suggestion,
                })

    schema_consistency = (schema_matches / schema_checks) if schema_checks > 0 else 1.0

    # --- desc_consistency: PENDING (Phase 3 / LLM required) ---
    desc_consistency = None

    metrics = {
        "name_adherence": round(name_adherence, 3),
        "forbidden_free": round(forbidden_free, 3),
        "schema_consistency": round(schema_consistency, 3),
        "desc_consistency": "PENDING (Phase 3 not yet enabled)",
    }

    # Interim score: redistribute desc_consistency's 0.20 weight across the
    # other 3 proportionally, so the interim number isn't silently treating
    # "unknown" as "perfect".
    available_weight = WEIGHTS["name_adherence"] + WEIGHTS["forbidden_free"] + WEIGHTS["schema_consistency"]
    interim_score = 100 * (
        (WEIGHTS["name_adherence"] / available_weight) * name_adherence
        + (WEIGHTS["forbidden_free"] / available_weight) * forbidden_free
        + (WEIGHTS["schema_consistency"] / available_weight) * schema_consistency
    )

    return {
        "source": source,
        "total_product_mentions": total_mentions,
        "canonical_mentions": canonical_hits,
        "forbidden_mentions": forbidden_hits_raw,
        "metrics": metrics,
        "interim_score": round(interim_score, 1),
        "note": "interim_score excludes desc_consistency (Phase 3 pending) and redistributes its weight across the other 3 metrics",
        "forbidden_findings": findings,
        "schema_findings": schema_findings,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="product-registry.yaml")
    parser.add_argument("--urls", nargs="*", default=[])
    parser.add_argument("--url-file", default=None)
    args = parser.parse_args()

    sources = list(args.urls)
    if args.url_file:
        sources.extend(
            line.strip() for line in Path(args.url_file).read_text().splitlines() if line.strip()
        )

    if not sources:
        print("ERROR: provide --urls or --url-file")
        sys.exit(1)

    registry = load_registry(args.registry)
    canonical_terms, forbidden_terms, canonical_lookup = build_term_maps(registry)

    results = []
    for source in sources:
        print(f"Scanning: {source}")
        try:
            result = score_page(source, registry, canonical_terms, forbidden_terms, canonical_lookup)
            results.append(result)
            print(f"  interim_score: {result['interim_score']}/100  "
                  f"(name_adherence={result['metrics']['name_adherence']}, "
                  f"forbidden_free={result['metrics']['forbidden_free']}, "
                  f"schema_consistency={result['metrics']['schema_consistency']})")
            for f in result["forbidden_findings"]:
                suggestion = f" -> use '{f['suggested_canonical']}'" if f["suggested_canonical"] else ""
                print(f"    [{f['count']}x] '{f['term']}'{suggestion}")
        except Exception as e:
            print(f"  ERROR scanning {source}: {e}")
            results.append({"source": source, "error": str(e)})
        print()

    Path("reports").mkdir(exist_ok=True)
    out_path = Path("reports") / f"site_consistency_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"pages": results}, f, indent=2)

    valid_scores = [r["interim_score"] for r in results if "interim_score" in r]
    if valid_scores:
        overall = round(sum(valid_scores) / len(valid_scores), 1)
        print(f"Overall interim consistency score across {len(valid_scores)} page(s): {overall}/100")
        print("(This EXCLUDES desc_consistency - full score needs Phase 3 wired up)")

    print(f"Full report: {out_path}")


if __name__ == "__main__":
    main()
