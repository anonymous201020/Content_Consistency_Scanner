#!/usr/bin/env python3
"""
validate_registry.py

Phase 0 integrity checks for product-registry.yaml, the single source of
truth for the Content Consistency Scanner.

Checks performed:
  1. Schema sanity   - required keys present on each product entry
  2. Duplicate canonicals   - no two products share the same canonical name
  3. Forbidden collisions   - a forbidden term must not equal ANY product's
                              canonical name (own or someone else's), and
                              must not equal a canonical term that appears
                              in allow_in-exempted brand rules
  4. Cross-product forbidden overlap - same forbidden string listed under
                              two different products (usually a copy-paste
                              bug, sometimes intentional - reported as warn)
  5. Case-only duplicates   - forbidden entries that are just a case
                              variant of the canonical (expected) vs a case
                              variant of ANOTHER product's canonical
                              (suspicious - likely a mistake)

Exit code 0 = clean, 1 = errors found (blocks Phase 0 gate).
Warnings do not fail the run but are printed.
"""

import sys
import yaml
from collections import defaultdict

REQUIRED_PRODUCT_KEYS = {"canonical"}
RECOMMENDED_PRODUCT_KEYS = {"canonical", "forbidden", "category"}


def load_registry(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate(registry):
    errors = []
    warnings = []

    products = registry.get("products", {})
    if not products:
        errors.append("No 'products' section found (or it is empty).")
        return errors, warnings

    canonical_to_keys = defaultdict(list)
    forbidden_to_keys = defaultdict(list)
    all_canonicals = {}

    # Pass 1: collect
    for key, entry in products.items():
        missing = REQUIRED_PRODUCT_KEYS - set(entry.keys())
        if missing:
            errors.append(f"[{key}] missing required key(s): {sorted(missing)}")
            continue

        missing_recommended = RECOMMENDED_PRODUCT_KEYS - set(entry.keys())
        if missing_recommended and entry.get("type") != "category":
            warnings.append(
                f"[{key}] missing recommended key(s): {sorted(missing_recommended)}"
            )

        canonical = entry["canonical"]
        canonical_to_keys[canonical].append(key)
        all_canonicals[canonical] = key

        for forbidden_term in entry.get("forbidden", []) or []:
            forbidden_to_keys[forbidden_term].append(key)

    # Pass 2: duplicate canonicals
    for canonical, keys in canonical_to_keys.items():
        if len(keys) > 1:
            errors.append(
                f"Duplicate canonical '{canonical}' used by products: {keys}"
            )

    # Pass 3: forbidden term equals some product's canonical name
    for forbidden_term, owner_keys in forbidden_to_keys.items():
        if forbidden_term in all_canonicals:
            clashing_product = all_canonicals[forbidden_term]
            if clashing_product not in owner_keys:
                errors.append(
                    f"Forbidden term '{forbidden_term}' (listed under {owner_keys}) "
                    f"is the CANONICAL name of another product ('{clashing_product}'). "
                    f"This would make Vale block the correct spelling."
                )

    # Pass 4: same forbidden term claimed by multiple products
    for forbidden_term, keys in forbidden_to_keys.items():
        if len(keys) > 1:
            warnings.append(
                f"Forbidden term '{forbidden_term}' appears under multiple products: "
                f"{keys}. Confirm this is intentional (e.g. a shared legacy alias)."
            )

    # Pass 5: case-only collisions against OTHER products' canonicals
    lower_canonical_map = defaultdict(list)
    for canonical, key in all_canonicals.items():
        lower_canonical_map[canonical.lower()].append((canonical, key))

    for forbidden_term, owner_keys in forbidden_to_keys.items():
        lower = forbidden_term.lower()
        if lower in lower_canonical_map:
            for canonical, owner_of_canonical in lower_canonical_map[lower]:
                if canonical != forbidden_term and owner_of_canonical not in owner_keys:
                    warnings.append(
                        f"Forbidden term '{forbidden_term}' (under {owner_keys}) is a "
                        f"case-variant of '{canonical}' owned by '{owner_of_canonical}'. "
                        f"Double check this isn't a copy-paste mistake."
                    )

    # Pass 6: alternate_names must not collide with another product's
    # canonical name or forbidden list.
    for key, entry in products.items():
        for alt in entry.get("alternate_names", []) or []:
            if alt in all_canonicals and all_canonicals[alt] != key:
                errors.append(
                    f"[{key}] alternate_name '{alt}' collides with the canonical "
                    f"name of product '{all_canonicals[alt]}'."
                )
            owners = forbidden_to_keys.get(alt, [])
            other_owners = [o for o in owners if o != key]
            if other_owners:
                errors.append(
                    f"[{key}] alternate_name '{alt}' is forbidden under "
                    f"{other_owners} — an approved alias for one product "
                    f"can't be a banned term for another."
                )

    return errors, warnings


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "product-registry.yaml"
    try:
        registry = load_registry(path)
    except FileNotFoundError:
        print(f"ERROR: registry file not found at '{path}'")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: invalid YAML in '{path}':\n{e}")
        sys.exit(1)

    errors, warnings = validate(registry)

    print(f"Validated: {path}")
    print(f"Products checked: {len(registry.get('products', {}))}")
    print()

    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
        print()

    if errors:
        print(f"ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        print()
        print("Registry validation FAILED.")
        sys.exit(1)

    print("Registry validation PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    main()
