import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from f2.apps.douyin.crawler import DouyinCrawler

import f2_download


class F2OptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.yaml"

    def build(self, values):
        self.config.write_text(yaml.safe_dump({"douyin": values}), encoding="utf-8")
        return f2_download.build_options(
            self.config, "https://www.douyin.com/video/123", self.root
        )

    def test_uifid_and_headers_reach_http_transport(self):
        options = self.build({
            "cookie": "sessionid=test-session",
            "uifid": "test-uifid",
            "headers": {"user-agent": "TestBrowser", "Referer": "https://www.douyin.com/"},
        })
        crawler = DouyinCrawler(options)
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(200, json={"status_code": 0})

        with httpx.Client(
            transport=httpx.MockTransport(handle), headers=crawler.headers
        ) as client:
            client.get("https://www.douyin.com/aweme/v1/web/aweme/detail/")
        self.assertEqual(seen[0].headers["uifid"], "test-uifid")
        self.assertEqual(seen[0].headers["user-agent"], "TestBrowser")
        self.assertEqual(seen[0].headers["cookie"], "sessionid=test-session")
        self.assertEqual(options["mode"], "one")
        self.assertFalse(options["music"])
        self.assertFalse(options["cover"])

    def test_uifid_header_is_preserved(self):
        options = self.build({"headers": {"UIFID": "header-id"}})
        self.assertEqual(options["headers"]["uifid"], "header-id")

    def test_explicit_uifid_takes_precedence(self):
        options = self.build({"uifid": "explicit-id", "headers": {"UIFID": "header-id"}})
        self.assertEqual(options["headers"]["uifid"], "explicit-id")

    def test_blank_uifid_is_optional(self):
        options = self.build({"uifid": "", "cookie": ""})
        self.assertNotIn("uifid", options["headers"])
        self.assertTrue(options["headers"]["User-Agent"])

    def test_session_without_first_url_does_not_use_configured_url(self):
        self.build({"url": "https://www.douyin.com/video/456"})
        options = f2_download.build_options(self.config, None, self.root)
        self.assertIsNone(options["url"])

    def test_invalid_fields_are_rejected_without_values(self):
        for fields in [
            {"headers": "invalid"},
            {"headers": {"Cookie": "sensitive-cookie"}},
            {"headers": {"User-Agent": "secret\r\nheader"}},
            {"uifid": 123},
            {"uifid": "secret\r\nheader"},
        ]:
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError) as raised:
                self.build(fields)
            self.assertNotIn("secret", str(raised.exception))

    def test_runner_passes_built_options_to_f2(self):
        self.build({"uifid": "test-id"})
        with patch("f2_download.run_douyin") as run, patch(
            "f2_download.SignalManager"
        ), patch("f2_download.RichConsoleManager") as console:
            self.assertEqual(f2_download.main([
                "--config", str(self.config), "--url", "https://www.douyin.com/video/123",
                "--output", str(self.root),
            ]), 0)
        options = run.call_args.args[0]
        self.assertEqual(options["headers"]["uifid"], "test-id")
        console.return_value.progress.__enter__.assert_called_once()

    def test_runner_starts_session_without_initial_url(self):
        self.build({"cookie": ""})
        with patch("browser_login.run_browser_session") as run, patch(
            "f2_download.RichConsoleManager"
        ) as console:
            self.assertEqual(f2_download.main([
                "--browser-session", "--config", str(self.config), "--output", str(self.root)
            ]), 0)
        run.assert_awaited_once()
        console.assert_not_called()
        self.assertIsNone(run.call_args.args[0].get("url"))

    def test_one_shot_browser_capture_does_not_start_live_progress(self):
        self.build({"cookie": ""})
        with patch("browser_login.download_with_browser") as run, patch(
            "f2_download.RichConsoleManager"
        ) as console:
            self.assertEqual(f2_download.main([
                "--browser-login", "--url", "https://www.douyin.com/video/123",
                "--config", str(self.config), "--output", str(self.root)
            ]), 0)
        run.assert_awaited_once()
        console.assert_not_called()


if __name__ == "__main__":
    unittest.main()
