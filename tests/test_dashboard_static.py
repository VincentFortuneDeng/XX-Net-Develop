import pathlib
import re
import unittest
from html.parser import HTMLParser


ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_UI = ROOT / "Coding/XX-Net-5.16.7.1/code/default/launcher/web_ui"
HTML = WEB_UI / "dashboard.html"
CSS = WEB_UI / "css/dashboard.css"
JS = WEB_UI / "js/dashboard.js"


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.resources = []
        self.inline_scripts = []
        self.tables = 0
        self.details = 0
        self.file_inputs = 0
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "script":
            if attrs.get("src"):
                self.resources.append(attrs["src"])
            else:
                self._in_script = True
                self.inline_scripts.append("")
        if tag == "link" and attrs.get("href"):
            self.resources.append(attrs["href"])
        for key in ("src", "href", "poster", "srcset"):
            if key in attrs and tag not in ("link", "script", "a"):
                self.resources.append(attrs[key])
        if tag == "table":
            self.tables += 1
        if tag == "details":
            self.details += 1
        if tag == "input" and attrs.get("type") == "file":
            self.file_inputs += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.inline_scripts[-1] += data


class DashboardStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8") if HTML.exists() else ""
        cls.css = CSS.read_text(encoding="utf-8") if CSS.exists() else ""
        cls.js = JS.read_text(encoding="utf-8") if JS.exists() else ""
        cls.parser = DocumentParser()
        cls.parser.feed(cls.html)

    def test_six_required_regions_exist(self):
        self.assertEqual({
            "overview", "user-journey", "cross-layer", "resources",
            "exceptions", "evidence",
        }, self.parser.ids & {
            "overview", "user-journey", "cross-layer", "resources",
            "exceptions", "evidence",
        })
        for region in ("overview", "user-journey", "cross-layer", "resources", "exceptions", "evidence"):
            self.assertRegex(self.html, r'<section[^>]+id="' + region + r'"[^>]+aria-labelledby=')

    def test_only_local_versioned_static_resources(self):
        self.assertEqual(["css/dashboard.css", "js/dashboard.js"], self.parser.resources)
        self.assertTrue(all(not script.strip() for script in self.parser.inline_scripts))
        self.assertNotRegex(self.html, r"(?:https?:)?//")
        self.assertIn('http-equiv="Content-Security-Policy"', self.html)
        self.assertIn("connect-src 'self'", self.html)
        self.assertIn("form-action 'none'", self.html)

    def test_charts_have_crosshair_tooltip_legend_and_tables(self):
        self.assertGreaterEqual(self.parser.tables, 3)
        self.assertGreaterEqual(self.parser.details, 3)
        for marker in ("chart-crosshair", "chart-tooltip", "chart-legend"):
            self.assertIn(marker, self.html)
            self.assertIn(marker, self.css)
        self.assertIn("renderLineChart", self.js)
        self.assertIn("stroke-width", self.js)
        self.assertNotRegex(self.js, r"dual[-_ ]?axis|secondY|y2Axis")

    def test_probe_import_is_local_file_only(self):
        self.assertEqual(1, self.parser.file_inputs)
        self.assertIn('id="probe-file"', self.html)
        self.assertIn('accept=".json,application/json"', self.html)
        self.assertIn("summarizeProbes", self.js)
        self.assertIn("current.generation", self.js)

    def test_no_dynamic_html_or_sensitive_payload_export(self):
        self.assertNotIn(".innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)
        for forbidden in ("Authorization", "Cookie", "session_id", "hostname", "host:", "remoteAddress"):
            self.assertNotIn(forbidden, self.js)
        self.assertNotRegex(self.js, r"JSON\.stringify\s*\([^)]*(?:snapshot|samples)")

    def test_runtime_api_is_read_only_and_same_origin(self):
        self.assertIn('fetch("/runtime_metrics"', self.js)
        self.assertNotRegex(self.js, r"fetch\([^\n]*(?:https?:|//)")
        self.assertNotRegex(self.js, r"method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)")
        self.assertNotIn("XMLHttpRequest", self.js)
        self.assertNotIn("WebSocket", self.js)
        self.assertNotIn("EventSource", self.js)

    def test_palette_and_dark_mode_are_explicit(self):
        for color in ("#2a78d6", "#eb6834", "#1baf7a", "#3987e5", "#d95926", "#199e70"):
            self.assertIn(color, self.css)
        self.assertIn("prefers-color-scheme: dark", self.css)
        self.assertNotIn("@import", self.css)
        self.assertNotRegex(self.css, r"url\s*\(\s*['\"]?https?:")

    def test_probe_byte_columns_disclose_per_attempt_minimum_semantics(self):
        self.assertIn("单次预期字节", self.html)
        self.assertIn("最小实收字节", self.html)
        self.assertNotIn(">预期字节<", self.html)
        self.assertNotIn(">实际字节<", self.html)

    def test_no_sample_unknown_and_generation_boundaries_are_visible(self):
        for text in ("NO_SAMPLE", "unknown", "进程代际", "证据边界", "实际观察窗口"):
            self.assertIn(text, self.html + self.js)
        self.assertIn("aria-live=", self.html)
        self.assertIn("aria-label", self.html)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_pipe_health_and_direct_fallback_are_not_ambiguous(self):
        for marker in (
                "smart_router.pipe_running", "PipeSocks 已停止",
                "direct_connect_failures_total",
                "不等于用户最终失败", "不包含 direct 建连回退"):
            self.assertIn(marker, self.html + self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
