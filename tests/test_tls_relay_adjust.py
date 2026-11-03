import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
LIB = APP / "lib" / "noarch"
TLS_RELAY = APP / "x_tunnel" / "local" / "tls_relay_front"
sys.path.insert(0, str(LIB))


class Collector:
    def __init__(self):
        self.events = []

    def _add(self, level, fmt, *args):
        self.events.append((level, fmt % args))

    def debug(self, *args):
        self._add("DEBUG", *args)

    def info(self, *args):
        self._add("INFO", *args)

    def error(self, *args):
        self._add("ERROR", *args)

    def set_buffer(self, size):
        pass


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


namespace("x_tunnel", APP / "x_tunnel")
namespace("x_tunnel.local", APP / "x_tunnel" / "local")
namespace("x_tunnel.local.tls_relay_front", TLS_RELAY)
ip_manager_module = load_file("x_tunnel.local.tls_relay_front.ip_manager", TLS_RELAY / "ip_manager.py")
config_module = load_file("x_tunnel.local.tls_relay_front.config", TLS_RELAY / "config.py")

logger = Collector()
namespace("xlog", getLogger=lambda *args, **kwargs: logger)
namespace("env_info", data_path=tempfile.gettempdir())
namespace("front_base.openssl_wrap", SSLContext=object)
namespace("front_base.http_dispatcher", HttpsDispatcher=object)
namespace("front_base.connect_manager", ConnectManager=object)
namespace("gae_proxy", APP / "gae_proxy")
namespace("gae_proxy.local", APP / "gae_proxy" / "local")
namespace("gae_proxy.local.check_local_network")
namespace("x_tunnel.local.tls_relay_front.host_manager", HostManager=object)
namespace("x_tunnel.local.tls_relay_front.connect_creator", ConnectCreator=object)
namespace("x_tunnel.local.tls_relay_front.http2_stream", Stream=object)
front_module = load_file("x_tunnel.local.tls_relay_front.front", TLS_RELAY / "front.py")


def host(adjust=None, port=443):
    result = {"sni": None, "url_path": "/data", "port": port}
    if adjust is not None:
        result["adjust"] = adjust
    return result


def make_manager(hosts, speeds, use_ipv6="force_ipv4"):
    config = types.SimpleNamespace(
        ip_initial_score=1,
        ip_initial_rtt=1000,
        ip_initial_speed=1000,
        ip_cal_expect_time_package_size=1000,
        max_links_per_ip=1,
        use_ipv6=use_ipv6,
    )
    manager = ip_manager_module.IpManager(
        config,
        types.SimpleNamespace(info=hosts),
        Collector(),
        None,
    )
    manager.get_speed = lambda ip_str: speeds[ip_str]
    return manager


class TlsRelayAdjustTest(unittest.TestCase):
    def test_worker_score_threshold_uses_upstream_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = config_module.Config(str(Path(temp_dir) / "config.json"))

        self.assertEqual(config.dispather_work_max_score, 2)

    def test_front_propagates_adjust_to_ipv4_and_ipv6_hosts(self):
        front = front_module.Front.__new__(front_module.Front)
        front.config = types.SimpleNamespace(allow_set_ips=1)
        front.host_manager = mock.Mock()
        front.http_dispatcher = mock.Mock()
        front.logger = Collector()
        ips = {
            "192.0.2.10": {
                "sni": None,
                "url_path": "/data",
                "port": 443,
                "ipv6": "2001:db8::10",
                "adjust": 12.5,
            }
        }

        front.set_ips(ips)

        host_info = front.host_manager.set_host.call_args.args[0]
        self.assertEqual(host_info["192.0.2.10"].get("adjust"), 12.5)
        self.assertEqual(host_info["2001:db8::10"].get("adjust"), 12.5)

    def test_lowest_total_cost_wins_and_updates_link_state(self):
        hosts = {
            "192.0.2.1": host(adjust=200),
            "192.0.2.2": host(adjust=5),
        }
        manager = make_manager(
            hosts,
            {"192.0.2.1:443": (1000, 10), "192.0.2.2:443": (100, 20)},
        )

        with mock.patch.object(ip_manager_module.time, "time", return_value=1000.0):
            selected = manager.get_ip_sni_host()
            second = manager.get_ip_sni_host()
            third = manager.get_ip_sni_host()

        self.assertEqual(selected["ip_str"], "192.0.2.2:443")
        self.assertEqual(selected["adjust"], 5)
        self.assertEqual(second["ip_str"], "192.0.2.1:443")
        self.assertIsNone(third)
        self.assertEqual(manager.ip_dict["192.0.2.2"]["links"], 1)
        self.assertEqual(manager.ip_dict["192.0.2.2"]["last_try"], 1000.0)

    def test_missing_adjust_defaults_to_zero(self):
        manager = make_manager(
            {"192.0.2.3": host()},
            {"192.0.2.3:443": (100, 10)},
        )

        selected = manager.get_ip_sni_host()

        self.assertEqual(selected.get("adjust"), 0)

    def test_zero_speed_and_non_finite_or_invalid_adjust_are_skipped(self):
        hosts = {
            "192.0.2.1": host(adjust=0),
            "192.0.2.2": host(adjust="not-a-number"),
            "192.0.2.3": host(adjust="1e1000"),
            "192.0.2.4": host(),
        }
        manager = make_manager(
            hosts,
            {
                "192.0.2.1:443": (0, 1),
                "192.0.2.2:443": (1000, 1),
                "192.0.2.3:443": (900, 1),
                "192.0.2.4:443": (10, 5),
            },
        )

        selected = manager.get_ip_sni_host()

        self.assertEqual(selected["ip_str"], "192.0.2.4:443")
        debug_messages = [event[1] for event in manager.logger.events if event[0] == "DEBUG"]
        self.assertEqual(len(debug_messages), 3)

    def test_negative_adjust_participates_in_score(self):
        hosts = {
            "192.0.2.2": host(adjust=0),
            "192.0.2.1": host(adjust=-50),
        }
        manager = make_manager(
            hosts,
            {"192.0.2.1:443": (100, 10), "192.0.2.2:443": (100, 10)},
        )

        selected = manager.get_ip_sni_host()

        self.assertEqual(selected["ip_str"], "192.0.2.1:443")
        self.assertEqual(selected["adjust"], -50)

    def test_ipv6_filter_and_throttled_log_are_preserved(self):
        manager = make_manager(
            {"2001:db8::1": host(adjust=1), "192.0.2.1": host()},
            {"[2001:db8::1]:443": (100, 10), "192.0.2.1:443": (100, 10)},
        )

        with mock.patch.object(ip_manager_module.time, "time", return_value=1000.0):
            selected = manager.get_ip_sni_host()

        self.assertEqual(selected["ip_str"], "192.0.2.1:443")
        self.assertIn(
            ("INFO", "get_ip_sni_host skipped 1 IPv6 fronts (no usable local IPv6)"),
            manager.logger.events,
        )

    def test_ipv6_enabled_returns_selected_adjust(self):
        manager = make_manager(
            {"2001:db8::1": host(adjust=3)},
            {"[2001:db8::1]:443": (100, 10)},
            use_ipv6="force_ipv6",
        )

        selected = manager.get_ip_sni_host()

        self.assertEqual(selected["ip_str"], "[2001:db8::1]:443")
        self.assertEqual(selected.get("adjust"), 3)


if __name__ == "__main__":
    unittest.main()
