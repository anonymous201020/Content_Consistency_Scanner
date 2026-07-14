#!/usr/bin/env python3
"""
universal_consistency_scan.py

Single entry point that checks content against product-registry.yaml
(Layers 0+1+3 from the plan, WITHOUT the LLM semantic layer / Phase 3 -
no Anthropic key or any AI API required).

Three source types, any combination in one run:
  --github <repo_url>       clones the repo (shallow) and scans all
                             .md/.mdx/.html files
  --local <path>             scans an already-downloaded folder directly
                             (use this for a Google Drive link: download/
                             sync it to your laptop first, then point here)
  --live-urls <url> [<url> ...]  renders each URL with Playwright (handles
                             JS-rendered / SSR pages, not just static HTML)
                             and checks the final rendered content

Checks performed (all deterministic - no AI):
  - canonical vs. forbidden product names (case, spacing, legacy names)
  - brand-level forbidden terms (VPN/movpn) with scope/allow_in respected
  - JSON-LD schema name fields on live pages

Output: ONE markdown report file, written for a human (or Claude Code) to
action directly - each finding shows exactly where it is, what's wrong,
and what to change it to.

Usage:
    python3 universal_consistency_scan.py --registry product-registry.yaml \\
        --github https://github.com/your-org/content-repo \\
        --live-urls https://www.cosgrid.com/products/sdwan https://docs.cosgrid.com/qshield \\
        --out report.md
"""

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

CONTENT_EXTENSIONS = {".md", ".mdx", ".html", ".htm", ".txt"}
HEAVY_WEIGHT_TERMS = {"ReBaaNC", "VPN", "movpn"}


# --------------------------------------------------------------------------
# Registry loading (same as the other scripts - single source of truth)
# --------------------------------------------------------------------------

