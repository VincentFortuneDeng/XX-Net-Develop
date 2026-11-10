"""DomainRecords 缓存读写必须使用同一把锁保护集合迭代。"""
import collections
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
NOARCH = APP / "lib" / "noarch"
if str(NOARCH) not in sys.path:
    sys.path.insert(0, str(NOARCH))


for name, path in (
        ("smart_router", APP / "smart_router"),
        ("smart_router.local", APP / "smart_router" / "local")):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module

spec = importlib.util.spec_from_file_location(
    "smart_router.local.host_records", APP / "smart_router" / "local" / "host_records.py")
host_records = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = host_records
spec.loader.exec_module(host_records)


class GatedSet(set):
    """在真实 set 迭代器创建后暂停，写线程随后修改同一个集合。"""
    def __init__(self, values):
        super().__init__(values)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.iterator = None

    def __iter__(self):
        self.iterator = super().__iter__()
        return self

    def __next__(self):
        self.entered.set()
        self.release.wait(2)
        return next(self.iterator)

    def add(self, value):
        super().add(value)
        self.release.set()


class ReleasingLock:
    """修复后写线程等待读锁时，让受控读迭代继续完成。"""
    def __init__(self, values):
        self.lock = threading.RLock()
        self.values = values

    def __enter__(self):
        if threading.current_thread().name == "cache-writer":
            if not self.lock.acquire(False):
                self.values.release.set()
                self.lock.acquire()
        else:
            self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class SaveGateLock:
    """在保存快照释放锁后放行写线程；若脏位已锁内清除则不等待。"""
    def __init__(self, records, lock):
        self.records = records
        self.lock = lock
        self.released = threading.Event()
        self.writer_done = threading.Event()

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()
        if threading.current_thread().name == "save-thread":
            self.released.set()
            if self.records.need_save:
                self.writer_done.wait(2)


class DomainRecordsConcurrencyTest(unittest.TestCase):
    def test_set_ips_cannot_mutate_set_during_get_ips_iteration(self):
        records = host_records.DomainRecords.__new__(host_records.DomainRecords)
        records.file_path = "/unused"
        records.capacity = 100
        records.ttl = 3600
        records.last_save_time = time.time()
        records.last_update_time = 0
        records.need_save = False
        values = GatedSet([b"192.0.2.1"])
        records.lock = ReleasingLock(values)
        records.cache = collections.OrderedDict([
            (b"example.com", {"r": "unknown", "dns": {1: values}, "g": 1,
                              "query_count": 0, "update": time.time()})])
        errors = []

        def write():
            try:
                self.assertTrue(values.entered.wait(1))
                records.set_ips(b"example.com", [b"192.0.2.2"], 1)
            except Exception as caught:
                errors.append(caught)
            finally:
                values.release.set()

        writer = threading.Thread(target=write, name="cache-writer")
        writer.start()
        try:
            first_read = records.get_ips(b"example.com", 1)
        except Exception as caught:
            errors.append(caught)
        finally:
            values.release.set()
            writer.join(2)

        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(first_read, [b"192.0.2.1"])
        self.assertEqual(
            set(records.get_ips(b"example.com", 1)), {b"192.0.2.1", b"192.0.2.2"})

    def test_loaded_domain_uses_bytes_keys_and_complete_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "domain_records.txt"
            path.write_text("example.com gae 0\n", encoding="utf-8")
            records = host_records.DomainRecords(str(path))

            self.assertEqual(list(records.cache), [b"example.com"])
            record = records.cache[b"example.com"]
            self.assertEqual(
                set(record), {"r", "dns", "g", "query_count", "update"})

    def test_loaded_domain_rule_and_gae_deny_accept_str_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "domain_records.txt"
            path.write_text("example.com gae 0\n", encoding="utf-8")
            records = host_records.DomainRecords(str(path))

            for expected_count, domain in enumerate(("example.com", b"example.com"), 1):
                with self.subTest(domain=domain):
                    self.assertEqual(records.get_rule(domain), "gae")
                    self.assertEqual(records.accept_gae(domain), 0)
                    records.add_query_count(domain)
                    self.assertEqual(records.get_query_count(domain), expected_count)

    def test_domain_save_does_not_clear_dirty_for_a_concurrent_write(self):
        with tempfile.TemporaryDirectory() as directory:
            records = host_records.DomainRecords(str(Path(directory) / "domain_records.txt"))
            gate = SaveGateLock(records, records.lock)
            records.lock = gate
            records.need_save = True
            errors = []

            def write():
                try:
                    self.assertTrue(gate.released.wait(2))
                    records.set_ips(b"late.example", [b"192.0.2.2"], 1)
                except Exception as caught:
                    errors.append(caught)
                finally:
                    gate.writer_done.set()

            writer = threading.Thread(target=write, name="cache-writer")
            saver = threading.Thread(target=lambda: records.save(True), name="save-thread")
            writer.start()
            saver.start()
            saver.join(3)
            writer.join(3)

            self.assertFalse(saver.is_alive() or writer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(records.need_save)

    def test_ip_save_does_not_clear_dirty_for_a_concurrent_write(self):
        with tempfile.TemporaryDirectory() as directory:
            records = host_records.IpRecord(str(Path(directory) / "ip_records.txt"))
            gate = SaveGateLock(records, records.lock)
            records.lock = gate
            records.need_save = True
            errors = []

            def write():
                try:
                    self.assertTrue(gate.released.wait(2))
                    records.set("192.0.2.2", {"r": "direct", "c": 1, "update": 1})
                except Exception as caught:
                    errors.append(caught)
                finally:
                    gate.writer_done.set()

            writer = threading.Thread(target=write, name="cache-writer")
            saver = threading.Thread(target=lambda: records.save(True), name="save-thread")
            writer.start()
            saver.start()
            saver.join(3)
            writer.join(3)

            self.assertFalse(saver.is_alive() or writer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(records.need_save)


if __name__ == "__main__":
    unittest.main()
