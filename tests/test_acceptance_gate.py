import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from acceptance_gate import validate_evidence


def evidence_document():
    return {
        "schema_version": 1,
        "production_level": "E2",
        "claims": ["offline", "default_startup"],
        "status": "CONVERGED",
        "uncovered": ["tray_menu", "long_term_trend"],
        "samples": {
            "attempts": 2,
            "complete_successes": 2,
            "success_rate": 1.0,
        },
        "manifest": {
            "total": 1,
            "executed": 1,
            "passed": 1,
            "failed": 0,
            "missing": 0,
            "undeclared_skips": 0,
            "timeouts": 0,
            "retries": 0,
        },
        "process_generation": "generation-a",
        "production_evidence_sha256": "a" * 64,
        "production": {
            "would_auto_install": False,
            "start_invoked": True,
        },
    }


def production_verdict():
    return {
        "status": "passed",
        "production_level": "E2",
        "evidence_sha256": "a" * 64,
        "process_generation": "generation-a",
        "samples": {
            "attempts": 2,
            "complete_successes": 2,
            "success_rate": 1.0,
        },
        "facts": {
            "would_auto_install": False,
            "start_invoked": True,
            "tray_menu_observed": False,
            "normal_exit_observed": False,
        },
    }


class AcceptanceGateTest(unittest.TestCase):
    def assertError(self, error, document, verdict=None):
        self.assertIn(error, validate_evidence(document, verdict))

    def test_valid_e2_evidence_with_declared_limits(self):
        self.assertEqual(validate_evidence(evidence_document(), production_verdict()), [])

    def test_e1_cannot_claim_default_startup(self):
        document = evidence_document()
        document["production_level"] = "E1"
        document["status"] = "BLOCKED"
        verdict = production_verdict()
        verdict["production_level"] = "E1"
        self.assertError("E1_CANNOT_CLAIM_DEFAULT_STARTUP", document, verdict)

    def test_full_desktop_requires_actual_e3_facts(self):
        document = evidence_document()
        document["production_level"] = "E3"
        document["claims"].append("full_desktop")
        verdict = production_verdict()
        verdict["production_level"] = "E3"
        self.assertError("E3_FACTS_REQUIRED", document, verdict)

        verdict["facts"]["tray_menu_observed"] = True
        verdict["facts"]["normal_exit_observed"] = True
        self.assertEqual(validate_evidence(document, verdict), [])

    def test_zero_attempts_cannot_report_full_success(self):
        document = evidence_document()
        document["samples"] = {
            "attempts": 0,
            "complete_successes": 0,
            "success_rate": 1.0,
        }
        self.assertError("SUCCESS_RATE_WITHOUT_SAMPLE", document, production_verdict())

        document["samples"]["success_rate"] = None
        self.assertError("SAMPLES_REQUIRED", document, production_verdict())

    def test_converged_requires_nonempty_uncovered_register(self):
        document = evidence_document()
        document["uncovered"] = []
        self.assertError("UNCOVERED_REQUIRED", document, production_verdict())

    def test_samples_must_match_production_probe_verdict(self):
        verdict = production_verdict()
        verdict["samples"]["attempts"] = 3
        self.assertError("PRODUCTION_SAMPLE_MISMATCH", evidence_document(), verdict)

        verdict = production_verdict()
        del verdict["samples"]
        self.assertError("PRODUCTION_SAMPLE_MISMATCH", evidence_document(), verdict)

    def test_converged_requires_an_uncovered_register(self):
        document = evidence_document()
        del document["uncovered"]
        self.assertError("UNCOVERED_REQUIRED", document, production_verdict())

    def test_converged_requires_complete_manifest(self):
        for key, value in (
            ("executed", 0),
            ("passed", 0),
            ("failed", 1),
            ("missing", 1),
            ("undeclared_skips", 1),
            ("timeouts", 1),
            ("retries", 1),
        ):
            with self.subTest(key=key):
                document = evidence_document()
                document["manifest"][key] = value
                self.assertError("MANIFEST_NOT_COMPLETE", document, production_verdict())

    def test_production_claim_requires_passed_verdict(self):
        document = evidence_document()
        self.assertError("PRODUCTION_VERDICT_REQUIRED", document)

        verdict = production_verdict()
        verdict["status"] = "failed"
        self.assertError("PRODUCTION_VERDICT_NOT_PASSED", document, verdict)

    def test_verdict_must_bind_evidence_hash_generation_and_level(self):
        for key, value, error in (
            ("evidence_sha256", "b" * 64, "PRODUCTION_EVIDENCE_HASH_MISMATCH"),
            ("process_generation", "generation-b", "PRODUCTION_GENERATION_MISMATCH"),
            ("production_level", "E1", "PRODUCTION_LEVEL_MISMATCH"),
        ):
            with self.subTest(key=key):
                verdict = production_verdict()
                verdict[key] = value
                self.assertError(error, evidence_document(), verdict)

    def test_auto_install_or_uninvoked_start_cannot_claim_default_startup(self):
        for key, error in (
            ("would_auto_install", "AUTO_INSTALL_BLOCKS_STARTUP_CLAIM"),
            ("start_invoked", "START_NOT_INVOKED"),
        ):
            with self.subTest(key=key):
                document = evidence_document()
                document["production"][key] = True if key == "would_auto_install" else False
                self.assertError(error, document, production_verdict())

    def test_blocked_offline_evidence_does_not_need_production_verdict(self):
        document = evidence_document()
        document["production_level"] = "E0"
        document["claims"] = ["offline"]
        document["status"] = "BLOCKED"
        del document["production_evidence_sha256"]
        del document["process_generation"]
        del document["production"]
        self.assertEqual(validate_evidence(document), [])

    def test_cli_rejects_verdict_without_raw_production_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            evidence_path = temp / "evidence.json"
            verdict_path = temp / "verdict.json"
            evidence_path.write_text(json.dumps(evidence_document()))
            verdict_path.write_text(json.dumps(production_verdict()))
            result = subprocess.run([
                sys.executable, "-I", str(ROOT / "tests/acceptance_gate.py"),
                str(evidence_path), "--production-verdict", str(verdict_path),
            ], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("PRODUCTION_EVIDENCE_REQUIRED", result.stdout)

    def test_cli_returns_nonzero_and_rejects_raw_evidence_hash_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            evidence_path = temp / "evidence.json"
            raw_path = temp / "production.json"
            verdict_path = temp / "verdict.json"
            document = evidence_document()
            raw = b'{"generation":"generation-a"}'
            document["production_evidence_sha256"] = hashlib.sha256(raw).hexdigest()
            verdict = production_verdict()
            verdict["evidence_sha256"] = document["production_evidence_sha256"]
            evidence_path.write_text(json.dumps(document))
            raw_path.write_bytes(raw)
            verdict_path.write_text(json.dumps(verdict))
            command = [
                sys.executable, "-I", str(ROOT / "tests/acceptance_gate.py"),
                str(evidence_path), "--production-evidence", str(raw_path),
                "--production-verdict", str(verdict_path),
            ]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            raw_path.write_bytes(b'{"generation":"generation-b"}')
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("PRODUCTION_EVIDENCE_HASH_MISMATCH", result.stdout)


if __name__ == "__main__":
    unittest.main()
