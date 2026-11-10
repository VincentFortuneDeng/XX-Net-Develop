"""XX-Net 生产证据校验器；两段式门禁的第一段。"""
import argparse
import hashlib
import json
import math
from pathlib import Path


TOP_FIELDS = [
    "schema_version", "production_level", "entry", "argv", "cwd",
    "python_executable", "config_source_category", "process_id", "process_generation",
    "source", "deployment", "preflight", "desktop", "dashboard",
    "probe_document", "observation",
]
PROBE_CATEGORIES = {"control", "http", "https", "socks5", "direct", "proxy"}
FAILURE_NAMES = ["timeout", "truncated", "connection", "http_status", "content_mismatch", "other"]
PROBE_FIELDS = [
    "category", "status", "attempts", "complete_successes", "failures",
    "expected_bytes", "actual_bytes", "latency_seconds",
]
PIPE_METRIC_NAMES = [
    "smart_router.pipe_running",
    "smart_router.direct_connect_failures_total",
    "smart_router.handoff_failures_total",
    "smart_router.pipe_register_failures_total",
    "smart_router.pipe_stopped_handoffs_total",
    "smart_router.pipe_closed_socket_handoffs_total",
    "smart_router.pipe_select_failures_total",
    "smart_router.pipe_loop_failures_total",
]
PIPE_REJECTION_NAMES = [
    "smart_router.pipe_register_failures_total",
    "smart_router.pipe_stopped_handoffs_total",
    "smart_router.pipe_closed_socket_handoffs_total",
]


def _object(value, fields, optional=()):
    if not isinstance(value, dict):
        return False
    return all(field in value for field in fields) and set(value) <= set(fields) | set(optional)


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _validate_probe(probe):
    if not _object(probe, PROBE_FIELDS, ["content_sha256"]):
        return False
    if probe["category"] not in PROBE_CATEGORIES or probe["status"] not in {"idle", "running", "complete", "failed"}:
        return False
    if not all(_integer(probe[field]) for field in ("attempts", "complete_successes", "expected_bytes", "actual_bytes")):
        return False
    if probe["complete_successes"] > probe["attempts"] or not _object(probe["failures"], FAILURE_NAMES):
        return False
    if any(not _integer(probe["failures"][name]) for name in FAILURE_NAMES):
        return False
    failures = sum(probe["failures"][name] for name in FAILURE_NAMES)
    if probe["complete_successes"] + failures > probe["attempts"]:
        return False
    if probe["status"] == "complete" and (probe["complete_successes"] != probe["attempts"] or failures):
        return False
    if probe["status"] == "failed" and not failures:
        return False
    if not _number(probe["latency_seconds"]) or probe["latency_seconds"] < 0:
        return False
    if "content_sha256" in probe and not _hash(probe["content_sha256"]):
        return False
    return True


