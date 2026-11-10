import ast
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "test-results/2026-11-09-runtime-convergence"
MATRIX_PATH = RESULTS / "contract-matrix.json"
SCHEMA_PATH = RESULTS / "runtime-metrics-schema.json"
GROUPS = {"x_tunnel_download", "dns_routing", "front_http", "smart_router_pipe", "launcher"}
ORACLES = {"normal", "target_exception", "adjacent_exception", "recovery", "stop_or_generation"}
NODE = "tests/test_x_tunnel_memory_backpressure.py::MemoryBackpressureTest.test_future_command_queue_has_a_byte_limit"


def validate_matrix(document, schema, root=ROOT):
    errors = []
    if document.get("schema_version") != 1 or not isinstance(document.get("cards"), list):
        return ["INVALID_MATRIX"]
    cards = document["cards"]
    groups = {card.get("group") for card in cards if isinstance(card, dict)}
    for group in sorted(GROUPS - groups):
        errors.append("MISSING_GROUP:" + group)
    fields = schema.get("fields", {})
    for card in cards:
        if not isinstance(card, dict):
            errors.append("INVALID_CARD")
            continue
        card_id = card.get("id", "<unknown>")
        for key in ("id", "group", "intent", "oracles", "tests", "metrics", "production_level", "uncovered"):
            if key not in card:
                errors.append("MISSING_FIELD:" + card_id + ":" + key)
        if not isinstance(card.get("oracles"), dict):
            errors.append("INVALID_ORACLES:" + card_id)
            continue
        for oracle in sorted(ORACLES - set(card["oracles"])):
            errors.append("MISSING_ORACLE:" + card_id + ":" + oracle)
        tests = card.get("tests", [])
        for node in tests:
            if not test_node_exists(node, root):
                errors.append("MISSING_TEST_NODE:" + node)
        if not card.get("metrics") and not card.get("manual_evidence"):
            errors.append("METRIC_OR_MANUAL_EVIDENCE_REQUIRED:" + card_id)
        for metric in card.get("metrics", []):
            if metric not in fields:
                errors.append("MISSING_METRIC:" + metric)
        for name, oracle in card["oracles"].items():
            if name not in ORACLES:
                errors.append("UNKNOWN_ORACLE:" + card_id + ":" + name)
                continue
            if "not_applicable" in oracle:
                if not oracle.get("reason"):
                    errors.append("ORACLE_REASON_REQUIRED:" + card_id + ":" + name)
                continue
            for node in oracle.get("tests", []):
                if node not in tests:
                    errors.append("ORACLE_TEST_NOT_REGISTERED:" + card_id + ":" + name)
                if not test_node_exists(node, root):
                    continue
                log_name = oracle.get("result_log")
                if not log_name:
                    errors.append("RESULT_LOG_REQUIRED:" + card_id + ":" + name)
                    continue
                log_path = Path(log_name)
                if not log_path.is_absolute():
                    log_path = root / log_path
                try:
                    output = log_path.read_text()
                except OSError:
                    errors.append("RESULT_LOG_MISSING:" + card_id + ":" + name)
                    continue
                if not test_node_passed(node, output):
                    errors.append("RESULT_NOT_PASSED:" + card_id + ":" + name + ":" + node)
    return errors


def test_node_exists(node, root=ROOT):
    try:
        path, class_name, method = node.split("::")[0], node.split("::")[1].split(".")[0], node.split(".")[-1]
        tree = ast.parse((root / path).read_text())
    except (OSError, SyntaxError, ValueError, IndexError):
        return False
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == class_name:
            return any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method for child in item.body)
    return False