def load_registry(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_term_index(registry):
    """
    Returns list of dicts: {term, canonical, product_key, allow_in, weight}
    Same logic as drift_report.py, kept in sync intentionally.
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
                "weight": 3 if forbidden in HEAVY_WEIGHT_TERMS else 1,
            })

    brand = registry.get("brand", {})
    for rule in brand.get("global_rules", []) or []:
        for forbidden in rule.get("forbid", []) or []:
            terms.append({
                "term": forbidden,
                "canonical": None,
                "product_key": "brand.global_rules",
                "allow_in": rule.get("allow_in", []),
                "weight": 3 if forbidden in HEAVY_WEIGHT_TERMS else 1,
                "message": rule.get("message", ""),
            })

    company = brand.get("company", {})
    for forbidden in company.get("forbidden", []) or []:
        terms.append({
            "term": forbidden,
            "canonical": company.get("canonical"),
            "product_key": "brand.company",
            "allow_in": [],
            "weight": 1,
        })

    canonical_terms = []
    for key, entry in registry.get("products", {}).items():
        if entry.get("canonical"):
            canonical_terms.append(entry["canonical"])

    return terms, canonical_terms


def is_allowed_path(path_str, allow_globs):
    return any(fnmatch.fnmatch(path_str, glob) for glob in allow_globs)


def find_matches(text, term):
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")
    return list(pattern.finditer(text))


def context_snippet(text, start, end, radius=60):
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    snippet = text[lo:hi].replace("\n", " ")
    return f"...{snippet}..."


# --------------------------------------------------------------------------
# Source 1: GitHub repo
# --------------------------------------------------------------------------

def scan_github(repo_url, terms):
    tmp_dir = tempfile.mkdtemp(prefix="ccs_github_")
    print(f"Cloning {repo_url} ...")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, tmp_dir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    findings = scan_local_dir(tmp_dir, terms, label_prefix=f"[github:{repo_url}] ")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return findings


# --------------------------------------------------------------------------
# Source 2: local folder (also used for "downloaded from Drive" content)
# --------------------------------------------------------------------------

def scan_local_dir(path, terms, label_prefix=""):
    findings = []
    files_scanned = 0
    for file_path in Path(path).rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in CONTENT_EXTENSIONS:
            continue
        files_scanned += 1
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  ! could not read {file_path}: {e}", file=sys.stderr)
            continue

        rel_path = label_prefix + str(file_path.relative_to(path))

        for term_info in terms:
            if is_allowed_path(rel_path, term_info["allow_in"]):
                continue
            for match in find_matches(text, term_info["term"]):
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append({
                    "source": rel_path,
                    "location": f"line {line_no}",
                    "wrong_term": term_info["term"],
                    "correct_term": term_info["canonical"],
                    "context": context_snippet(text, match.start(), match.end()),
                })

    print(f"  {files_scanned} file(s) scanned in {path}")
    return findings


# --------------------------------------------------------------------------
# Source 3: live URLs via Playwright (handles JS-rendered / SSR pages)
# --------------------------------------------------------------------------

def scan_live_urls(urls, terms, canonical_terms):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "playwright not installed. Run:\n"
            "  pip install playwright --break-system-packages\n"
            "  playwright install chromium"
        )

    findings = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for url in urls:
            print(f"Rendering: {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                visible_text = page.inner_text("body")
            except Exception as e:
                print(f"  ! failed to load {url}: {e}", file=sys.stderr)
                findings.append({
                    "source": url,
                    "location": "page load",
                    "wrong_term": None,
                    "correct_term": None,
                    "context": f"ERROR: could not load page - {e}",
                })
                continue

            # text-based forbidden term matches
            for term_info in terms:
                if is_allowed_path(url, term_info["allow_in"]):
                    continue
                for match in find_matches(visible_text, term_info["term"]):
                    findings.append({
                        "source": url,
                        "location": "visible page text",
                        "wrong_term": term_info["term"],
                        "correct_term": term_info["canonical"],
                        "context": context_snippet(visible_text, match.start(), match.end()),
                    })

            # JSON-LD schema check
            jsonld_matches = re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE
            )
            for block in jsonld_matches:
                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    if name and name not in canonical_terms:
                        # is it a known forbidden variant with a suggestion?
                        suggestion = next((t["canonical"] for t in terms if t["term"] == name), None)
                        findings.append({
                            "source": url,
                            "location": "JSON-LD <script> block",
                            "wrong_term": name,
                            "correct_term": suggestion,
                            "context": f'"name": "{name}"',
                        })
        browser.close()

    return findings


# --------------------------------------------------------------------------
# Report generation - the single deliverable file
# --------------------------------------------------------------------------

def generate_report(all_findings, out_path, registry_path):
    by_source = {}
    for f in all_findings:
        by_source.setdefault(f["source"], []).append(f)

    lines = []
    lines.append("# Content Consistency Findings")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Registry used: `{registry_path}`")
    lines.append(f"Total findings: {len(all_findings)} across {len(by_source)} source(s)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Instructions for whoever (or whatever) fixes these")
    lines.append("")
    lines.append(
        "Each finding below shows an incorrect product name/term found in "
        "content, the correct name it should be per the official registry, "
        "and surrounding context to locate it. Replace the wrong term with "
        "the correct term wherever it appears, preserving surrounding "
        "formatting and tone. Do not change anything not listed below."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for source, findings in sorted(by_source.items()):
        lines.append(f"## `{source}`")
        lines.append("")
        lines.append(f"{len(findings)} finding(s)")
        lines.append("")
        for f in findings:
            lines.append(f"- **Location:** {f['location']}")
            if f["wrong_term"]:
                suggestion = f["correct_term"] if f["correct_term"] else "(no direct replacement - review manually, see brand rule)"
                lines.append(f"  **Wrong:** `{f['wrong_term']}` → **Correct:** `{suggestion}`")
            lines.append(f"  **Context:** {f['context']}")
            lines.append("")
        lines.append("---")
        lines.append("")

    if not all_findings:
        lines.append("No inconsistencies found. 🎉")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to: {out_path}")


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="product-registry.yaml")
    parser.add_argument("--github", nargs="*", default=[], help="one or more GitHub repo URLs")
    parser.add_argument("--local", nargs="*", default=[], help="one or more local folder paths")
    parser.add_argument("--live-urls", nargs="*", default=[], help="one or more live URLs (rendered via Playwright)")
    parser.add_argument("--out", default="report.md")
    args = parser.parse_args()

    if not (args.github or args.local or args.live_urls):
        print("ERROR: provide at least one of --github, --local, or --live-urls")
        sys.exit(1)

    registry = load_registry(args.registry)
    terms, canonical_terms = build_term_index(registry)
    print(f"Loaded {len(terms)} forbidden-term rules from {args.registry}\n")

    all_findings = []

    for repo_url in args.github:
        try:
            all_findings.extend(scan_github(repo_url, terms))
        except RuntimeError as e:
            print(f"ERROR scanning {repo_url}: {e}")

    for local_path in args.local:
        print(f"Scanning local folder: {local_path}")
        all_findings.extend(scan_local_dir(local_path, terms))

    if args.live_urls:
        try:
            all_findings.extend(scan_live_urls(args.live_urls, terms, canonical_terms))
        except RuntimeError as e:
            print(f"ERROR during live-site scan: {e}")

    generate_report(all_findings, args.out, args.registry)


if __name__ == "__main__":
    main()

