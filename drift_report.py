#!/usr/bin/env python3
"""
drift_report.py

Phase 0 read-only drift report. Scans a directory of content files for
occurrences of each product's `forbidden` terms from product-registry.yaml,
honoring `allow_in` globs on brand-level rules and `scope` where relevant.

This intentionally does NOT require Vale - it's a fast first pass so you can
see the size of the problem (per the plan: "produce a read-only drift report
across all 244 files -> triage list") before Phase 1 sets up Vale properly.

Usage:
    python drift_report.py --registry product-registry.yaml --content ./content --ext .md .mdx .html

Output:
    Prints a grouped report to stdout and writes reports/drift_<timestamp>.json
"""

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml


def load_registry(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_term_index(registry):
    """
    Returns a list of dicts:
      {term, canonical, product_key, allow_in (list of globs), case_sensitive}
    """
    terms = []
    for key, entry in registry.get("products", {}).items():
        canonical = entry.get("canonical", "")
        for forbidden in entry.get("forbidden", []) or []:
            terms.append({
                "term": forbidden,
                "canonical": canonical,
                "product_key": key,
                "allow_in": [],
                "case_sensitive": True,
            })

    # Brand-level global_rules (e.g. VPN/movpn) with allow_in + scope
    brand = registry.get("brand", {})
    for rule in brand.get("global_rules", []) or []:
        for forbidden in rule.get("forbid", []) or []:
            terms.append({
                "term": forbidden,
                "canonical": None,
                "product_key": "brand.global_rules",
                "allow_in": rule.get("allow_in", []),
                "case_sensitive": True,
                "message": rule.get("message", ""),
            })

    company = brand.get("company", {})
    for forbidden in company.get("forbidden", []) or []:
        terms.append({
            "term": forbidden,
            "canonical": company.get("canonical"),
            "product_key": "brand.company",
            "allow_in": [],
            "case_sensitive": True,
        })

    return terms


def is_allowed_path(path_str, allow_globs):
    return any(fnmatch.fnmatch(path_str, glob) for glob in allow_globs)


def scan_files(content_dir, extensions, terms):
    findings = []
    files_scanned = 0

    for path in Path(content_dir).rglob("*"):
        if not path.is_file():
            continue
        if extensions and path.suffix.lower() not in extensions:
            continue
        files_scanned += 1

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  ! could not read {path}: {e}", file=sys.stderr)
            continue

        rel_path = str(path)

        for term_info in terms:
            term = term_info["term"]
            if not term:
                continue
            if is_allowed_path(rel_path, term_info["allow_in"]):
                continue

            # word-boundary, case-sensitive match on the exact forbidden string
            pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append({
                    "file": rel_path,
                    "line": line_no,
                    "forbidden_term": term,
                    "canonical": term_info["canonical"],
                    "product_key": term_info["product_key"],
                })

    return findings, files_scanned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="product-registry.yaml")
    parser.add_argument("--content", default="./content")
    parser.add_argument("--ext", nargs="*", default=[".md", ".mdx", ".html", ".txt"])
    args = parser.parse_args()

    extensions = {e.lower() for e in args.ext}
    registry = load_registry(args.registry)
    terms = build_term_index(registry)

    print(f"Loaded {len(terms)} forbidden-term rules from {args.registry}")
    print(f"Scanning {args.content} for extensions: {sorted(extensions)}")
    print()

    findings, files_scanned = scan_files(args.content, extensions, terms)

    print(f"Files scanned: {files_scanned}")
    print(f"Violations found: {len(findings)}")
    print()

    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)

    for file, hits in sorted(by_file.items()):
        print(f"{file}  ({len(hits)} hit(s))")
        for h in hits:
            suggestion = f" -> use '{h['canonical']}'" if h["canonical"] else ""
            print(f"    line {h['line']}: '{h['forbidden_term']}'{suggestion}")
    print()

    Path("reports").mkdir(exist_ok=True)
    out_path = Path("reports") / f"drift_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "files_scanned": files_scanned,
            "violation_count": len(findings),
            "findings": findings,
        }, f, indent=2)

    print(f"Full report written to: {out_path}")


if __name__ == "__main__":
    main()