def test_node_passed(node, output):
    class_name = node.split("::")[1].split(".")[0]
    method = node.split(".")[-1]
    header = re.compile(
        rf"^{re.escape(method)} \((?:[^.()]+\.)*{re.escape(class_name)}(?:\.{re.escape(method)})?\) \.\.\.(.*)$")
    any_test = re.compile(r"^test[^ ]* \([^)]*\) \.\.\.")
    waiting_for_result = False
    for line in output.splitlines():
        if waiting_for_result:
            outcome = line.strip()
            if outcome == "ok":
                return True
            if outcome in {"FAIL", "ERROR"} or outcome.startswith("skipped ") or any_test.match(line):
                return False
            continue
        match = header.match(line)
        if match is None:
            continue
        outcome = match.group(1).strip()
        if outcome == "ok":
            return True
        if outcome in {"FAIL", "ERROR"} or outcome.startswith("skipped "):
            return False
        waiting_for_result = True
    return False


def valid_card(log_path):
    oracles = {
        name: {"tests": [NODE], "result_log": str(log_path)}
        for name in ORACLES
    }
    return {
        "id": "download-queue-boundary",
        "group": "x_tunnel_download",
        "intent": "合法窗口内恢复且真实越界有界关闭",
        "oracles": oracles,
        "tests": [NODE],
        "metrics": ["x_tunnel.command_gap_recovered_total"],
        "production_level": "E0",
        "uncovered": [],
    }


def valid_document(log_path):
    cards = []
    for group in sorted(GROUPS):
        card = valid_card(log_path)
        card["group"] = group
        card["id"] = group + "-contract"
        cards.append(card)
    return {"schema_version": 1, "cards": cards}


class ContractMatrixTest(unittest.TestCase):
    def test_actual_matrix_is_complete(self):
        document = json.loads(MATRIX_PATH.read_text())
        schema = json.loads(SCHEMA_PATH.read_text())
        self.assertEqual(validate_matrix(document, schema), [])

    def test_all_five_oracles_are_independently_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "result.log"
            log.write_text("test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest) ... ok\n")
            document = valid_document(log)
            del document["cards"][0]["oracles"]["recovery"]
            schema = {"fields": {"x_tunnel.command_gap_recovered_total": {}}}
            self.assertIn("MISSING_ORACLE:dns_routing-contract:recovery", validate_matrix(document, schema))

    def test_missing_test_method_and_metric_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "result.log"
            log.write_text("test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest) ... ok\n")
            document = valid_document(log)
            bad_node = NODE.replace("test_future_command_queue_has_a_byte_limit", "test_not_real")
            document["cards"][0]["tests"] = [bad_node]
            for oracle in document["cards"][0]["oracles"].values():
                oracle["tests"] = [bad_node]
            errors = validate_matrix(document, {"fields": {}})
            self.assertIn("MISSING_TEST_NODE:" + bad_node, errors)
            self.assertIn("MISSING_METRIC:x_tunnel.command_gap_recovered_total", errors)

    def test_logged_success_is_accepted_but_failure_is_not(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "result.log"
            log.write_text(
                "test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest.test_future_command_queue_has_a_byte_limit) ... \n"
                "2026-11-09 [x_tunnel][DEBUG] SendBuffer reset\n"
                "ok\n")
            document = valid_document(log)
            schema = {"fields": {"x_tunnel.command_gap_recovered_total": {}}}
            self.assertEqual(validate_matrix(document, schema), [])

            log.write_text(
                "test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest) ... \n"
                "FAIL\n"
                "test_other_case (__main__.MemoryBackpressureTest) ... ok\n")
            errors = validate_matrix(document, schema)
            self.assertTrue(any(error.startswith("RESULT_NOT_PASSED:") for error in errors))

    def test_precise_method_must_be_ok_in_result_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "result.log"
            log.write_text("test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest) ... FAIL\n")
            document = valid_document(log)
            schema = {"fields": {"x_tunnel.command_gap_recovered_total": {}}}
            errors = validate_matrix(document, schema)
            self.assertTrue(any(error.startswith("RESULT_NOT_PASSED:") for error in errors))

            log.write_text("test_future_command_queue_has_a_byte_limit (__main__.MemoryBackpressureTest) ... ok\n")
            self.assertEqual(validate_matrix(document, schema), [])


if __name__ == "__main__":
    unittest.main()
