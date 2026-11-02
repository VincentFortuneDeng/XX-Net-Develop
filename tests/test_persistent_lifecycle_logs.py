#!/usr/bin/env python3
"""O 阶段只允许八个既有生命周期 INFO 显式持久化。"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "Coding", "XX-Net-5.16.7.1", "code", "default")

EXPECTED = {
    "launcher/module_init.py": [
        "module %s started",
        "module %s stopped",
        "start %s time cost:%d ms",
    ],
    "launcher/start.py": [
        "Stopping all modules before exit!",
    ],
    "smart_router/local/__init__.py": [
        "Proxy server listen:%s:%d.",
    ],
    "x_tunnel/local/client.py": [
        "Socks5 server listen:%s:%d.",
        "Close Socks5 server ",
        "Stopping session",
    ],
}


class PersistentLifecycleLogTests(unittest.TestCase):
    def test_only_the_eight_lifecycle_info_calls_persist(self):
        found = {}
        for relative in EXPECTED:
            with open(os.path.join(CODE, relative), encoding="utf-8") as source:
                tree = ast.parse(source.read(), filename=relative)
            messages = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "info":
                    continue
                persisted = [kw for kw in node.keywords if kw.arg == "persist"]
                if not persisted:
                    continue
                self.assertEqual(len(persisted), 1)
                self.assertIsInstance(persisted[0].value, ast.Constant)
                self.assertIs(persisted[0].value.value, True)
                self.assertTrue(node.args)
                self.assertIsInstance(node.args[0], ast.Constant)
                messages.append(node.args[0].value)
            found[relative] = sorted(messages)

        self.assertEqual(found, {path: sorted(messages) for path, messages in EXPECTED.items()})
        self.assertEqual(sum(len(messages) for messages in found.values()), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
