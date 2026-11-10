"""对离线、生产和桌面验收声明执行固定规则的证据门禁。"""
import argparse
import hashlib
import json
from pathlib import Path


LEVELS = {"E0", "E1", "E2", "E3"}
CLAIMS = {"offline", "headless_service", "default_startup", "full_desktop"}
STATUSES = {"CONVERGED", "BLOCKED", "FAILED"}
MANIFEST_FIELDS = (
    "total", "executed", "passed", "failed", "missing",
    "undeclared_skips", "timeouts", "retries",
)


def validate_evidence(document: dict, production_verdict: dict | None = None) -> list[str]:
    errors = []

    def add(code):
        if code not in errors:
            errors.append(code)

    if not isinstance(document, dict):
        return ["INVALID_DOCUMENT"]

    if document.get("schema_version") != 1:
        add("INVALID_SCHEMA_VERSION")
    level = document.get("production_level")
    if level not in LEVELS:
        add("INVALID_PRODUCTION_LEVEL")
    status = document.get("status")
    if status not in STATUSES:
        add("INVALID_STATUS")
    claims = document.get("claims")
    if not isinstance(claims, list) or not claims or any(claim not in CLAIMS for claim in claims):
        add("INVALID_CLAIM")
        claims = []
    if not isinstance(document.get("uncovered"), list) or (status == "CONVERGED" and not document["uncovered"]):
        add("UNCOVERED_REQUIRED")

    if level == "E0" and any(claim != "offline" for claim in claims):
        add("E0_CANNOT_CLAIM_PRODUCTION")
    if level == "E1" and "default_startup" in claims:
        add("E1_CANNOT_CLAIM_DEFAULT_STARTUP")
    if level in {"E0", "E1"} and "full_desktop" in claims:
        add("E1_CANNOT_CLAIM_DEFAULT_STARTUP")
    if level == "E2" and "full_desktop" in claims:
        add("E2_CANNOT_CLAIM_FULL_DESKTOP")

    samples = document.get("samples")
    if not isinstance(samples, dict):
        add("INVALID_SAMPLE_COUNTS")
    else:
        attempts = samples.get("attempts")
        successes = samples.get("complete_successes")
        rate = samples.get("success_rate")
        valid_counts = (
            isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0
            and isinstance(successes, int) and not isinstance(successes, bool)
            and 0 <= successes <= attempts
        )
        if not valid_counts:
            add("INVALID_SAMPLE_COUNTS")
        elif attempts == 0:
            if rate is not None:
                add("SUCCESS_RATE_WITHOUT_SAMPLE")
            if status == "CONVERGED":
                add("SAMPLES_REQUIRED")
        elif not isinstance(rate, (int, float)) or isinstance(rate, bool) or abs(rate - successes / attempts) > 1e-9:
            add("INVALID_SUCCESS_RATE")

    manifest = document.get("manifest")
    if not isinstance(manifest, dict) or any(
        not isinstance(manifest.get(key), int) or isinstance(manifest.get(key), bool) or manifest.get(key) < 0
        for key in MANIFEST_FIELDS
    ):
        add("INVALID_MANIFEST")
    elif status == "CONVERGED":
        complete = (
            manifest["total"] > 0
            and manifest["executed"] == manifest["total"]
            and manifest["passed"] == manifest["total"]
            and all(manifest[key] == 0 for key in MANIFEST_FIELDS[3:])
        )
        if not complete:
            add("MANIFEST_NOT_COMPLETE")

    if status == "CONVERGED":
        if level not in {"E2", "E3"}:
            add("CONVERGED_REQUIRES_E2")
        if "default_startup" not in claims:
            add("CONVERGED_REQUIRES_DEFAULT_STARTUP")

    needs_production = level in {"E1", "E2", "E3"} or any(claim != "offline" for claim in claims)
    if needs_production:
        if not isinstance(production_verdict, dict):
            add("PRODUCTION_VERDICT_REQUIRED")
        else:
            if production_verdict.get("status") != "passed":
                add("PRODUCTION_VERDICT_NOT_PASSED")
            if production_verdict.get("production_level") != level:
                add("PRODUCTION_LEVEL_MISMATCH")
            if production_verdict.get("evidence_sha256") != document.get("production_evidence_sha256"):
                add("PRODUCTION_EVIDENCE_HASH_MISMATCH")
            if production_verdict.get("process_generation") != document.get("process_generation"):
                add("PRODUCTION_GENERATION_MISMATCH")
            if production_verdict.get("samples") != samples:
                add("PRODUCTION_SAMPLE_MISMATCH")

    if level in {"E2", "E3"} or any(claim in {"default_startup", "full_desktop"} for claim in claims):
        production = document.get("production")
        if not isinstance(production, dict):
            add("INVALID_PRODUCTION_PREFLIGHT")
            production = {}
        facts = production_verdict.get("facts", {}) if isinstance(production_verdict, dict) else {}
        if production.get("would_auto_install") is True or facts.get("would_auto_install") is True:
            add("AUTO_INSTALL_BLOCKS_STARTUP_CLAIM")
        if production.get("start_invoked") is not True or facts.get("start_invoked") is not True:
            add("START_NOT_INVOKED")

    if level == "E3" or "full_desktop" in claims:
        facts = production_verdict.get("facts", {}) if isinstance(production_verdict, dict) else {}
        if facts.get("tray_menu_observed") is not True or facts.get("normal_exit_observed") is not True:
            add("E3_FACTS_REQUIRED")

    return errors


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("--production-evidence")
    parser.add_argument("--production-verdict")
    args = parser.parse_args(argv)
    try:
        document = json.loads(Path(args.evidence).read_text())
        verdict = json.loads(Path(args.production_verdict).read_text()) if args.production_verdict else None
    except (OSError, ValueError, TypeError):
        print("INVALID_JSON")
        return 1

    errors = validate_evidence(document, verdict)
    claims = document.get("claims", []) if isinstance(document, dict) else []
    level = document.get("production_level") if isinstance(document, dict) else None
    needs_production = level in {"E1", "E2", "E3"} or any(claim != "offline" for claim in claims)
    if needs_production and not args.production_evidence:
        if "PRODUCTION_EVIDENCE_REQUIRED" not in errors:
            errors.append("PRODUCTION_EVIDENCE_REQUIRED")
    if args.production_evidence:
        try:
            actual_hash = hashlib.sha256(Path(args.production_evidence).read_bytes()).hexdigest()
        except OSError:
            actual_hash = None
        if actual_hash != document.get("production_evidence_sha256") or actual_hash != (
            verdict.get("evidence_sha256") if isinstance(verdict, dict) else None
        ):
            if "PRODUCTION_EVIDENCE_HASH_MISMATCH" not in errors:
                errors.append("PRODUCTION_EVIDENCE_HASH_MISMATCH")
    for error in errors:
        print(error)
    if errors:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