def _fingerprint_code(root, relative_files):
    root = Path(root)
    entries = []
    for name in sorted(relative_files):
        relative = Path(name)
        if not isinstance(name, str) or not name or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid code manifest path")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise OSError("code file unavailable")
        entries.append({"file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _frozen_file_list():
    document = json.loads(Path(__file__).with_name("deployment-plan.json").read_text(encoding="utf-8"))
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("invalid code manifest")
    names = [item.get("file") if isinstance(item, dict) else None for item in files]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("invalid code manifest")
    return names


def validate_document(document, file_paths=None):
    if not _object(document, TOP_FIELDS) or document.get("schema_version") != 1:
        return ["INVALID_PRODUCTION_SCHEMA"]
    errors = []

    def add(code):
        if code not in errors:
            errors.append(code)

    generation = document["process_generation"]
    if not isinstance(generation, str) or not generation:
        add("PROCESS_GENERATION_MISMATCH")
        generation = ""
    if document["production_level"] not in {"E2", "E3"}:
        add("PRODUCTION_LEVEL_REQUIRES_E2")
    if (document["entry"] != "/Applications/XX-Net/start" or
            document["argv"] != ["/Applications/XX-Net/start"] or
            document["cwd"] != "/Applications/XX-Net"):
        add("DEFAULT_ENTRY_REQUIRED")
    if not isinstance(document["python_executable"], str) or not document["python_executable"].startswith("/"):
        add("INVALID_PYTHON_EXECUTABLE")
    if document["config_source_category"] not in {"existing_user_config", "fresh_default_config"}:
        add("INVALID_CONFIG_SOURCE")
    if not _integer(document["process_id"], 1):
        add("INVALID_PROCESS_ID")

    source = document["source"] if _object(document["source"], ["code_root", "code_sha256"]) else {}
    deployment = document["deployment"] if _object(
        document["deployment"], ["code_root", "code_sha256", "backup_verified", "atomic"]) else {}
    if not source or not deployment:
        add("INVALID_CODE_FACTS")
    if deployment.get("code_root") != "/Applications/XX-Net/code/default":
        add("PRODUCTION_CODE_ROOT_MISMATCH")
    if not isinstance(source.get("code_root"), str) or not source.get("code_root", "").startswith("/"):
        add("SOURCE_CODE_ROOT_MISMATCH")
    if not _hash(source.get("code_sha256")) or not _hash(deployment.get("code_sha256")):
        add("INVALID_CODE_HASH")
    elif source["code_sha256"] != deployment["code_sha256"]:
        add("DEPLOYED_CODE_HASH_MISMATCH")
    if deployment.get("backup_verified") is not True or deployment.get("atomic") is not True:
        add("DEPLOYMENT_INTEGRITY_REQUIRED")
    if file_paths is not None:
        for facts, error in (
            (source, "SOURCE_CODE_HASH_MISMATCH"),
            (deployment, "PRODUCTION_CODE_HASH_MISMATCH"),
        ):
            try:
                actual_hash = _fingerprint_code(facts.get("code_root"), file_paths)
            except (OSError, TypeError, ValueError):
                add("CODE_FINGERPRINT_UNAVAILABLE")
            else:
                if facts.get("code_sha256") != actual_hash:
                    add(error)

    preflight_fields = ["would_auto_install", "would_unpack", "would_escalate", "would_modify_system", "start_invoked"]
    preflight = document["preflight"] if _object(document["preflight"], preflight_fields) else {}
    if not preflight:
        add("INVALID_PRODUCTION_PREFLIGHT")
    for field in preflight_fields:
        if field in preflight and not isinstance(preflight[field], bool):
            add("INVALID_PRODUCTION_PREFLIGHT")
    if preflight.get("would_auto_install") is True:
        add("AUTO_INSTALL_REQUIRED")
    if preflight.get("would_unpack") is True:
        add("UNPACK_REQUIRED")
    if preflight.get("would_escalate") is True:
        add("ESCALATION_REQUIRED")
    if preflight.get("would_modify_system") is True:
        add("SYSTEM_MODIFICATION_REQUIRED")
    if preflight.get("start_invoked") is not True:
        add("START_NOT_INVOKED")

    desktop_fields = ["page_open_request", "tray_backend", "tray_state", "tray_menu_observed", "normal_exit_observed"]
    desktop = document["desktop"] if _object(document["desktop"], desktop_fields) else {}
    if not desktop:
        add("INVALID_DESKTOP_FACTS")
    if desktop.get("page_open_request") != "succeeded":
        add("PAGE_OPEN_REQUIRED")
    if desktop.get("tray_backend") not in {"native", "fallback"}:
        add("TRAY_BACKEND_REQUIRED")
    if desktop.get("tray_state") != "running":
        add("TRAY_RUNNING_REQUIRED")
    if not isinstance(desktop.get("tray_menu_observed"), bool) or not isinstance(desktop.get("normal_exit_observed"), bool):
        add("INVALID_DESKTOP_FACTS")
    if document["production_level"] == "E3" and (
            desktop.get("tray_menu_observed") is not True or desktop.get("normal_exit_observed") is not True):
        add("E3_FACTS_REQUIRED")

    dashboard_fields = [
        "http_status", "six_regions", "console_errors", "charts_present",
        "tables_present", "single_inflight", "stale", "process_generation",
    ]
    dashboard = document["dashboard"] if _object(document["dashboard"], dashboard_fields) else {}
    if not dashboard:
        add("INVALID_DASHBOARD_FACTS")
    if dashboard.get("http_status") != 200:
        add("DASHBOARD_HTTP_ERROR")
    if dashboard.get("six_regions") is not True:
        add("DASHBOARD_REGIONS_MISSING")
    if dashboard.get("console_errors") != 0 or not _integer(dashboard.get("console_errors")):
        add("DASHBOARD_CONSOLE_ERRORS")
    for field in ("charts_present", "tables_present", "single_inflight"):
        if dashboard.get(field) is not True:
            add("DASHBOARD_" + field.upper())
    if dashboard.get("stale") is not False:
        add("DASHBOARD_STALE")
    if dashboard.get("process_generation") != generation:
        add("PROCESS_GENERATION_MISMATCH")

    probe_document = document["probe_document"]
    if not _object(probe_document, ["schema_version", "process_generation", "probes"]) or probe_document.get("schema_version") != 1:
        add("INVALID_PROBE_SCHEMA")
        probes = []
    else:
        if probe_document["process_generation"] != generation:
            add("PROCESS_GENERATION_MISMATCH")
        probes = probe_document["probes"] if isinstance(probe_document["probes"], list) else []
        if not isinstance(probe_document["probes"], list):
            add("INVALID_PROBE_SCHEMA")
    if any(not _validate_probe(item) for item in probes):
        add("INVALID_PROBE_SCHEMA")
    by_category = {}
    for item in probes:
        if isinstance(item, dict) and isinstance(item.get("category"), str):
            if item["category"] in by_category:
                add("INVALID_PROBE_SCHEMA")
            by_category[item["category"]] = item
    if set(by_category) != PROBE_CATEGORIES:
        add("PROBE_CATEGORIES_INCOMPLETE")
    for category, item in by_category.items():
        if not _validate_probe(item):
            continue
        failures = sum(item["failures"][name] for name in FAILURE_NAMES)
        if (item["status"] != "complete" or item["attempts"] < 1 or
                item["complete_successes"] != item["attempts"] or failures or
                item["actual_bytes"] != item["expected_bytes"]):
            add("PROBE_NOT_COMPLETE")
        if category in {"direct", "proxy"} and (
                item["attempts"] < 16 or item["expected_bytes"] != 1048576 or item["actual_bytes"] != 1048576):
            add("RANGE_PROBE_INCOMPLETE")

    observation = document["observation"] if _object(
        document["observation"], ["started_at", "ended_at", "valid_samples", "gap_count"]) else {}
    if not observation:
        add("INVALID_OBSERVATION")
        samples = []
    else:
        samples = observation["valid_samples"] if isinstance(observation["valid_samples"], list) else []
        if not _number(observation["started_at"]) or not _number(observation["ended_at"]):
            add("INVALID_OBSERVATION")
        elif observation["ended_at"] - observation["started_at"] < 60:
            add("OBSERVATION_WINDOW_TOO_SHORT")
        if not _integer(observation["gap_count"]):
            add("INVALID_OBSERVATION")
    if len(samples) < 6:
        add("INSUFFICIENT_VALID_SAMPLES")
    previous_time = None
    valid_sample_times = []
    for sample in samples:
        if not _object(sample, ["sampled_at", "process_generation", "collection", "modules"]):
            add("INVALID_RUNTIME_SAMPLE")
            continue
        if sample["process_generation"] != generation or sample["collection"] != "ok":
            add("PROCESS_GENERATION_MISMATCH")
        if not _number(sample["sampled_at"]):
            add("INVALID_RUNTIME_SAMPLE")
            continue
        modules = sample["modules"]
        if not _object(modules, ["gae_proxy", "smart_router", "x_tunnel"]):
            add("INVALID_RUNTIME_SAMPLE")
            continue
        module_values = list(modules.values())
        if any(not _object(item, ["state", "collection"], ["metrics"]) for item in module_values):
            add("INVALID_RUNTIME_SAMPLE")
            continue
        for name in ("smart_router", "x_tunnel"):
            if modules[name]["state"] != "running" or modules[name]["collection"] != "ok":
                add("REQUIRED_MODULE_NOT_RUNNING")
        pipe_metrics = modules["smart_router"].get("metrics")
        if not _object(pipe_metrics, PIPE_METRIC_NAMES) or any(
                not _integer(pipe_metrics.get(name)) for name in PIPE_METRIC_NAMES):
            add("INVALID_PIPE_METRICS")
        else:
            if pipe_metrics["smart_router.pipe_running"] != 1:
                add("PIPE_NOT_RUNNING")
            if pipe_metrics["smart_router.pipe_select_failures_total"] != 0:
                add("PIPE_SELECT_FAILURE_OBSERVED")
            if pipe_metrics["smart_router.pipe_loop_failures_total"] != 0:
                add("PIPE_LOOP_FAILURE_OBSERVED")
            if pipe_metrics["smart_router.handoff_failures_total"] != sum(
                    pipe_metrics[name] for name in PIPE_REJECTION_NAMES):
                add("PIPE_HANDOFF_COUNT_MISMATCH")
        gae = modules["gae_proxy"]
        if (gae["state"], gae["collection"]) not in {
                ("running", "ok"), ("not_running", "not_running")}:
            add("INVALID_RUNTIME_SAMPLE")
        if previous_time is not None and sample["sampled_at"] < previous_time:
            add("INVALID_RUNTIME_SAMPLE")
        previous_time = sample["sampled_at"]
        valid_sample_times.append(sample["sampled_at"])
        if observation and _number(observation.get("started_at")) and _number(observation.get("ended_at")) and not (
                observation["started_at"] <= sample["sampled_at"] <= observation["ended_at"]):
            add("INVALID_RUNTIME_SAMPLE")
    if valid_sample_times and valid_sample_times[-1] - valid_sample_times[0] < 60:
        add("OBSERVATION_SAMPLE_WINDOW_TOO_SHORT")
    return errors


def _verdict(document, evidence_hash, errors):
    preflight = document.get("preflight", {}) if isinstance(document, dict) else {}
    desktop = document.get("desktop", {}) if isinstance(document, dict) else {}
    deployment = document.get("deployment", {}) if isinstance(document, dict) else {}
    probe_document = document.get("probe_document", {}) if isinstance(document, dict) else {}
    probes = probe_document.get("probes", []) if isinstance(probe_document, dict) else []
    if isinstance(probes, list) and all(
            isinstance(item, dict) and _integer(item.get("attempts")) and
            _integer(item.get("complete_successes")) for item in probes):
        attempts = sum(item["attempts"] for item in probes)
        successes = sum(item["complete_successes"] for item in probes)
        samples = {
            "attempts": attempts,
            "complete_successes": successes,
            "success_rate": successes / attempts if attempts else None,
        }
    else:
        samples = None
    return {
        "schema_version": 1,
        "status": "failed" if errors else "passed",
        "production_level": document.get("production_level") if isinstance(document, dict) else None,
        "evidence_sha256": evidence_hash,
        "process_generation": document.get("process_generation") if isinstance(document, dict) else None,
        "samples": samples,
        "code_sha256": deployment.get("code_sha256"),
        "code_root": deployment.get("code_root"),
        "facts": {
            "start_invoked": preflight.get("start_invoked") is True,
            "would_auto_install": preflight.get("would_auto_install") is True,
            "tray_menu_observed": desktop.get("tray_menu_observed") is True,
            "normal_exit_observed": desktop.get("normal_exit_observed") is True,
        },
        "errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    evidence_path = Path(args.evidence)
    try:
        raw = evidence_path.read_bytes()
        evidence_hash = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        file_paths = _frozen_file_list()
        errors = validate_document(document, file_paths)
    except (OSError, ValueError, TypeError):
        evidence_hash = None
        document = {}
        errors = ["INVALID_JSON"]
    verdict = _verdict(document, evidence_hash, errors)
    try:
        Path(args.out).write_text(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        print("VERDICT_WRITE_FAILED")
        return 1
    for error in errors:
        print(error)
    if errors:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
