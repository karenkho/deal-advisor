"""The held-out test set is frozen by hash and its answer keys meet the plan. Nothing here runs a TOR through the app."""
import hashlib
import json
from pathlib import Path

import pytest

SET = Path(__file__).resolve().parent.parent / "eval" / "test_set"
MANIFEST = json.loads((SET / "manifest.json").read_text())
KEYS = {n: json.loads((SET / f"{n}_answer_key.json").read_text()) for n in ("alumni", "fleet", "center")}


@pytest.mark.parametrize("name", sorted(MANIFEST["files"]))
def test_no_held_out_file_has_changed(name):
    assert hashlib.sha256((SET / name).read_bytes()).hexdigest() == MANIFEST["files"][name], f"{name} changed: the held-out set is frozen"


def test_nothing_was_added_to_the_frozen_set():
    assert {p.name for p in SET.iterdir() if p.is_file()} == set(MANIFEST["files"]) | {"manifest.json"}


@pytest.mark.parametrize("name", sorted(KEYS))
def test_each_key_has_the_required_planted_flaws_and_risks(name):
    key = KEYS[name]
    kinds = {f["kind"] for f in key["planted_flaws"]}
    assert {"vague_requirement", "conflict", "implied_migration", "out_of_scope_item", "prompt_injection", "pii"} <= kinds
    assert 4 <= len(key["planted_flaws"]) <= 7 and key["expected_risks"] and key["commercial_terms"]["requested_delivery_months"]
    assert len({f["id"] for f in key["features"]}) == len(key["features"])


def test_the_terms_differ_with_one_healthy_and_one_risky():
    assert {k["commercial_terms"]["terms"] for k in KEYS.values()} == {"healthy", "risky", "moderate"}


def test_the_formats_required_for_the_held_out_set_are_all_there():
    assert "Label: description" in KEYS["alumni"]["format"] and "bullets in its cells" in KEYS["fleet"]["format"] and (SET / "tor_convention_center.docx").exists()


def test_domains_do_not_overlap_the_exclusion_list():
    excluded = ("hr", "port", "energy", "billing", "courier", "lending", "inventory", "warehouse", "lgu", "enrollment", "travel", "expense", "delivery", "clinic", "library")
    for key in KEYS.values():
        assert not set(key["domain"].split("_")) & set(excluded), key["domain"]


def test_one_bulletin_supersedes_one_statement_and_adds_one_requirement():
    bulletin = KEYS["center"]["bid_bulletins"][0]["expected_effects"]
    assert len(bulletin["superseded"]) == 1 and bulletin["new_requirement"] and (SET / "center_bid_bulletin_01.pdf").exists()
