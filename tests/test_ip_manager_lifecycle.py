"""IpManager 扫描线程的 reset/stop 代际与计数测试，不执行真实网络扫描。"""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

REAL_THREAD = threading.Thread


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
NOARCH = APP / "lib" / "noarch"
sys.path.insert(0, str(NOARCH))

SPEC = importlib.util.spec_from_file_location("front_base.ip_manager", NOARCH / "front_base" / "ip_manager.py")
ip_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ip_module
SPEC.loader.exec_module(ip_module)


class Collector:
    name = "ip-manager-test"
    debug = info = warning = warn = error = exception = lambda self, *args: None


class RecordedThread:
    instances = []

    def __init__(self, target=None, args=(), name=None, **kwargs):
        self.target = target
        self.args = args
        self.name = name
        self.start_calls = 0
        self.__class__.instances.append(self)

    def start(self):
        self.start_calls += 1


class IpManagerLifecycleTest(unittest.TestCase):
    def setUp(self):
        RecordedThread.instances = []
        self.manager = ip_module.IpManager.__new__(ip_module.IpManager)
        self.manager.logger = Collector()
        self.manager.config = types.SimpleNamespace(
            max_scan_ip_thread_num=2, scan_ip_interval=0)
        self.manager.ip_source = object()
        self.manager.ip_lock = threading.Lock()
        self.manager.scan_thread_lock = threading.Lock()
        self.manager._scan_lifecycle_lock = threading.RLock()
        self.manager._scan_generation = 0
        self.manager._stopped = False
        self.manager.load_config = self.load_config
        self.manager.load_ip = lambda: None
        self.thread_patch = mock.patch.object(ip_module.threading, "Thread", RecordedThread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def load_config(self):
        self.manager.scan_ip_thread_num = self.manager.config.max_scan_ip_thread_num

    def test_old_worker_exit_does_not_consume_new_generation_count(self):
        self.manager.reset()
        old_worker = RecordedThread.instances[0]
        self.manager.reset()
        new_workers = RecordedThread.instances[2:4]
        self.assertEqual(self.manager.scan_thread_count, 2)

        self.manager.running = False
        old_worker.target(*old_worker.args)

        self.assertEqual(
            self.manager.scan_thread_count, 2,
            "旧代 worker 退出不得扣减新代已登记线程")

        new_workers[0].target(*new_workers[0].args)
        self.assertEqual(self.manager.scan_thread_count, 1)

    def test_stop_during_reset_cannot_publish_late_scan_threads(self):
        self.manager.reset()
        initial_threads = len(RecordedThread.instances)
        entered = threading.Event()
        release = threading.Event()

        def gated_load_ip():
            entered.set()
            release.wait(2)

        self.manager.load_ip = gated_load_ip
        resetter = REAL_THREAD(target=self.manager.reset)
        resetter.start()
        self.assertTrue(entered.wait(1))
        self.manager.stop()
        release.set()
        resetter.join(2)

        self.assertFalse(resetter.is_alive())
        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.scan_thread_count, 0)
        self.assertEqual(len(RecordedThread.instances), initial_threads)

    def test_reset_after_stop_does_not_revive_scanning(self):
        self.manager.reset()
        self.manager.stop()
        initial_threads = len(RecordedThread.instances)

        self.manager.reset()

        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.scan_thread_count, 0)
        self.assertEqual(len(RecordedThread.instances), initial_threads)

    def test_old_check_result_cannot_commit_after_reset(self):
        manager = self.manager
        manager.running = True
        manager.scan_thread_count = 1
        manager.scan_ip_thread_num = 2
        manager.scan_fail_count = 0
        manager.ip_dict = {}
        manager.ip_list = []
        manager.good_ip_num = 0
        manager.max_good_ip_num = 100
        manager.scan_ip_log = None
        manager.ip_source = types.SimpleNamespace(get_ip=lambda: "192.0.2.1")
        manager.check_local_network = types.SimpleNamespace(
            is_ok=lambda *args: True, report_fail=mock.Mock())
        entered = threading.Event()
        release = threading.Event()
        committed = threading.Event()
        result = types.SimpleNamespace(
            ok=True, request_time=1, domain="example.com", h2=False)

        def gated_check(ip_str):
            entered.set()
            release.wait(2)
            return result

        manager.check_ip = gated_check
        manager.add_ip = mock.Mock(side_effect=lambda *args: committed.set() or True)
        manager.remove_slowest_ip = mock.Mock()
        manager.save = mock.Mock()
        worker = REAL_THREAD(target=manager.scan_ip_worker, args=(0,))
        worker.start()
        self.assertTrue(entered.wait(1))
        manager.reset()
        release.set()
        committed.wait(0.1)
        manager.stop()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        manager.add_ip.assert_not_called()
        manager.save.assert_not_called()
        self.assertEqual(manager.ip_dict, {})

    def test_recheck_persistent_failure_removes_after_threshold(self):
        manager = self.manager
        manager.running = True
        manager.ip_dict = {"192.0.2.1": {"fail_times": 3}}
        manager.check_local_network = types.SimpleNamespace(is_ok=lambda *args: True)
        manager.report_connect_fail = mock.Mock()
        manager.check_ip = mock.Mock(side_effect=[
            None, AssertionError("超过阈值后不应再次复查同一失败结果")])

        with mock.patch.object(ip_module.time, "sleep", return_value=None):
            manager.recheck_ip("192.0.2.1")

        manager.check_ip.assert_called_once_with("192.0.2.1")
        manager.report_connect_fail.assert_any_call("192.0.2.1", force_remove=True)

    def test_background_recheck_retries_before_removing_persistent_failure(self):
        manager = self.manager
        manager.running = True
        manager.ip_dict = {"192.0.2.1": {"fail_times": 3}}
        manager.check_local_network = types.SimpleNamespace(is_ok=lambda *args: True)
        manager.report_connect_fail = mock.Mock()
        manager.check_ip = mock.Mock(side_effect=[None, None])

        with mock.patch.object(ip_module.time, "sleep", return_value=None) as sleep:
            manager.recheck_ip("192.0.2.1", first_report=False)

        self.assertEqual(manager.check_ip.call_count, 2)
        sleep.assert_called_once_with(5)
        manager.report_connect_fail.assert_any_call("192.0.2.1", force_remove=True)

    def test_normal_reset_creates_current_generation_workers(self):
        self.manager.reset()

        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.scan_thread_count, 2)
        self.assertEqual(len(RecordedThread.instances), 2)
        self.assertTrue(all(thread.start_calls == 1 for thread in RecordedThread.instances))


if __name__ == "__main__":
    unittest.main()
