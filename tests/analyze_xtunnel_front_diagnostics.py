import argparse
import json
from pathlib import Path
import re


TID_PATTERN = re.compile(r"\btid=(\d+)")
FRONT_PATTERN = re.compile(r"\bfront:([A-Za-z0-9_.-]+)")
STATUS_PATTERN = re.compile(r"\bstatus:(\d+)")
SELECT_PATTERN = re.compile(r"\bselect:(\d+(?:\.\d+)?)")
REQUEST_PATTERN = re.compile(r"\brequest:(\d+(?:\.\d+)?)")
TRACE_PATTERN = re.compile(r"(?:^|\s)trace:(.*?)(?: \(suppressed \d+\))?$")
SUPPRESSED_PATTERN = re.compile(r"\(suppressed (\d+)\)")
SESSION_ID_PATTERN = re.compile(r"([?&]session_id=)[^&\s]+", re.IGNORECASE)
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    r"\s+-\s+\[(?P<level>[A-Z]+)\]\s+(?P<message>.*)$"
)


def _match(pattern, message, convert=str):
    match = pattern.search(message)
    return convert(match.group(1)) if match else None


def analyze_records(records: list[dict]) -> dict:
    groups = {}
    unmatched = []
    suppressed_reports = 0
    unscoped_records = 0
    sessions_by_tid = {}

    for record in records:
        session = record.get("session") or None
        if session is None:
            unscoped_records += 1
        else:
            session = str(session)

        message = str(record.get("message", ""))
        tid = _match(TID_PATTERN, message)
        if tid is None:
            unmatched_record = dict(record)
            unmatched_record["message"] = SESSION_ID_PATTERN.sub(r"\1[redacted]", message)
            unmatched.append(unmatched_record)
            continue

        sessions_by_tid.setdefault(tid, set()).add(session)
        trace = _match(TRACE_PATTERN, message)
        states = []
        state_trace = re.sub(r",:\d+$", "", trace or "")
        for entry in re.split(r",(?=\d+:)", state_trace):
            state_match = re.match(r"^\d+:(.*)$", entry)
            if state_match:
                states.append(state_match.group(1))
        suppressed = _match(SUPPRESSED_PATTERN, message, int) or 0
        suppressed_reports += suppressed
        groups.setdefault(tid, []).append({
            "timestamp": record.get("timestamp"),
            "module": record.get("module"),
            "session": session,
            "front": _match(FRONT_PATTERN, message),
            "status": _match(STATUS_PATTERN, message, int),
            "select": _match(SELECT_PATTERN, message, float),
            "request": _match(REQUEST_PATTERN, message, float),
            "trace": trace,
            "last_state": states[-1] if states else None,
            "suppressed": suppressed,
        })

    return {
        "visible_records": len(records),
        "unique_tids": sorted(groups, key=int),
        "suppressed_reports": suppressed_reports,
        "unscoped_records": unscoped_records,
        "ambiguous_tids": sorted(
            (tid for tid, sessions in sessions_by_tid.items() if None in sessions or len(sessions) > 1),
            key=int,
        ),
        "unmatched": unmatched,
        "tids": groups,
    }


def main():
    parser = argparse.ArgumentParser(description="按 tid 关联可见的 X-Tunnel front 诊断日志")
    parser.add_argument("logs", nargs="+", type=Path, help="显式日志文件路径")
    parser.add_argument("--output", required=True, type=Path, help="输出 JSON 路径")
    parser.add_argument("--session", required=True, help="这些日志所属的单一进程/会话标识")
    args = parser.parse_args()

    records = []
    for path in args.logs:
        with path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                match = LOG_PATTERN.match(line.rstrip("\n"))
                if not match or match.group("level") != "WARNING":
                    continue
                records.append({
                    "timestamp": match.group("timestamp"),
                    "module": path.name.split(".", 1)[0],
                    "session": args.session,
                    "message": match.group("message"),
                })

    result = analyze_records(records)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
