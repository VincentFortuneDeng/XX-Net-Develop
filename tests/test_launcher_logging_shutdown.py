import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default" / "launcher" / "start.py"


class LauncherLoggingShutdownTest(unittest.TestCase):
    def test_normal_and_forced_exit_stop_async_loggers(self):
        tree = ast.parse(START.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

        for name in ("exit_handler", "uncaught_exception_handler"):
            calls = [node for node in ast.walk(functions[name])
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "stop" and isinstance(node.func.value, ast.Name)
                     and node.func.value.id == "xlog_module"]
            self.assertTrue(calls, "%s must drain asynchronous loggers" % name)


if __name__ == "__main__":
    unittest.main()
