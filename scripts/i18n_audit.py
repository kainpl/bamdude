"""Find unused i18n leaf keys in frontend/src/i18n/locales/{en,uk}.ts.

Treat a key as USED when any of these match anywhere in frontend/src/**/*.{ts,tsx}:

1. Literal: t('foo.bar') / t("foo.bar") / t(`foo.bar`)
2. Template-prefix: t(`foo.${something}`) → keeps every `foo.*` leaf
3. Plural sibling: a literal hit on `foo.bar` keeps `foo.bar_one/_other/_zero/_few/_many/_two`
4. Trans component: <Trans i18nKey="foo.bar" /> / i18nKey={'foo.bar'}

Backend i18n JSONs (telegram_ui_*, notification_templates_*, maintenance_types_*)
are loaded as bulk records by locale_updater.py — not surveyed here.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = ROOT / "frontend" / "src" / "i18n" / "locales"
SRC_DIR = ROOT / "frontend" / "src"

PLURAL_SUFFIXES = ("_zero", "_one", "_two", "_few", "_many", "_other")


# -------- Step 1: parse en.ts into a flat list of leaf-key paths --------


def load_keys() -> tuple[list[str], list[str]]:
    """Use Node to evaluate the .ts modules — far more reliable than home-rolling
    a TS-object → JSON converter on a 4900-line file with embedded newlines and
    template literals in values."""
    script = """
