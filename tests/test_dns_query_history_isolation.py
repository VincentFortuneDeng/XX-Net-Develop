import importlib.util
from pathlib import Path
import re
import sys
import threading
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
sys.path.insert(0, str(APP / "lib" / "noarch"))


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


namespace("smart_router", APP / "smart_router")
namespace("smart_router.local", APP / "smart_router" / "local")
global_var = namespace("smart_router.local.global_var")

SPEC = importlib.util.spec_from_file_location(
    "smart_router.local.dns_query",
    APP / "smart_router" / "local" / "dns_query.py",
)
dns_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dns_module
SPEC.loader.exec_module(dns_module)


class Cache:
    def get_ips(self, domain, dns_type):
        return []

    def set_ips(self, domain, ips, dns_type):
        pass


class Rules:
    def check_host(self, domain, port):
        return None


class GfwList:
    def in_white_list(self, domain):
        return False

    def in_block_list(self, domain):
        return False


class LocalResolver:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def query(self, domain, dns_type=1, timeout=3):
        self.calls.append((domain, dns_type))
        return list(self.answers.get((domain, dns_type), []))


class LateParallelResultTest(unittest.TestCase):
    def test_timed_out_generation_cannot_overwrite_newer_query_cache(self):
        class RecordingCache:
            def __init__(self):
                self.writes = []

            def set_ips(self, domain, ips, dns_type):
                self.writes.append((domain, list(ips), dns_type))

        cache = RecordingCache()
        global_var.domain_cache = cache
        parallel = dns_module.ParallelQuery()
        started = threading.Event()
        release = threading.Event()

        def slow(domain, dns_type):
            started.set()
            release.wait(1)
            return [b"192.0.2.1"]

        first = threading.Thread(target=parallel.query, args=(b"late.example", 1, [slow]))
        first.start()
        self.assertTrue(started.wait(1))
        second = parallel.query(b"late.example", 1, [lambda domain, dns_type: [b"192.0.2.2"]])
        release.set()
        first.join(1)

        self.assertEqual(second, [b"192.0.2.2"])
        self.assertEqual(cache.writes, [(b"late.example", [b"192.0.2.2"], 1)])
        self.assertEqual(parallel.generations, {})


class DnsQueryHistoryIsolationTest(unittest.TestCase):
    def make_query(self, answers):
        query = dns_module.CombineDnsQuery.__new__(dns_module.CombineDnsQuery)
        query.domain_allowed_pattern = re.compile(br"(?!-)[A-Z\d-]{1,63}(?<!-)$")
        query.local_dns_resolve = LocalResolver(answers)
        global_var.domain_cache = Cache()
        global_var.user_rules = Rules()
        global_var.gfwlist = GfwList()
        global_var.config = types.SimpleNamespace(pac_policy="normal")
        return query

    def test_repeated_top_level_alias_query_resolves_again(self):
        query = self.make_query({
            (b"start.example", 1): [b"alias.example"],
            (b"alias.example", 1): [b"192.0.2.10"],
        })

        first = query.query(b"start.example")
        second = query.query(b"start.example")

        self.assertEqual(first, [b"192.0.2.10"])
        self.assertEqual(second, [b"192.0.2.10"])
        self.assertEqual(query.local_dns_resolve.calls.count((b"alias.example", 1)), 2)

    def test_alias_cycle_is_still_cut_within_one_call(self):
        query = self.make_query({
            (b"first.example", 1): [b"second.example"],
            (b"second.example", 1): [b"first.example"],
        })

        self.assertEqual(query.query(b"first.example"), [])
        self.assertEqual(query.local_dns_resolve.calls, [
            (b"first.example", 1),
            (b"second.example", 1),
            (b"first.example", 1),
        ])

    def test_different_domains_can_share_an_alias(self):
        query = self.make_query({
            (b"one.example", 1): [b"shared.example"],
            (b"two.example", 1): [b"shared.example"],
            (b"shared.example", 1): [b"192.0.2.20"],
        })

        self.assertEqual(query.query(b"one.example"), [b"192.0.2.20"])
        self.assertEqual(query.query(b"two.example"), [b"192.0.2.20"])

    def test_dns_types_have_independent_histories(self):
        query = self.make_query({
            (b"start.example", 1): [b"alias.example"],
            (b"alias.example", 1): [b"192.0.2.30"],
            (b"start.example", 28): [b"alias.example"],
            (b"alias.example", 28): [b"2001:db8::30"],
        })

        self.assertEqual(query.query(b"start.example", 1), [b"192.0.2.30"])
        self.assertEqual(query.query(b"start.example", 28), [b"2001:db8::30"])


if __name__ == "__main__":
    unittest.main()
