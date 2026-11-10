"""macOS 无托盘退出、授权失败与重复退出的离线回归测试。"""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MAC_TRAY = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default" / "launcher" / "mac_tray.py"


class Collector:
    def _record(self, *args, **kwargs):
        pass

    debug = info = warn = warning = error = exception = _record


class NSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class ExitCalled(Exception):
    pass


def namespace(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def load_mac_tray():
    config = types.SimpleNamespace(control_port=8085, os_proxy_mode="pac")
    module_init = namespace("module_init", stop_all=mock.Mock())
    app_kit = namespace(
        "AppKit",
        NSObject=NSObject,
        CFNotificationCenterAddObserver=object(),
    )
    stubs = {
        "config": namespace(
            "config",
            config=config,
            app_name="XX-Net",
            get_language=lambda: "zh_CN",
        ),
        "simple_i18n": namespace("simple_i18n", SimpleI18N=types.SimpleNamespace(po_loader=lambda path: {})),
        "module_init": module_init,
        "xlog": namespace("xlog", getLogger=lambda *args, **kwargs: Collector()),
        "objc": namespace("objc", callbackFor=lambda callback: lambda fn: fn),
        "AppKit": app_kit,
        "SystemConfiguration": namespace("SystemConfiguration"),
        "PyObjCTools": namespace("PyObjCTools", AppHelper=types.SimpleNamespace()),
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("mac_tray_quit_under_test", MAC_TRAY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, config, module_init


class MacTrayQuitTest(unittest.TestCase):
    def setUp(self):
        self.mac, self.config, self.module_init = load_mac_tray()
        self.mac.quit_started = False
        self.helper_calls = []
        self.admin_calls = []

        def check_call(args):
            if not all(isinstance(arg, str) for arg in args):
                raise TypeError("subprocess arguments must be strings")
            self.helper_calls.append(list(args))
            return 0

        self.check_call = mock.Mock(side_effect=check_call)
        self.admin_call = mock.Mock(side_effect=self._record_admin)
        self.exit = mock.Mock(side_effect=ExitCalled())

    def _record_admin(self, args):
        self.admin_calls.append(list(args))
        return 1

    def run_quit(self, services=b"Wi-Fi\n", proxy_state="pac"):
        with mock.patch.object(self.mac.subprocess, "check_output", return_value=services), \
                mock.patch.object(self.mac.subprocess, "check_call", self.check_call), \
                mock.patch.object(self.mac.subprocess, "call", self.admin_call), \
                mock.patch.object(self.mac, "getProxyState", return_value=proxy_state), \
                mock.patch.object(self.mac.os, "_exit", self.exit):
            self.mac.sys_tray.windowWillClose_(None)

    def test_headless_quit_uses_detected_service_without_admin_or_ui(self):
        with self.assertRaises(ExitCalled):
            self.run_quit()

        self.assertEqual([], self.admin_calls)
        self.assertTrue(self.helper_calls)
        self.assertTrue(all(call[2] == "Wi-Fi" for call in self.helper_calls))
        self.module_init.stop_all.assert_called_once_with()
        self.exit.assert_called_once_with(0)

    def test_duplicate_quit_does_not_repeat_proxy_cleanup(self):
        with self.assertRaises(ExitCalled):
            self.run_quit()
        self.run_quit()

        self.assertEqual(3, len(self.helper_calls))
        self.assertEqual([], self.admin_calls)
        self.module_init.stop_all.assert_called_once_with()
        self.exit.assert_called_once_with(0)

    def test_concurrent_quit_during_authorization_does_not_prompt_again(self):
        admin_entered = threading.Event()
        release_admin = threading.Event()
        first_errors = []

        def blocking_admin(args):
            self._record_admin(args)
            admin_entered.set()
            if not release_admin.wait(3):
                raise TimeoutError("test authorization gate timed out")
            return 1

        self.check_call.side_effect = OSError("helper unavailable")
        self.admin_call.side_effect = blocking_admin

        def first_quit():
            try:
                self.mac.sys_tray.windowWillClose_(None)
            except ExitCalled:
                return
            except Exception as error:
                first_errors.append(error)

        with mock.patch.object(self.mac.subprocess, "check_output", return_value=b"Wi-Fi\n"), \
                mock.patch.object(self.mac.subprocess, "check_call", self.check_call), \
                mock.patch.object(self.mac.subprocess, "call", self.admin_call), \
                mock.patch.object(self.mac, "getProxyState", return_value="pac"), \
                mock.patch.object(self.mac.os, "_exit", self.exit):
            first = threading.Thread(target=first_quit)
            first.start()
            self.assertTrue(admin_entered.wait(1))
            self.mac.sys_tray.windowWillClose_(None)
            self.assertEqual(1, len(self.admin_calls))
            self.module_init.stop_all.assert_not_called()
            release_admin.set()
            first.join(3)

        self.assertFalse(first.is_alive())
        self.assertEqual([], first_errors)
        self.assertEqual(1, len(self.admin_calls))
        self.module_init.stop_all.assert_called_once_with()
        self.exit.assert_called_once_with(0)

    def test_admin_refusal_still_stops_headless_process(self):
        self.check_call.side_effect = OSError("helper unavailable")
        with self.assertRaises(ExitCalled):
            self.run_quit()

        self.assertEqual(1, len(self.admin_calls))
        self.module_init.stop_all.assert_called_once_with()
        self.exit.assert_called_once_with(0)

    def test_normal_menu_disable_still_saves_and_updates_ui(self):
        self.mac.currentService = "Wi-Fi"
        self.mac.sys_tray.updateStatusBarMenu = mock.Mock()
        self.config.save = mock.Mock()
        with mock.patch.object(self.mac.subprocess, "check_call", self.check_call), \
                mock.patch.object(self.mac.subprocess, "call", self.admin_call):
            self.mac.sys_tray.disableProxy_(False)

        self.assertEqual("disable", self.config.os_proxy_mode)
        self.config.save.assert_called_once_with()
        self.mac.sys_tray.updateStatusBarMenu.assert_called_once_with()
        self.assertEqual([], self.admin_calls)


if __name__ == "__main__":
    unittest.main()