const en = require('./frontend/src/i18n/locales/en.ts').default;
const uk = require('./frontend/src/i18n/locales/uk.ts').default;
function flatten(o, p='') {
  const r = [];
  for (const [k,v] of Object.entries(o)) {
    const path = p ? p+'.'+k : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) r.push(...flatten(v, path));
    else r.push(path);
  }
  return r;
}
process.stdout.write(JSON.stringify({en: flatten(en), uk: flatten(uk)}));
"""
    out = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True)
    data = json.loads(out)
    return data["en"], data["uk"]


# -------- Step 2: scan source for t() / <Trans /> hits --------
#
# Direct t('foo.bar') is one signal, but BamDude routes many keys through
# variable indirection: `labelKey: 'nav.archives'` then `t(labelKey)`,
# `stateMap[state]` lookup tables that resolve to a key string at runtime,
# `t?.('time.daysAgo', {count})` with optional chaining the simple regex
# misses, etc. The reliable cross-cutting check is: does the literal key
# string appear ANYWHERE in any source file? If yes, it's referenced.

# Matches: t(`foo.bar.${ident}.baz`) -> captures dotted prefix before ${}
PREFIX_T = re.compile(r"""\bt\(\s*`([a-zA-Z0-9_.-]+)\.\$\{""")

# Matches: i18nKey="foo.bar"  /  i18nKey={'foo.bar'}  with template-prefix
TRANS_PREFIX = re.compile(r"""i18nKey\s*=\s*\{?\s*`([a-zA-Z0-9_.-]+)\.\$\{""")


def _gather_source_text() -> str:
    """Concatenate every .ts/.tsx in frontend/src (minus the locale files
    themselves). Returns one giant text blob — fast string-in-blob lookups
    beat per-file regex passes for our 4k-key search space."""
    chunks: list[str] = []
    for path in SRC_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in (".ts", ".tsx"):
            continue
        if "i18n/locales" in path.as_posix():
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def scan_sources(en_keys: list[str]) -> tuple[set[str], set[str]]:
    """Return (literal_hits, prefix_hits).

    literal_hits: keys whose exact dotted path appears in any source file as
    a quoted string ('foo.bar' | "foo.bar" | `foo.bar`).

    For pluralized keys (`foo.bar_one` / `foo.bar_other` / ...) i18next is
    called with the BASE (`t('foo.bar', {count})`) — the base is NOT itself
    a leaf key. So we also scan for the base name; any plural-suffixed
    leaves with a matching base count as used.

    prefix_hits: prefixes consumed by t(`foo.${x}`) / i18nKey={`foo.${x}`}.
    """
    blob = _gather_source_text()
    literal: set[str] = set()

    def _quoted(s: str) -> bool:
        return (f"'{s}'" in blob) or (f'"{s}"' in blob) or (f"`{s}`" in blob)

    # Direct hits: leaf-key exactly quoted somewhere.
    for k in en_keys:
        if _quoted(k):
            literal.add(k)

    # Plural-base hits: for each plural-suffixed leaf, check if its base
    # (key[:-len(suffix)]) is quoted; if so, mark all sibling plurals used.
    keyset = set(en_keys)
    bases: set[str] = set()
    for k in en_keys:
        for suf in PLURAL_SUFFIXES:
            if k.endswith(suf):
                bases.add(k[: -len(suf)])
                break
    for base in bases:
        if _quoted(base):
            for suf in PLURAL_SUFFIXES:
                if (base + suf) in keyset:
                    literal.add(base + suf)

    prefixes: set[str] = set()
    for m in PREFIX_T.finditer(blob):
        prefixes.add(m.group(1))
    for m in TRANS_PREFIX.finditer(blob):
        prefixes.add(m.group(1))

    return literal, prefixes


# -------- Step 3: classify --------


def classify(en_keys: list[str], literal: set[str], prefixes: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (used_literal, used_via_prefix, unused)."""
    keyset = set(en_keys)
    used_literal: set[str] = set()
    used_via_prefix: set[str] = set()

    # Literal hits: direct match
    for k in literal:
        if k in keyset:
            used_literal.add(k)
        # Plural-base hit: t('foo.bar', {count}) keeps foo.bar_<plural>
        for suf in PLURAL_SUFFIXES:
            if (k + suf) in keyset:
                used_literal.add(k + suf)
        # Sometimes the literal IS the plural base name (e.g. 'foo.bar' with key
        # only existing as 'foo.bar_one' / 'foo.bar_other') — covered above.

    # Prefix hits: t(`foo.${x}`) keeps every key starting with `foo.`
    for p in prefixes:
        prefix_dot = p + "."
        for k in en_keys:
            if k == p or k.startswith(prefix_dot):
                used_via_prefix.add(k)

    used = used_literal | used_via_prefix
    unused = sorted(set(en_keys) - used)
    return sorted(used_literal), sorted(used_via_prefix), unused


def main() -> int:
    en_keys, uk_keys = load_keys()
    print(f"Locale leaf keys -- en: {len(en_keys)}, uk: {len(uk_keys)}")
    en_set, uk_set = set(en_keys), set(uk_keys)
    only_en = sorted(en_set - uk_set)
    only_uk = sorted(uk_set - en_set)
    if only_en or only_uk:
        print(f"  !!  parity skew -- only-en: {len(only_en)}, only-uk: {len(only_uk)}")
        for k in only_en[:5]:
            print(f"     only-en: {k}")
        for k in only_uk[:5]:
            print(f"     only-uk: {k}")

    literal, prefixes = scan_sources(en_keys)
    print(f"Source hits -- literal t()/Trans: {len(literal)}, template-prefix t(`x.${{...}}`): {len(prefixes)}")
    if prefixes:
        print(f"  template prefixes in use: {sorted(prefixes)}")

    used_literal, used_prefix, unused = classify(en_keys, literal, prefixes)

    print()
    print(f"Used (literal/Trans/plural):  {len(used_literal)}")
    print(f"Used (template-prefix only):  {len(set(used_prefix) - set(used_literal))}")
    print(f"UNUSED (candidates to drop):  {len(unused)}")
    print()

    out = ROOT / "temp" / "i18n_unused_keys.txt"
    out.write_text("\n".join(unused) + ("\n" if unused else ""), encoding="utf-8")
    print(f"Full unused-keys list written to: {out.relative_to(ROOT)}")

    if unused:
        print()
        print("First 30 unused (grouped by top-level namespace):")
        from collections import defaultdict

        by_ns = defaultdict(list)
        for k in unused:
            ns = k.split(".", 1)[0]
            by_ns[ns].append(k)
        for ns in sorted(by_ns, key=lambda n: -len(by_ns[n]))[:15]:
            print(f"  {ns}: {len(by_ns[ns])}")
            for k in by_ns[ns][:3]:
                print(f"    - {k}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
