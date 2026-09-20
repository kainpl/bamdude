"""The GitHub issue forms exist in English and Ukrainian, and both must stay *the same form*.

GitHub issue forms have no localisation of their own, so ``.github/ISSUE_TEMPLATE/``
carries each form twice — ``NN-<slug>.yml`` (English) and ``NN-<slug>.uk.yml``
(Ukrainian) — and the reporter picks a language in the template chooser. Two
copies drift the moment one is edited alone: a printer model added to the English
dropdown and not to the Ukrainian one is exactly the gap nobody notices until a
report arrives with the wrong data. This test holds the two copies to the same
*shape* — field ids and order, which fields are required, how many options each
dropdown offers, the labels and title prefix — and leaves the words free.

It also pins two things GitHub itself enforces only by silently misbehaving:
a ``name`` that is not unique across templates breaks the chooser, and a label a
form references that does not exist in the repository is dropped without a word
(the old template asked for ``triage``, which was never created).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / ".github" / "ISSUE_TEMPLATE"

# Labels that exist in kainpl/bamdude and that a *form* may apply. A new label
# is created in the repository first and added here second — the point of the
# set is that the two cannot silently disagree.
FORM_LABELS = {"bug", "enhancement"}

pytestmark = pytest.mark.skipif(
    not TEMPLATE_DIR.is_dir(),
    reason="the issue templates are not part of this checkout (Docker test image)",
)


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"{path.name}: not a mapping"
    return data


def _forms() -> dict[str, Path]:
    """All form files keyed by filename, ``config.yml`` excluded."""
    return {p.name: p for p in sorted(TEMPLATE_DIR.glob("*.yml")) if p.name != "config.yml"}


def _slug(name: str) -> str:
    """``02-bug-report.uk.yml`` -> ``bug-report``.

    The numeric prefix only orders the chooser (English, then its Ukrainian copy,
    then the next form), so the two halves of a pair carry *different* prefixes
    and are matched on the slug alone.
    """
    stem = name.removesuffix(".uk.yml").removesuffix(".yml")
    prefix, _, slug = stem.partition("-")
    assert prefix.isdigit() and slug, f"{name}: expected NN-<slug>[.uk].yml"
    return slug


def _pairs() -> list[tuple[Path, Path]]:
    forms = _forms()
    english = {_slug(n): p for n, p in forms.items() if not n.endswith(".uk.yml")}
    ukrainian = {_slug(n): p for n, p in forms.items() if n.endswith(".uk.yml")}
    assert set(english) == set(ukrainian), (
        f"every form needs both languages — English only: {sorted(set(english) - set(ukrainian))}, "
        f"Ukrainian only: {sorted(set(ukrainian) - set(english))}"
    )
    return [(english[slug], ukrainian[slug]) for slug in sorted(english)]


def _shape(item: dict) -> tuple:
    """Everything about a body item that must agree between languages."""
    attrs = item.get("attributes") or {}
    options = attrs.get("options")
    option_count = len(options) if isinstance(options, list) else None
    required_options = None
    if item.get("type") == "checkboxes" and isinstance(options, list):
        required_options = tuple(bool(o.get("required")) for o in options)
    return (
        item.get("type"),
        item.get("id"),
        bool((item.get("validations") or {}).get("required")),
        attrs.get("multiple"),
        attrs.get("render"),
        option_count,
        required_options,
    )


def test_every_form_has_what_github_requires():
    for name, path in _forms().items():
        data = _load(path)
        for key in ("name", "description", "body"):
            assert key in data, f"{name}: missing required key {key!r}"
        ids = [item.get("id") for item in data["body"] if item.get("type") != "markdown"]
        assert all(ids), f"{name}: every non-markdown field needs an id"
        assert len(ids) == len(set(ids)), f"{name}: duplicate field ids"


def test_form_names_are_unique_across_languages():
    names = [_load(p)["name"] for p in _forms().values()]
    assert len(names) == len(set(names)), f"template names must be unique, got {names}"


def test_forms_only_apply_labels_that_exist():
    for name, path in _forms().items():
        labels = _load(path).get("labels") or []
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",")]
        unknown = set(labels) - FORM_LABELS
        assert not unknown, f"{name}: label(s) {sorted(unknown)} are not in the repository — GitHub drops them silently"


def test_english_and_ukrainian_forms_have_the_same_shape():
    for en_path, uk_path in _pairs():
        en, uk = _load(en_path), _load(uk_path)
        assert en.get("title") == uk.get("title"), f"{en_path.name}: title prefix differs from {uk_path.name}"
        assert en.get("labels") == uk.get("labels"), f"{en_path.name}: labels differ from {uk_path.name}"
        en_shape = [_shape(i) for i in en["body"]]
        uk_shape = [_shape(i) for i in uk["body"]]
        assert en_shape == uk_shape, (
            f"{en_path.name} and {uk_path.name} are not the same form.\n  en: {en_shape}\n  uk: {uk_shape}"
        )
