from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnose_product_recall",
    ROOT / "scripts" / "diagnose_product_recall.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_normalization_handles_protocol_variants() -> None:
    text = MODULE.normalized("Wi-Fi and Z-Wave")
    assert text == "wifi and zwave"
    assert MODULE.contains_term(text, "wi fi")
    assert MODULE.contains_term(text, "z wave")


def test_smart_proximity_is_bounded() -> None:
    assert MODULE.within_tokens(
        "smart wifi led color bulb", {"smart"}, {"bulb"}, 6
    )
    assert not MODULE.within_tokens(
        "smart vacuum replacement kit with spare motor filter and bulb",
        {"smart"},
        {"bulb"},
        6,
    )


def test_stable_rank_is_deterministic() -> None:
    one = MODULE.stable_rank(20260729, "smart_bulb", "B000TEST")
    two = MODULE.stable_rank(20260729, "smart_bulb", "B000TEST")
    other = MODULE.stable_rank(20260729, "smart_switch", "B000TEST")
    assert one == two
    assert one != other


def test_audit_labels_are_closed_set() -> None:
    assert MODULE.AUDIT_LABELS == {
        "correct_target",
        "false_positive",
        "ambiguous",
        "wrong_device_type",
        "accessory",
        "non_smart",
        "insufficient_evidence",
    }


def test_script_declares_only_identity_and_audit_columns() -> None:
    declared = set(
        MODULE.ALLOWED_IDENTITY_COLUMNS
        + MODULE.CANDIDATE_AUDIT_COLUMNS
        + MODULE.TARGET_COLUMNS
    )
    assert "average_rating" not in declared
    assert "rating_number" not in declared
    assert "price" not in declared
    assert "rating" not in declared
    assert "user_id" not in declared


def _candidate(**updates):
    row = {
        "parent_asin": "B000TEST",
        "main_category": "Electronics",
        "title": "",
        "categories": "[]",
        "features": "[]",
        "description": "[]",
        "store": "",
        "details": "{}",
        "source_domains": ["Electronics"],
        "candidate_device_types": [],
        "eligible_device_types": [],
        "candidate_device_terms": [],
        "candidate_smart_terms": [],
        "matched_fields": [],
        "exclusion_reasons": [],
        "ambiguity_status": "single_device_type",
    }
    row.update(updates)
    return row


def _rules():
    return tomllib.loads(
        (ROOT / "config" / "product_filter_rules_w3r_v1_4_draft.toml").read_text(
            encoding="utf-8"
        )
    )


def test_bare_english_matter_is_not_matter_protocol() -> None:
    terms = _rules()["smart_control"]["specific_terms"]
    assert "matter" not in terms
    assert "matter compatible" in terms


def test_arduino_dimmer_module_is_not_a_switch() -> None:
    row = _candidate(
        title="Arduino Smart Home Light Dimmer Module Controller Board",
        features='["Controls lighting with a Raspberry Pi"]',
        candidate_device_types=["smart_switch"],
    )
    result = MODULE.classify_switch(row, _rules())
    assert result["proposed_decision"] == "exclude"
    assert result["audit_label"] == "wrong_device_type"


def test_wemo_light_switch_is_recoverable() -> None:
    row = _candidate(
        title="Belkin WeMo Light Switch",
        description=(
            '"Wi-Fi enabled switch replaces a standard light switch and is '
            'controlled by app, Alexa and Google Assistant."'
        ),
        candidate_device_types=["smart_switch"],
    )
    result = MODULE.classify_switch(row, _rules())
    assert result["proposed_decision"] == "include"
    assert result["audit_label"] == "correct_target"


def test_security_cameras_are_not_bulbs() -> None:
    row = _candidate(
        title="WiFi Light Bulb Security Cameras Wireless Outdoor",
        candidate_device_types=["smart_bulb"],
    )
    result = MODULE.classify_bulb(row, _rules())
    assert result["proposed_decision"] == "exclude"
    assert result["audit_label"] == "wrong_device_type"


def test_generic_company_smart_home_copy_is_not_control_evidence() -> None:
    row = _candidate(
        title="GE Ultra Bright LED Floodlight Bulb",
        description='"Our company provides the best smart home experience."',
        candidate_device_types=["smart_bulb"],
    )
    result = MODULE.classify_bulb(row, _rules())
    assert result["proposed_decision"] == "exclude"
    assert result["audit_label"] == "non_smart"


def test_fireplace_controller_is_not_a_wall_light_switch() -> None:
    row = _candidate(
        title="WiFi Smart Switch for Voice Activated Gas Fireplace",
        features='["Alexa and Google Assistant voice control"]',
        candidate_device_types=["smart_plug", "smart_switch"],
    )
    result = MODULE.classify_switch(row, _rules())
    assert result["proposed_decision"] == "exclude"
    assert result["audit_label"] == "wrong_device_type"
