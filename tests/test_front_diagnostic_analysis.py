import importlib.util
from pathlib import Path
import unittest


ANALYZER_PATH = Path(__file__).resolve().with_name("analyze_xtunnel_front_diagnostics.py")
SPEC = importlib.util.spec_from_file_location("analyze_xtunnel_front_diagnostics", ANALYZER_PATH)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


class FrontDiagnosticAnalysisTest(unittest.TestCase):
    def test_correlates_visible_records_without_inventing_attempts(self):
        records = [
            {
                "timestamp": "2026-11-05 14:38:46.001",
                "module": "seley_front",
                "message": "task b'POST ny3.xx-net.org/'b'/data?tid=634':1 timeout "
                           "trace:100:start_request,1:h1_req_sent,20:get_response,:0",
            },
            {
                "timestamp": "2026-11-05 14:38:46.101",
                "module": "x_tunnel",
                "message": "front retry ny3.xx-net.org/data?tid=634 front:seley_front "
                           "status:602 select:0.010000 request:30.000000 (suppressed 2)",
            },
            {
                "timestamp": "2026-11-05 14:39:01.001",
                "module": "x_tunnel",
                "message": "front retry ny3.xx-net.org/data?tid=634 front:cloudflare_front "
                           "status:602 select:0.125000 request:30.500000",
            },
            {
                "timestamp": "2026-11-05 14:39:02.001",
                "module": "x_tunnel",
                "message": "front retry ny3.xx-net.org/data?tid=634 front:tls_relay_front "
                           "status:603 select:0.050000 request:1.250000",
            },
            {
                "timestamp": "2026-11-05 14:40:00.001",
                "module": "x_tunnel",
                "message": "front retry ny3.xx-net.org/data?tid=635 front:cloudflare_front "
                           "status:503 select:0.020000 request:2.000000",
            },
            {
                "timestamp": "2026-11-05 14:40:01.001",
                "module": "x_tunnel",
                "message": "get_front fail",
            },
        ]

        for record in records:
            record["session"] = "pid-100"

        result = analyzer.analyze_records(records)

        self.assertEqual(result["visible_records"], 6)
        self.assertEqual(result["unique_tids"], ["634", "635"])
        self.assertEqual(result["suppressed_reports"], 2)
        self.assertEqual(result["ambiguous_tids"], [])
        self.assertEqual(result["unscoped_records"], 0)
        self.assertEqual(result["unmatched"], [records[-1]])
        self.assertNotIn("attempts", result)

        correlated = result["tids"]["634"]
        self.assertEqual(len(correlated), 4)
        self.assertEqual(
            [record["timestamp"] for record in correlated],
            [record["timestamp"] for record in records[:4]],
        )
        self.assertEqual(
            {record["front"] for record in correlated if record["front"]},
            {"seley_front", "cloudflare_front", "tls_relay_front"},
        )
        self.assertEqual(correlated[0]["last_state"], "get_response")
        self.assertIn("h1_req_sent", correlated[0]["trace"])
        self.assertEqual(correlated[1]["status"], 602)
        self.assertEqual(correlated[1]["select"], 0.01)
        self.assertEqual(correlated[1]["request"], 30.0)
        self.assertEqual(correlated[1]["suppressed"], 2)

    def test_trace_preserves_worker_spaces_commas_and_final_state(self):
        records = [{
            "timestamp": "2026-11-05 14:38:46.001",
            "module": "seley_front",
            "session": "pid-100",
            "message": "task b'POST ny3.xx-net.org/'b'/data?tid=640':1 timeout "
                       "trace:0:start_request,1:get_worker(2):192.0.2.1, score:100,"
                       "2:start send header,3:get_response (suppressed 4)",
        }]

        result = analyzer.analyze_records(records)

        correlated = result["tids"]["640"][0]
        self.assertIn("get_worker(2):192.0.2.1, score:100", correlated["trace"])
        self.assertIn("start send header", correlated["trace"])
        self.assertEqual(correlated["last_state"], "get_response")
        self.assertEqual(correlated["suppressed"], 4)
        self.assertEqual(result["suppressed_reports"], 4)

    def test_reused_tid_across_sessions_is_marked_ambiguous(self):
        records = [
            {
                "timestamp": "2026-11-05 14:00:00.000",
                "module": "x_tunnel",
                "session": "pid-100",
                "message": "front retry host/data?tid=1 front:seley_front "
                           "status:602 select:0.010000 request:30.000000",
            },
            {
                "timestamp": "2026-11-05 15:00:00.000",
                "module": "x_tunnel",
                "session": "pid-200",
                "message": "front retry host/data?tid=1 front:cloudflare_front "
                           "status:503 select:0.020000 request:1.000000",
            },
        ]

        result = analyzer.analyze_records(records)

        self.assertEqual(result["ambiguous_tids"], ["1"])
        self.assertEqual(
            [record["session"] for record in result["tids"]["1"]],
            ["pid-100", "pid-200"],
        )

    def test_missing_session_is_explicitly_unscoped(self):
        records = [{
            "timestamp": "2026-11-05 14:00:00.000",
            "module": "x_tunnel",
            "message": "front retry host/data?tid=1 front:seley_front "
                       "status:602 select:0.010000 request:30.000000",
        }]

        result = analyzer.analyze_records(records)

        self.assertEqual(result["unscoped_records"], 1)
        self.assertEqual(result["ambiguous_tids"], ["1"])


if __name__ == "__main__":
    unittest.main()
