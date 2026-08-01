from __future__ import annotations

import importlib.util
import sys
import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "select_target_products.py"
SPEC = importlib.util.spec_from_file_location("select_target_products", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

with (PROJECT_ROOT / "config" / "product_filter_rules.toml").open("rb") as handle:
    RULES = MODULE.compile_rules(tomllib.load(handle))


def classify(
    title: str,
    *,
    categories: list[str] | None = None,
    features: list[str] | None = None,
    description: list[str] | None = None,
) -> dict:
    record = {
        "parent_asin": "TESTPARENT",
        "main_category": "Tools & Home Improvement",
        "title": title,
        "categories": categories or [],
        "features": features or [],
        "description": description or [],
        "store": None,
        "details": {},
        "price": None,
        "average_rating": 1.0,
        "rating_number": 999,
    }
    return MODULE.analyze_candidate(record, RULES)


class ProductFilterRulesTests(unittest.TestCase):
    def test_smart_plug_is_included(self) -> None:
        result = classify("Kasa Wi-Fi Smart Plug with Alexa App Control")
        self.assertEqual(result["eligible_device_types"], ["smart_plug"])

    def test_connected_smart_bulb_is_included(self) -> None:
        result = classify("Color Smart Bulb", features=["Wi-Fi app control"])
        self.assertEqual(result["eligible_device_types"], ["smart_bulb"])

    def test_smart_wall_switch_is_included(self) -> None:
        result = classify("Smart Wall Light Switch", features=["Works with Alexa"])
        self.assertEqual(result["eligible_device_types"], ["smart_switch"])

    def test_smart_dimmer_boundary_is_included_with_control_evidence(self) -> None:
        result = classify("Smart Wall Dimmer", features=["Zigbee app control"])
        self.assertEqual(result["eligible_device_types"], ["smart_switch"])

    def test_plain_led_bulb_is_not_a_candidate(self) -> None:
        result = classify("A19 LED Light Bulb 60W Equivalent")
        self.assertIsNone(result)

    def test_ethernet_switch_is_excluded(self) -> None:
        result = classify("Smart Managed 8-Port Gigabit Ethernet Network Switch")
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "network switch" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_spark_plug_is_excluded(self) -> None:
        result = classify("Smart Compatible Iridium Spark Plug")
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("spark plug" in reason for reason in result["exclusion_reasons"])
        )

    def test_switch_wall_plate_accessory_is_excluded(self) -> None:
        result = classify("Wall Plate for Smart Light Switch")
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("accessory" in reason for reason in result["exclusion_reasons"])
        )

    def test_bluetooth_audio_plug_is_not_smart_evidence(self) -> None:
        result = classify("Bluetooth Headphones with 3.5mm Audio Plug")
        self.assertIsNone(result)

    def test_generic_smart_switch_without_wall_context_is_excluded(self) -> None:
        result = classify("Portable Blender with Smart Switch")
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "required_target_context_absent" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_smart_charger_with_plain_plug_is_excluded(self) -> None:
        result = classify("Smart Battery Charger with US Plug")
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "approved_identity_phrase_absent" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_smart_touch_mirror_switch_is_excluded(self) -> None:
        result = classify(
            "Wall Mounted Smart Mirror with Touch Switch and Dimmable Light"
        )
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "approved_identity_phrase_absent" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_smart_power_strip_requires_specific_control_evidence(self) -> None:
        result = classify("Smart Power Strip with Six Outlets and USB Ports")
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "conditional_without_specific_smart_control_evidence" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_wifi_plug_and_play_adapter_is_excluded(self) -> None:
        result = classify("Wireless CarPlay Adapter with 5GHz WiFi Plug and Play")
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("plug and play" in reason for reason in result["exclusion_reasons"])
        )

    def test_echo_smart_bulb_bundle_is_excluded(self) -> None:
        result = classify("Echo bundle with Sengled Smart Bulb")
        self.assertIn("smart_bulb", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("echo" in reason for reason in result["exclusion_reasons"])
        )

    def test_smart_switch_bathroom_mirror_is_excluded(self) -> None:
        result = classify("Wall Mounted Bathroom Mirror with Smart Switch")
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("mirror" in reason for reason in result["exclusion_reasons"])
        )

    def test_camera_bundle_with_smart_plug_is_excluded(self) -> None:
        result = classify("Security Camera Bundle with WiFi Smart Plug")
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("camera" in reason for reason in result["exclusion_reasons"])
        )

    def test_non_smart_appliance_surge_protector_is_excluded(self) -> None:
        result = classify(
            "Heavy Duty 2400 Watt Appliance Surge Protector Smart Plug "
            "with Outlet Saver Power Cord"
        )
        self.assertIn("smart_plug", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any(
                "outlet saver" in reason
                for reason in result["exclusion_reasons"]
            )
        )

    def test_rf_only_zmart_switch_is_excluded(self) -> None:
        result = classify(
            "Viatek Zmart Smart Switch",
            categories=["Home automation", "Lamps & Lighting"],
            description=["Instant two way wireless switch with 30ft RF remote"],
        )
        self.assertIn("smart_switch", result["candidate_device_types"])
        self.assertFalse(result["eligible_after_exclusions"])
        self.assertTrue(
            any("zmart" in reason for reason in result["exclusion_reasons"])
        )

    def test_rating_does_not_affect_classification(self) -> None:
        first = classify("Wi-Fi Smart Plug")
        second_record = {
            "parent_asin": "TESTPARENT",
            "main_category": "Tools & Home Improvement",
            "title": "Wi-Fi Smart Plug",
            "categories": [],
            "features": [],
            "description": [],
            "store": None,
            "details": {},
            "price": None,
            "average_rating": 5.0,
            "rating_number": 1,
        }
        second = MODULE.analyze_candidate(second_record, RULES)
        comparable = (
            "candidate_device_types",
            "eligible_device_types",
            "exclusion_reasons",
            "provisional_device_type",
        )
        self.assertEqual(
            {key: first[key] for key in comparable},
            {key: second[key] for key in comparable},
        )


if __name__ == "__main__":
    unittest.main()
