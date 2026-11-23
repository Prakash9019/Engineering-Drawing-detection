"""
Unit Tests for Core Extraction and Reasoning Utilities
Fast (<0.5s), offline, no API key required.
"""
import pytest
from core.confidence import label_to_score, calc_final, route
from core.isa_decode import decode_isa, classify_discipline, cedm_normalize
from core.geometry import iou, box_center, box_area, distance
from core.json_parser import parse_json


class TestConfidence:
    def test_label_to_score(self):
        assert label_to_score("HIGH") == 0.95
        assert label_to_score("MEDIUM") == 0.75
        assert label_to_score("LOW") == 0.50
        assert label_to_score("UNKNOWN") == 0.75

    def test_calc_final_range(self):
        score = calc_final(c_det=0.9, c_ocr=0.9, c_geo=0.8, c_val=1.0, c_reg=0.5)
        assert 0.0 <= score <= 1.0
        assert score > 0.7

    def test_route(self):
        assert route(0.90) == "AUTO_ACCEPT"
        assert route(0.70) == "REVIEW_REQUIRED"
        assert route(0.40) == "AUTO_REJECT"


class TestIsaDecode:
    def test_decode_standard_tag(self):
        res = decode_isa("PIT-211")
        assert res["type"] == "INSTRUMENT"
        assert "PRESSURE" in res.get("description", "").upper()

    def test_decode_flow_element(self):
        res = decode_isa("FE-224")
        assert res["type"] == "INSTRUMENT"
        assert "FLOW" in res.get("description", "").upper()

    def test_classify_discipline(self):
        assert classify_discipline("PIT-211") == "INSTRUMENTATION"
        assert classify_discipline("2\"-ETH-V057-61440X") == "PIPING"

    def test_decode_empty(self):
        res = decode_isa("")
        assert res["type"] == "UNKNOWN"


class TestGeometry:
    def test_iou_identical(self):
        box = [10, 10, 50, 50]
        assert pytest.approx(iou(box, box), 0.01) == 1.0

    def test_iou_disjoint(self):
        box1 = [0, 0, 10, 10]
        box2 = [20, 20, 30, 30]
        assert iou(box1, box2) == 0.0

    def test_box_center_and_area(self):
        box = [0, 0, 100, 50]
        assert box_center(box) == (50.0, 25.0)
        assert box_area(box) == 5000

    def test_distance(self):
        assert distance((0, 0), (3, 4)) == 5.0


class TestJsonParser:
    def test_parse_json_markdown(self):
        raw = "```json\n[{\"tag\": \"PIT-201\"}]\n```"
        parsed = parse_json(raw)
        assert isinstance(parsed, list)
        assert parsed[0]["tag"] == "PIT-201"

    def test_parse_json_plain(self):
        raw = '{"drawing": "4224-MGDV"}'
        parsed = parse_json(raw)
        assert isinstance(parsed, dict)
        assert parsed["drawing"] == "4224-MGDV"

    def test_parse_json_fallback(self):
        raw = "not valid json"
        assert parse_json(raw) == "not valid json"
