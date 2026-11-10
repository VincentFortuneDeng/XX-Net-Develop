"""配置原子写入与 Smart Router 停止保存顺序测试，全程使用临时文件。"""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
NOARCH = APP / "lib" / "noarch"
if str(NOARCH) not in sys.path:
    sys.path.insert(0, str(NOARCH))

spec = importlib.util.spec_from_file_location("xconfig", NOARCH / "xconfig.py")
xconfig = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = xconfig
spec.loader.exec_module(xconfig)


def load_config(path):
    config = xconfig.Config(str(path))
    config.set_var("value", 0)
    config.load()
    return config


class ConfigConcurrencyTest(unittest.TestCase):
    def test_reader_sees_old_or_new_complete_config_during_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"value": 1}', encoding="utf-8")
            saver = load_config(path)
            self.assertEqual(saver.value, 1)
            saver.value = 2

            write_started = threading.Event()
            allow_write = threading.Event()
            real_open = open

            class GatedFile:
                def __init__(self, file_object):
                    self.file_object = file_object

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.file_object.close()

                def write(self, content):
                    write_started.set()
                    if not allow_write.wait(2):
                        raise TimeoutError("测试未能放行配置写入")
                    return self.file_object.write(content)

                def flush(self):
                    return self.file_object.flush()

                def fileno(self):
                    return self.file_object.fileno()

            def gated_open(file, mode="r", *args, **kwargs):
                file_object = real_open(file, mode, *args, **kwargs)
                if "w" in mode:
                    return GatedFile(file_object)
                return file_object

            errors = []
            def save():
                try:
                    saver.save()
                except Exception as caught:
                    errors.append(caught)

            with mock.patch("builtins.open", side_effect=gated_open):
                thread = threading.Thread(target=save, name="config-saver")
                thread.start()
                self.assertTrue(write_started.wait(2))
                reader = load_config(path)
                observed_value = reader.value
                allow_write.set()
                thread.join(3)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observed_value, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 2})
            self.assertEqual([item.name for item in path.parent.iterdir()], ["config.json"])

    def test_missing_file_uses_defaults_and_nondefault_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = load_config(path)
            self.assertEqual(config.value, 0)

            config.value = 7
            config.save()
            self.assertEqual(load_config(path).value, 7)

    def test_smart_router_saves_final_cache_after_producers_stop(self):
        init_path = APP / "smart_router" / "local" / "__init__.py"
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
        stop_node = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "stop")
        events = []

        class Recorder:
            def __init__(self, name):
                self.name = name

            def save(self, force=False):
                self.assert_force(force)
                events.append(self.name + ".save")

            def stop(self):
                events.append(self.name + ".stop")

            def shutdown(self):
                events.append(self.name + ".shutdown")

            @staticmethod
            def assert_force(force):
                if force is not True:
                    raise AssertionError("最终缓存保存必须 force=True")

        namespace = {
            "ready": True,
            "g": types.SimpleNamespace(
                domain_cache=Recorder("domain"),
                ip_cache=Recorder("ip"),
                connect_manager=Recorder("connect"),
                pipe_socks=Recorder("pipe"),
                dns_query=Recorder("dns"),
                dns_srv=Recorder("dns_srv"),
                proxy_server=Recorder("proxy")),
        }
        module = ast.Module(body=[stop_node], type_ignores=[])
        exec(compile(module, str(init_path), "exec"), namespace)

        namespace["stop"]()

        self.assertEqual(events[-2:], ["domain.save", "ip.save"])
        self.assertEqual(namespace["ready"], False)
        self.assertEqual(len(events), 7)

    def test_smart_router_partial_startup_stop_skips_uninitialized_resources(self):
        init_path = APP / "smart_router" / "local" / "__init__.py"
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
        stop_node = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "stop")
        events = []

        class Cache:
            def __init__(self, name):
                self.name = name

            def save(self, force=False):
                events.append((self.name, force))

        namespace = {
            "ready": True,
            "g": types.SimpleNamespace(
                domain_cache=Cache("domain"), ip_cache=Cache("ip"),
                connect_manager=None, pipe_socks=None, dns_query=None,
                dns_srv=None, proxy_server=None),
        }
        module = ast.Module(body=[stop_node], type_ignores=[])
        exec(compile(module, str(init_path), "exec"), namespace)

        namespace["stop"]()

        self.assertEqual(events, [("domain", True), ("ip", True)])
        self.assertFalse(namespace["ready"])


if __name__ == "__main__":
    unittest.main()
