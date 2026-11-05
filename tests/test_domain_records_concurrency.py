"""DomainRecords 缓存读写必须使用同一把锁保护集合迭代。"""
import collections
import importlib.util
from pathlib import Path
import sys
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


if __name__ == "__main__":
    unittest.main()
