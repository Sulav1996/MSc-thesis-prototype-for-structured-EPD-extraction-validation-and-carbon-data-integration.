"""
Regression tests (standard library unittest — no extra packages).

    python -m unittest discover -s tests -v

* VELUX EPD: the new extractor must reproduce every value of the human-approved
  record in data/epds.json (the first prototype's reviewed extraction).
* ILCD+EPD sample data sets (skipped when the 'ILCD pdf' folder is absent).
* Store round trip: save → search → fetch → version → bundle → rebuild.
"""

from __future__ import annotations

import glob
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from dictionary import load_dictionary, match_indicator, parse_module_header  # noqa: E402
from epd_store import EPDStore, load_settings  # noqa: E402
from extraction.common import parse_date, parse_number  # noqa: E402
from extraction.extractor import extract_epd, extract_epd_records  # noqa: E402
from extraction.pdf_reader import available_backend  # noqa: E402

VELUX = sorted(glob.glob(str(PROJECT / "uploads" / "*VELUX*.pdf")))
SAMPLES = PROJECT / "ILCD pdf" / "ILCD-EPD-Data-Format-release-v1.3" / "sample_data"


class DictionaryTests(unittest.TestCase):
    def test_v4_keeps_v3_keys(self):
        dictionary = load_dictionary()
        self.assertTrue(dictionary["version"].startswith("4."))
        for key in dictionary["_v3_original_keys"]:
            self.assertIn(key, dictionary)
        sections = dictionary["step3_dictionary"]["sections"]
        self.assertEqual(len(sections), 10)
        self.assertEqual(sum(len(s["fields"]) for s in sections), 86)

    def test_helpers(self):
        self.assertEqual(parse_number("1,04E+02"), 104.0)
        self.assertEqual(parse_number("-3.96E+01"), -39.6)
        self.assertIsNone(parse_number("MND"))
        self.assertEqual(parse_date("11/07/2025")["iso"], "2025-07-11")
        self.assertEqual(parse_module_header("Tot.A1-A3"), ("A1-A3", None))
        self.assertEqual(parse_module_header("C3/2"), ("C3", "2"))
        self.assertEqual(match_indicator("Water use", "EN15804_A2_UNKNOWN_EF", "m3 world eq deprived")["code"], "WDP")
        self.assertEqual(match_indicator("Use of net fresh water", "unresolved", "m3")["code"], "FW")


@unittest.skipUnless(VELUX and available_backend(), "VELUX PDF or PDF backend missing")
class VeluxRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = extract_epd(Path(VELUX[0]).read_bytes(), Path(VELUX[0]).name)
        gold = json.loads((PROJECT / "data" / "epds.json").read_text(encoding="utf-8"))
        cls.gold = next(r for r in gold if r["metadata"].get("registration_number") == "EPD-VEL-20250344-CBI1-EN")

    def test_values_match_approved_record(self):
        compared = 0
        for code, data in self.record["results"].items():
            for module, value in data["modules"].items():
                expected = self.gold["results"][code]["modules"][module]["value"]
                self.assertEqual(value["value"], expected, f"{code} {module}")
                compared += 1
        self.assertGreaterEqual(compared, 170)

    def test_step1_and_category(self):
        metadata = self.record["metadata"]
        self.assertEqual(metadata["registration_number"], "EPD-VEL-20250344-CBI1-EN")
        self.assertEqual(metadata["verifier"], "Dr.-Ing. Nikolay Minkov")
        self.assertEqual(self.record["physical_properties"]["mass_per_declared_unit_kg"], 44.08)
        params = self.record["step1"]["parameters"]
        self.assertEqual(params["core_standard"]["status"], "pass")
        self.assertEqual(params["third_party_verification"]["status"], "pass")
        self.assertEqual(params["programme_operator"]["status"], "pass")
        self.assertEqual(params["electricity_mix"]["status"], "warning")  # green electricity, not residual mix
        stage = next(m for m in self.record["step1"]["modules"] if m["module"] == "A1-A3")
        self.assertAlmostEqual(stage["gwp_value"], 104 + 4.21 + 24.6)
        self.assertEqual(stage["value_provenance"], "calculated_from_split")
        self.assertEqual(self.record["classification"]["category"], "skylight")


@unittest.skipUnless(SAMPLES.exists(), "ILCD sample data not available")
class IlcdXmlTests(unittest.TestCase):
    def test_wood_panel_xml(self):
        path = SAMPLES / "processes" / "EPDv1.3_example_57a4ae65-d305-421e-b21f-a3f0c35b8abe.xml"
        record = extract_epd_records(path.read_bytes(), path.name)[0]
        self.assertEqual(record["metadata"]["standard_profile"], "EN15804_A2_EF3.0")
        self.assertEqual(record["metadata"]["reference_service_life_years"], 100.0)
        self.assertIn("A1-A3", record["results"]["GWP-total"]["modules"])
        self.assertEqual(record["classification"]["category"], "timber")


class StoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EPDStore(load_settings(backend="local", data_dir=Path(tmp)))
            record = {
                "metadata": {"registration_number": "MD-99999-EN", "product_name": "Test concrete C30/37",
                             "manufacturer": "Test A/S", "declared_unit_raw": "1 m3",
                             "standard_profile": "EN15804_A2_UNKNOWN_EF", "standard": "EN 15804+A2"},
                "results": {"GWP-total": {"canonical_unit": "kg CO2 eq", "modules": {
                    "A1-A3": {"value": 250.0, "status": "declared"}, "D": {"value": -5.0, "status": "declared"}}}},
                "document": {"source_format": "pdf"},
            }
            from extraction.extractor import refresh_derived_blocks

            record = refresh_derived_blocks(record, "ready-mixed concrete C30/37 EPD Danmark")
            first = store.save_record(record, b"%PDF-1.4 test", "test.pdf", "text")
            self.assertEqual(first["status"], "created")
            self.assertEqual(store.save_record(record, b"%PDF-1.4 test", "test.pdf")["status"], "unchanged")
            rows, total = store.search(text="concrete")
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["category"], "concrete")
            self.assertEqual(rows[0]["gwp_a1_a3"], 250.0)
            self.assertEqual(rows[0]["gwp_d"], -5.0)
            bundle = store.export_bundle()
            other = EPDStore(load_settings(backend="local", data_dir=Path(tmp) / "other"))
            self.assertEqual(other.import_bundle(bundle)["created"], 1)
            (Path(tmp) / "epd_index.sqlite").unlink()
            rebuilt = EPDStore(load_settings(backend="local", data_dir=Path(tmp)))
            self.assertEqual(rebuilt.rebuild_index(), 1)


if __name__ == "__main__":
    unittest.main()
