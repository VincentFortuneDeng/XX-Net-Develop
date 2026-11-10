import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "test-results/2026-11-09-runtime-convergence"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("deploy")
validate_production = load("validate_production")
production_probe = load("production_probe")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(root, files):
    entries = [{
        "file": relative,
        "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
    } for relative in sorted(files)]
    return hashlib.sha256(json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def probe(category, attempts=1, expected=128):
    return {
        "category": category,
        "status": "complete",
        "attempts": attempts,
        "complete_successes": attempts,
        "failures": {
            "timeout": 0, "truncated": 0, "connection": 0,
            "http_status": 0, "content_mismatch": 0, "other": 0,
        },
        "expected_bytes": expected,
        "actual_bytes": expected,
        "latency_seconds": 0.25,
    }


def runtime_modules():
    return {
        "gae_proxy": {"state": "not_running", "collection": "not_running"},
        "smart_router": {
            "state": "running", "collection": "ok",
            "metrics": {
                "smart_router.pipe_running": 1,
                "smart_router.direct_connect_failures_total": 0,
                "smart_router.handoff_failures_total": 0,
                "smart_router.pipe_register_failures_total": 0,
                "smart_router.pipe_stopped_handoffs_total": 0,
                "smart_router.pipe_closed_socket_handoffs_total": 0,
                "smart_router.pipe_select_failures_total": 0,
                "smart_router.pipe_loop_failures_total": 0,
            },
        },
        "x_tunnel": {"state": "running", "collection": "ok"},
    }


def valid_evidence():
    return {
        "schema_version": 1,
        "production_level": "E2",
        "entry": "/Applications/XX-Net/start",
        "argv": ["/Applications/XX-Net/start"],
        "cwd": "/Applications/XX-Net",
        "python_executable": "/opt/homebrew/bin/python3",
        "config_source_category": "existing_user_config",
        "process_id": 1234,
        "process_generation": "generation-e2",
        "source": {
            "code_root": "/source/XX-Net/code/default",
            "code_sha256": "a" * 64,
        },
        "deployment": {
            "code_root": "/Applications/XX-Net/code/default",
            "code_sha256": "a" * 64,
            "backup_verified": True,
            "atomic": True,
        },
        "preflight": {
            "would_auto_install": False,
            "would_unpack": False,
            "would_escalate": False,
            "would_modify_system": False,
            "start_invoked": True,
        },
        "desktop": {
            "page_open_request": "succeeded",
            "tray_backend": "native",
            "tray_state": "running",
            "tray_menu_observed": False,
            "normal_exit_observed": False,
        },
        "dashboard": {
            "http_status": 200,
            "six_regions": True,
            "console_errors": 0,
            "charts_present": True,
            "tables_present": True,
            "single_inflight": True,
            "stale": False,
            "process_generation": "generation-e2",
        },
        "probe_document": {
            "schema_version": 1,
            "process_generation": "generation-e2",
            "probes": [
                probe("control"), probe("http"), probe("https"), probe("socks5"),
                probe("direct", 16, 1048576), probe("proxy", 16, 1048576),
            ],
        },
        "observation": {
            "started_at": 1000.0,
            "ended_at": 1065.0,
            "valid_samples": [
                {"sampled_at": 1000.0 + index * 10, "process_generation": "generation-e2",
                 "collection": "ok", "modules": runtime_modules()}
                for index in range(7)
            ],
            "gap_count": 0,
        },
    }


class ProductionValidationTest(unittest.TestCase):
    def test_deployment_plan_contains_only_portable_frozen_files(self):
        document = json.loads((TOOLS / "deployment-plan.json").read_text(encoding="utf-8"))
        self.assertEqual({"files"}, set(document))
        self.assertEqual(25, len(document["files"]))

    def test_complete_e2_fixture_passes_and_verdict_binds_raw_hash(self):
        document = valid_evidence()
        self.assertEqual([], validate_production.validate_document(document))
        with tempfile.TemporaryDirectory() as directory:
            evidence = pathlib.Path(directory) / "evidence.json"
            verdict = pathlib.Path(directory) / "verdict.json"
            evidence.write_text(json.dumps(document), encoding="utf-8")
            with mock.patch.object(validate_production, "_frozen_file_list", return_value=["a.txt"]), \
                    mock.patch.object(validate_production, "_fingerprint_code", return_value="a" * 64):
                self.assertEqual(0, validate_production.main([str(evidence), "--out", str(verdict)]))
            result = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual("passed", result["status"])
            self.assertEqual(sha(evidence.read_bytes()), result["evidence_sha256"])
            self.assertEqual("generation-e2", result["process_generation"])
            self.assertEqual("a" * 64, result["code_sha256"])
            self.assertEqual("/Applications/XX-Net/code/default", result["code_root"])

    def test_headless_argv_cannot_pass_as_e2(self):
        document = valid_evidence()
        document["argv"] = ["/Applications/XX-Net/start", "-no_popup", "-no_systray"]
        self.assertIn("DEFAULT_ENTRY_REQUIRED", validate_production.validate_document(document))

    def test_missing_page_and_tray_facts_fail(self):
        document = valid_evidence()
        document["desktop"]["page_open_request"] = "unknown"
        document["desktop"]["tray_state"] = "unknown"
        errors = validate_production.validate_document(document)
        self.assertIn("PAGE_OPEN_REQUIRED", errors)
        self.assertIn("TRAY_RUNNING_REQUIRED", errors)

    def test_wrong_code_root_hash_and_generation_fail(self):
        document = valid_evidence()
        document["deployment"]["code_root"] = "/tmp/unexpected"
        document["deployment"]["code_sha256"] = "b" * 64
        document["dashboard"]["process_generation"] = "other-generation"
        errors = validate_production.validate_document(document)
        self.assertIn("PRODUCTION_CODE_ROOT_MISMATCH", errors)
        self.assertIn("DEPLOYED_CODE_HASH_MISMATCH", errors)
        self.assertIn("PROCESS_GENERATION_MISMATCH", errors)

    def test_code_hashes_are_recomputed_from_frozen_file_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "a.txt").write_bytes(b"current-a")
            (target / "a.txt").write_bytes(b"current-a")
            document = valid_evidence()
            document["source"]["code_root"] = str(source)
            document["deployment"]["code_root"] = str(target)
            actual = fingerprint(source, ["a.txt"])
            document["source"]["code_sha256"] = actual
            document["deployment"]["code_sha256"] = actual
            errors = validate_production.validate_document(document, ["a.txt"])
            self.assertNotIn("SOURCE_CODE_HASH_MISMATCH", errors)
            self.assertNotIn("PRODUCTION_CODE_HASH_MISMATCH", errors)

            document["source"]["code_sha256"] = "a" * 64
            document["deployment"]["code_sha256"] = "a" * 64
            errors = validate_production.validate_document(document, ["a.txt"])
            self.assertIn("SOURCE_CODE_HASH_MISMATCH", errors)
            self.assertIn("PRODUCTION_CODE_HASH_MISMATCH", errors)

            document["source"]["code_sha256"] = actual
            document["deployment"]["code_sha256"] = actual
            (target / "a.txt").write_bytes(b"drifted-a")
            self.assertIn("PRODUCTION_CODE_HASH_MISMATCH",
                          validate_production.validate_document(document, ["a.txt"]))

    def test_no_probe_or_runtime_sample_fails(self):
        document = valid_evidence()
        document["probe_document"]["probes"] = []
        document["observation"]["valid_samples"] = []
        errors = validate_production.validate_document(document)
        self.assertIn("PROBE_CATEGORIES_INCOMPLETE", errors)
        self.assertIn("INSUFFICIENT_VALID_SAMPLES", errors)

    def test_failed_or_short_range_probe_fails(self):
        document = valid_evidence()
        direct = next(item for item in document["probe_document"]["probes"] if item["category"] == "direct")
        direct["complete_successes"] = 15
        direct["failures"]["truncated"] = 1
        direct["status"] = "failed"
        direct["actual_bytes"] = 1000
        errors = validate_production.validate_document(document)
        self.assertIn("PROBE_NOT_COMPLETE", errors)
        self.assertIn("RANGE_PROBE_INCOMPLETE", errors)

    def test_dashboard_errors_or_stale_fail(self):
        document = valid_evidence()
        document["dashboard"]["console_errors"] = 1
        document["dashboard"]["stale"] = True
        errors = validate_production.validate_document(document)
        self.assertIn("DASHBOARD_CONSOLE_ERRORS", errors)
        self.assertIn("DASHBOARD_STALE", errors)

    def test_short_window_and_five_samples_fail(self):
        document = valid_evidence()
        document["observation"]["ended_at"] = 1059.0
        document["observation"]["valid_samples"] = document["observation"]["valid_samples"][:5]
        errors = validate_production.validate_document(document)
        self.assertIn("OBSERVATION_WINDOW_TOO_SHORT", errors)
        self.assertIn("INSUFFICIENT_VALID_SAMPLES", errors)

    def test_sample_burst_cannot_impersonate_sixty_second_observation(self):
        document = valid_evidence()
        for index, sample in enumerate(document["observation"]["valid_samples"]):
            sample["sampled_at"] = 1000.0 + index * 0.1
        self.assertIn("OBSERVATION_SAMPLE_WINDOW_TOO_SHORT",
                      validate_production.validate_document(document))

    def test_stopped_required_module_cannot_be_a_healthy_sample(self):
        document = valid_evidence()
        document["observation"]["valid_samples"][0]["modules"]["x_tunnel"] = {
            "state": "not_running", "collection": "not_running",
        }
        self.assertIn("REQUIRED_MODULE_NOT_RUNNING",
                      validate_production.validate_document(document))

    def test_stopped_pipe_cannot_be_a_healthy_sample(self):
        document = valid_evidence()
        metrics = document["observation"]["valid_samples"][0]["modules"]["smart_router"]["metrics"]
        metrics["smart_router.pipe_running"] = 0
        self.assertIn("PIPE_NOT_RUNNING", validate_production.validate_document(document))

    def test_pipe_thread_failures_cannot_be_a_healthy_sample(self):
        for field, expected in (
                ("smart_router.pipe_select_failures_total", "PIPE_SELECT_FAILURE_OBSERVED"),
                ("smart_router.pipe_loop_failures_total", "PIPE_LOOP_FAILURE_OBSERVED")):
            with self.subTest(field=field):
                document = valid_evidence()
                document["observation"]["valid_samples"][0]["modules"]["smart_router"]["metrics"][field] = 1
                self.assertIn(expected, validate_production.validate_document(document))

    def test_handoff_total_must_equal_the_three_rejection_causes(self):
        document = valid_evidence()
        metrics = document["observation"]["valid_samples"][0]["modules"]["smart_router"]["metrics"]
        metrics["smart_router.handoff_failures_total"] = 2
        metrics["smart_router.pipe_closed_socket_handoffs_total"] = 1
        self.assertIn("PIPE_HANDOFF_COUNT_MISMATCH", validate_production.validate_document(document))

    def test_missing_or_sensitive_pipe_metrics_are_rejected(self):
        document = valid_evidence()
        metrics = document["observation"]["valid_samples"][0]["modules"]["smart_router"]["metrics"]
        del metrics["smart_router.pipe_running"]
        metrics["host"] = "not-allowed"
        self.assertIn("INVALID_PIPE_METRICS", validate_production.validate_document(document))

    def test_extra_sensitive_probe_field_is_rejected(self):
        document = valid_evidence()
        document["probe_document"]["probes"][0]["url"] = "not-allowed"
        self.assertIn("INVALID_PROBE_SCHEMA", validate_production.validate_document(document))

    def test_cli_invalid_input_returns_one_and_failed_verdict(self):
        document = valid_evidence()
        document["observation"]["ended_at"] = 1001.0
        with tempfile.TemporaryDirectory() as directory:
            evidence = pathlib.Path(directory) / "evidence.json"
            verdict = pathlib.Path(directory) / "verdict.json"
            evidence.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(1, validate_production.main([str(evidence), "--out", str(verdict)]))
            result = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual("failed", result["status"])
            self.assertIn("OBSERVATION_WINDOW_TOO_SHORT", result["errors"])


class DeployerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.backup = self.root / "backup"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_deploy_verifies_old_hash_backup_and_new_file(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.source, "nested/b.txt", b"new-b")
        self.write(self.target, "a.txt", b"old-a")
        result = deploy.deploy_files(
            self.source, self.target, ["a.txt", "nested/b.txt"],
            {"a.txt": sha(b"old-a"), "nested/b.txt": None}, self.backup,
        )
        self.assertEqual("deployed", result["status"])
        self.assertEqual(b"new-a", (self.target / "a.txt").read_bytes())
        self.assertEqual(b"new-b", (self.target / "nested/b.txt").read_bytes())
        self.assertEqual(b"old-a", (self.backup / "a.txt").read_bytes())
        self.assertFalse((self.backup / "nested/b.txt").exists())

    def test_old_hash_mismatch_stops_before_any_write(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.target, "a.txt", b"actual-old")
        with self.assertRaisesRegex(deploy.DeploymentError, "OLD_HASH_MISMATCH"):
            deploy.deploy_files(self.source, self.target, ["a.txt"], {"a.txt": sha(b"different")}, self.backup)
        self.assertEqual(b"actual-old", (self.target / "a.txt").read_bytes())
        self.assertFalse(self.backup.exists())

    def test_absolute_parent_and_symlink_paths_are_rejected(self):
        for relative in ("../outside", "/absolute"):
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(deploy.DeploymentError, "UNEXPECTED_PATH"):
                    deploy.deploy_files(self.source, self.target, [relative], {relative: None}, self.backup)
        self.write(self.source, "real.txt", b"new")
        (self.source / "link.txt").symlink_to(self.source / "real.txt")
        with self.assertRaisesRegex(deploy.DeploymentError, "UNEXPECTED_PATH"):
            deploy.deploy_files(self.source, self.target, ["link.txt"], {"link.txt": None}, self.backup)

    def test_backup_failure_leaves_target_unchanged(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.target, "a.txt", b"old-a")
        with mock.patch.object(deploy.shutil, "copy2", side_effect=OSError("injected")):
            with self.assertRaisesRegex(deploy.DeploymentError, "BACKUP_FAILED"):
                deploy.deploy_files(self.source, self.target, ["a.txt"], {"a.txt": sha(b"old-a")}, self.backup)
        self.assertEqual(b"old-a", (self.target / "a.txt").read_bytes())

    def test_partial_replace_rolls_back_first_file(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.source, "b.txt", b"new-b")
        self.write(self.target, "a.txt", b"old-a")
        self.write(self.target, "b.txt", b"old-b")
        real_replace = os.replace

        def fail_second(source, target):
            if pathlib.Path(target).name == "b.txt" and ".deploy-staging-" in str(source):
                raise OSError("injected partial write")
            return real_replace(source, target)

        with mock.patch.object(deploy.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(deploy.DeploymentError, "DEPLOYMENT_FAILED"):
                deploy.deploy_files(
                    self.source, self.target, ["a.txt", "b.txt"],
                    {"a.txt": sha(b"old-a"), "b.txt": sha(b"old-b")}, self.backup,
                )
        self.assertEqual(b"old-a", (self.target / "a.txt").read_bytes())
        self.assertEqual(b"old-b", (self.target / "b.txt").read_bytes())

    def test_abnormal_exit_rolls_back_and_removes_new_file(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.source, "b.txt", b"new-b")
        real_replace = os.replace

        def fail_second(source, target):
            if pathlib.Path(target).name == "b.txt" and ".deploy-staging-" in str(source):
                raise KeyboardInterrupt()
            return real_replace(source, target)

        with mock.patch.object(deploy.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(deploy.DeploymentError, "DEPLOYMENT_FAILED"):
                deploy.deploy_files(self.source, self.target, ["a.txt", "b.txt"], {"a.txt": None, "b.txt": None}, self.backup)
        self.assertFalse((self.target / "a.txt").exists())
        self.assertFalse((self.target / "b.txt").exists())

    def test_target_symlink_is_not_followed(self):
        self.write(self.source, "a.txt", b"new-a")
        self.write(self.root, "outside.txt", b"outside")
        (self.target / "a.txt").symlink_to(self.root / "outside.txt")
        with self.assertRaisesRegex(deploy.DeploymentError, "UNEXPECTED_PATH"):
            deploy.deploy_files(self.source, self.target, ["a.txt"], {"a.txt": sha(b"outside")}, self.backup)
        self.assertEqual(b"outside", (self.root / "outside.txt").read_bytes())


class StartBoundaryTest(unittest.TestCase):
    def test_auto_install_preflight_blocks_without_invoking_entry(self):
        runner = mock.Mock()
        result = production_probe.run_start_after_preflight({
            "would_auto_install": True, "would_unpack": False,
            "would_escalate": False, "would_modify_system": False,
        }, runner)
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("AUTO_INSTALL_REQUIRED", result["reason"])
        self.assertFalse(result["start_invoked"])
        runner.assert_not_called()

    def test_safe_preflight_invokes_only_default_entry(self):
        runner = mock.Mock(return_value="process-token")
        result = production_probe.run_start_after_preflight({
            "would_auto_install": False, "would_unpack": False,
            "would_escalate": False, "would_modify_system": False,
        }, runner)
        self.assertEqual("STARTED", result["status"])
        self.assertTrue(result["start_invoked"])
        runner.assert_called_once_with(["/Applications/XX-Net/start"], cwd="/Applications/XX-Net")


class ProductionProbeTest(unittest.TestCase):
    def test_signed_url_requires_explicit_environment(self):
        with mock.patch.dict(production_probe.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "RANGE_URL_REQUIRED"):
                production_probe.find_signed_url()

    def test_signed_url_uses_explicit_environment(self):
        url = "https://example.invalid/file?explicit=1"
        with mock.patch.dict(production_probe.os.environ, {"XXNET_RANGE_PROBE_URL": url}, clear=True):
            self.assertTrue(production_probe.find_signed_url() == url)

    def test_range_probe_summary_is_whitelisted_and_complete(self):
        def request(parts, route):
            return {
                "ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
                "latency_seconds": 0.5, "content_sha256": "a" * 64,
            }

        probes = production_probe.run_range_probes("https://example.invalid/file", 2, request)
        self.assertEqual(["direct", "proxy"], [item["category"] for item in probes])
        for item in probes:
            self.assertEqual("complete", item["status"])
            self.assertEqual(2, item["attempts"])
            self.assertEqual(2, item["complete_successes"])
            self.assertEqual(1048576, item["actual_bytes"])
            self.assertEqual(1.0, item["latency_seconds"])
            self.assertEqual("a" * 64, item["content_sha256"])
            self.assertEqual({
                "category", "status", "attempts", "complete_successes", "failures",
                "expected_bytes", "actual_bytes", "latency_seconds", "content_sha256",
            }, set(item))

    def test_range_probe_summary_classifies_truncated_attempt(self):
        outcomes = iter([
            {"ok": False, "failure": "truncated", "expected_bytes": 1048576,
             "actual_bytes": 1000, "latency_seconds": 0.25},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.5, "content_sha256": "a" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.5, "content_sha256": "a" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.5, "content_sha256": "a" * 64},
        ])

        def request(parts, route):
            return next(outcomes)

        direct, proxy = production_probe.run_range_probes("https://example.invalid/file", 2, request)
        self.assertEqual("failed", direct["status"])
        self.assertEqual(1, direct["complete_successes"])
        self.assertEqual(1, direct["failures"]["truncated"])
        self.assertEqual(1000, direct["actual_bytes"])
        self.assertNotIn("url", json.dumps(direct))
        self.assertEqual("complete", proxy["status"])

    def test_range_probe_detects_content_mismatch_and_rejects_sensitive_url(self):
        outcomes = iter([
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "a" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "a" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "b" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "a" * 64},
        ])
        direct = production_probe.run_range_probes(
            "https://example.invalid/file", 2, lambda parts, route: next(outcomes))[0]
        self.assertEqual(1, direct["failures"]["content_mismatch"])
        self.assertEqual("failed", direct["status"])
        with self.assertRaises(ValueError):
            production_probe.run_range_probes("http://example.invalid/file")
        with self.assertRaises(ValueError):
            production_probe.run_range_probes("https://user:secret@example.invalid/file")

    def test_range_probe_detects_cross_route_content_mismatch(self):
        outcomes = iter([
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "a" * 64},
            {"ok": True, "expected_bytes": 1048576, "actual_bytes": 1048576,
             "latency_seconds": 0.1, "content_sha256": "b" * 64},
        ])
        direct, proxy = production_probe.run_range_probes(
            "https://example.invalid/file", 1, lambda parts, route: next(outcomes))
        self.assertEqual("complete", direct["status"])
        self.assertEqual("failed", proxy["status"])
        self.assertEqual(1, proxy["failures"]["content_mismatch"])

    def test_range_request_requires_206_and_exact_content_range(self):
        for status, content_range, expected_failure in (
            (200, "bytes 0-1048575/2097152", "http_status"),
            (206, "bytes 1-1048576/2097152", "truncated"),
            (206, "bytes 0-1048575/2097152", None),
        ):
            with self.subTest(status=status, content_range=content_range):
                response = mock.Mock()
                response.status = status
                response.getheader.return_value = content_range
                response.read.return_value = b"x" * 1048576
                connection = mock.Mock()
                connection.getresponse.return_value = response
                with mock.patch.object(
                        production_probe.http.client, "HTTPSConnection", return_value=connection):
                    result = production_probe.one_range_request(
                        production_probe.urlsplit("https://example.invalid/file"), "direct")
                self.assertEqual(expected_failure is None, result["ok"])
                self.assertEqual(expected_failure, result["failure"])
                connection.close.assert_called_once_with()

    def test_range_request_counts_partial_exception_bytes_and_closes(self):
        class Response:
            status = 206

            def __init__(self):
                self.calls = 0

            def getheader(self, name):
                return "bytes 0-1048575/1048576"

            def read(self, amount):
                self.calls += 1
                if self.calls == 1:
                    return b"abc"
                raise production_probe.http.client.IncompleteRead(b"xy", 5)

        connection = mock.Mock()
        connection.getresponse.return_value = Response()
        with mock.patch.object(production_probe.http.client, "HTTPSConnection", return_value=connection):
            result = production_probe.one_range_request(
                production_probe.urlsplit("https://example.invalid/file"), "direct")
        self.assertFalse(result["ok"])
        self.assertEqual("truncated", result["failure"])
        self.assertEqual(5, result["actual_bytes"])
        connection.close.assert_called_once_with()
        self.assertEqual("bytes=0-1048575", connection.request.call_args.kwargs["headers"]["Range"])

    def test_entry_probe_classifies_http_status_without_leaking_details(self):
        class Response:
            status = 503

            def read(self):
                return b"private response body"

        connection = mock.Mock()
        connection.getresponse.return_value = Response()
        with mock.patch.object(production_probe, "_control", return_value=b""), \
                mock.patch.object(production_probe, "_https_proxy", return_value=b""), \
                mock.patch.object(production_probe, "_socks5_domain", return_value=b""), \
                mock.patch.object(production_probe.http.client, "HTTPConnection", return_value=connection):
            probes = production_probe.run_entry_probes()
        http = next(item for item in probes if item["category"] == "http")
        self.assertEqual(1, http["failures"]["http_status"])
        serialized = json.dumps(probes)
        self.assertNotIn("private response body", serialized)
        self.assertNotIn("www.gstatic.com", serialized)

    def test_probe_document_uses_authoritative_generation_before_and_after(self):
        def snapshot(generation, sampled_at):
            return {
                "schema_version": 1, "sampled_at": sampled_at,
                "process_generation": generation, "process": {}, "modules": runtime_modules(),
            }

        with mock.patch.object(production_probe, "fetch_runtime_snapshot",
                               side_effect=[snapshot("actual-generation", 1), snapshot("actual-generation", 2)]), \
                mock.patch.object(production_probe, "run_entry_probes", return_value=[]), \
                mock.patch.object(production_probe, "find_signed_url", return_value="https://example.invalid/file"), \
                mock.patch.object(production_probe, "run_range_probes", return_value=[]):
            result = production_probe.probe_document("claimed-generation")
        self.assertEqual("actual-generation", result["process_generation"])

        with mock.patch.object(production_probe, "fetch_runtime_snapshot",
                               side_effect=[snapshot("actual-generation", 1), snapshot("next-generation", 2)]), \
                mock.patch.object(production_probe, "run_entry_probes", return_value=[]), \
                mock.patch.object(production_probe, "find_signed_url", return_value="https://example.invalid/file"), \
                mock.patch.object(production_probe, "run_range_probes", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "PROCESS_GENERATION_MISMATCH"):
                production_probe.probe_document("claimed-generation")

    def test_observation_rejects_generation_and_collection_gaps(self):
        generation = "generation-e2"
        modules = runtime_modules()
        snapshots = iter([
            {"schema_version": 1, "sampled_at": 1000.0, "process_generation": generation,
             "process": {}, "modules": modules},
            {"schema_version": 1, "sampled_at": 1001.0, "process_generation": "old",
             "process": {}, "modules": modules},
            {"schema_version": 1, "sampled_at": 1002.0, "process_generation": generation,
             "process": {}, "modules": dict(modules, x_tunnel={"state": "unknown", "collection": "unknown"})},
            {"schema_version": 1, "sampled_at": 1003.0, "process_generation": generation,
             "process": {}, "modules": modules},
        ])
        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        wall = iter([1000.0, 1004.0])
        observation, samples = production_probe.observe_runtime(
            generation, 4, 0, lambda: next(snapshots), sleep=lambda _: None,
            monotonic=lambda: next(clock), wall=lambda: next(wall),
        )
        self.assertEqual(2, len(observation["valid_samples"]))
        self.assertEqual(2, observation["gap_count"])
        self.assertEqual(2, len(samples))
        self.assertEqual([1000.0, 1003.0], [item["sampled_at"] for item in observation["valid_samples"]])
        self.assertEqual(modules, observation["valid_samples"][0]["modules"])

    def test_stopped_required_module_is_not_a_valid_observation(self):
        snapshot = {
            "schema_version": 1, "sampled_at": 1000.0,
            "process_generation": "generation-e2", "process": {}, "modules": runtime_modules(),
        }
        self.assertTrue(production_probe._valid_snapshot(snapshot, "generation-e2"))
        snapshot["modules"]["x_tunnel"] = {"state": "not_running", "collection": "not_running"}
        self.assertFalse(production_probe._valid_snapshot(snapshot, "generation-e2"))

    def test_snapshot_requires_all_modules_and_fetch_exceptions_become_gaps(self):
        generation = "generation-e2"
        incomplete = {
            "schema_version": 1, "sampled_at": 1000.0,
            "process_generation": generation, "process": {},
            "modules": {"x_tunnel": {"collection": "ok"}},
        }
        self.assertFalse(production_probe._valid_snapshot(incomplete, generation))
        observation, samples = production_probe.observe_runtime(
            generation, 0, 0, lambda: (_ for _ in ()).throw(OSError("private")),
            sleep=lambda _: None, monotonic=lambda: 0.0,
            wall=iter([1000.0, 1000.0]).__next__,
        )
        self.assertEqual([], samples)
        self.assertEqual([], observation["valid_samples"])
        self.assertEqual(1, observation["gap_count"])

    def test_preflight_uses_selected_system_python_without_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "data/launcher").mkdir(parents=True)
            (root / "data/launcher/installed").touch()
            with mock.patch.object(production_probe, "_system_python", return_value="/trusted/python3"), \
                    mock.patch.object(production_probe, "_command_ok", return_value=True) as command_ok:
                result = production_probe.preflight_start(root)
        self.assertEqual("/trusted/python3", result["python_executable"])
        self.assertFalse(result["would_auto_install"])
        desktop_call = command_ok.call_args_list[0].args[0]
        self.assertEqual("/trusted/python3", desktop_call[0])
        self.assertEqual("-c", desktop_call[1])
        self.assertNotIn("-I", desktop_call)
        self.assertIn("PyObjCTools", desktop_call[2])

    def test_preflight_truth_table_blocks_every_forbidden_branch(self):
        safe = production_probe.evaluate_preflight(
            installed_marker=True, embedded_exists=False, embedded_usable=False,
            archive_exists=False, desktop_modules_available=True,
        )
        self.assertEqual({
            "would_auto_install": False, "would_unpack": False,
            "would_escalate": False, "would_modify_system": False,
        }, safe)
        self.assertTrue(production_probe.evaluate_preflight(
            False, False, False, False, True)["would_modify_system"])
        self.assertTrue(production_probe.evaluate_preflight(
            True, False, False, True, True)["would_unpack"])
        self.assertTrue(production_probe.evaluate_preflight(
            True, False, False, False, False)["would_auto_install"])
        self.assertFalse(production_probe.evaluate_preflight(
            True, True, True, True, False)["would_auto_install"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
