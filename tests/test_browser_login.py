import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from playwright.async_api import Error as PlaywrightError

import browser_login
import main


class BrowserDataTests(unittest.TestCase):
    def test_video_id(self):
        self.assertEqual(browser_login.video_id("https://www.douyin.com/video/123"), "123")
        self.assertEqual(browser_login.video_id("https://www.douyin.com/note/123"), "123")
        self.assertEqual(
            browser_login.video_id("https://www.douyin.com/user/abc?modal_id=123&vid=456"),
            "123",
        )
        self.assertIsNone(browser_login.video_id("https://v.douyin.com/abc/"))
        self.assertIsNone(browser_login.video_id("https://example.com/video/123"))

    def test_only_matching_successful_video_is_selected(self):
        item = {"aweme_id": "123"}
        for payload in [
            {"status_code": 0, "aweme_detail": item},
            {"status_code": 0, "aweme_list": [{"aweme_id": "456"}, item]},
        ]:
            self.assertEqual(browser_login.select_video(payload, "123"), item)
        for payload in [
            None, [], {"status_code": 1, "aweme_detail": item},
            {"aweme_detail": {"aweme_id": "456"}}, {"aweme_list": "invalid"},
        ]:
            self.assertIsNone(browser_login.select_video(payload, "123"))

    def test_cli_forwards_browser_mode(self):
        with patch("main.run_browser_session", return_value=0) as run:
            self.assertEqual(main.main([
                "https://www.douyin.com/video/123", "--browser-login"
            ]), 0)
        run.assert_called_once_with(
            main.BASE_DIR / "Download" / "douyin", main.BASE_DIR / "douyin.example.yaml",
            "https://www.douyin.com/video/123",
        )

    def test_cli_starts_browser_before_prompting_for_links(self):
        with patch("main.run_browser_session", return_value=0) as run, patch(
            "builtins.input"
        ) as read:
            self.assertEqual(main.main(["--browser-login"]), 0)
        self.assertIsNone(run.call_args.args[2])
        read.assert_not_called()

    def test_session_subprocess_inherits_input_and_starts_without_url(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.yaml"
            config.write_text("douyin: {}", encoding="utf-8")
            with patch("main.importlib.util.find_spec", return_value=object()), patch(
                "main.subprocess.run", return_value=MagicMock(returncode=0)
            ) as run:
                self.assertEqual(main.run_browser_session(Path(folder), config), 0)
            self.assertIn("--browser-session", run.call_args.args[0])
            self.assertNotIn("--url", run.call_args.args[0])
            self.assertNotIn("stdin", run.call_args.kwargs)

    def test_subprocess_forwards_browser_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.yaml"
            config.write_text("douyin: {}", encoding="utf-8")
            with patch("main.importlib.util.find_spec", return_value=object()), patch(
                "main.subprocess.run", return_value=MagicMock(returncode=1)
            ) as run, self.assertLogs("main", level="ERROR"):
                main.download_video("https://www.douyin.com/video/123", root, config, True)
            self.assertIn("--browser-login", run.call_args.args[0])


class BrowserFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = {
            "url": "https://www.douyin.com/video/123",
            "cookie": "sessionid=private-value",
            "headers": {"uifid": "private-value", "signature": "private-signature"},
        }

    async def test_capture_uses_separate_profile_and_closes_browser(self):
        page = MagicMock()
        page.url = self.options["url"]
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(return_value="TestBrowser")
        context = MagicMock()
        context.pages = [page]
        context.cookies = AsyncMock(return_value=[{"name": "sessionid", "value": "private"}])
        context.close = AsyncMock()
        response = MagicMock()
        response.url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        response.headers = {"content-type": "application/json"}
        response.status = 200
        response.json = AsyncMock(return_value={"aweme_detail": {"aweme_id": "123"}})

        async def navigate(url, **kwargs):
            if url == self.options["url"]:
                page.on.call_args.args[1](response)

        page.goto = AsyncMock(side_effect=navigate)
        with patch("browser_login.async_playwright") as playwright, patch(
            "browser_login.PROFILE_DIR", self.root / "isolated"
        ):
            engine = playwright.return_value.__aenter__.return_value
            engine.chromium.launch_persistent_context = AsyncMock(return_value=context)
            item, user_agent = await browser_login.capture_video(self.options)
            self.assertEqual(item["aweme_id"], "123")
            self.assertEqual(user_agent, "TestBrowser")
            launch = engine.chromium.launch_persistent_context.call_args.kwargs
            self.assertEqual(launch["user_data_dir"], str(self.root / "isolated"))
            self.assertFalse(launch["headless"])
        context.close.assert_awaited_once()

    async def test_browser_launch_failure_is_actionable_without_details(self):
        with patch("browser_login.async_playwright") as playwright, patch(
            "browser_login.PROFILE_DIR", self.root / "isolated"
        ):
            engine = playwright.return_value.__aenter__.return_value
            engine.chromium.launch_persistent_context = AsyncMock(
                side_effect=PlaywrightError("private-path-or-token")
            )
            with self.assertRaises(browser_login.BrowserDownloadError) as raised:
                await browser_login.capture_video(self.options)
        self.assertIn("Edge", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    async def test_download_does_not_forward_account_credentials_to_cdn(self):
        item = {"aweme_id": "123", "aweme_type": 0}
        data = {"aweme_id": "123", "video_play_addr": ["https://example.com/media.mp4"]}
        with patch("browser_login.capture_video", new=AsyncMock(
            return_value=(item, "TestBrowser")
        )), patch("browser_login.PostDetailFilter") as normalize, patch(
            "browser_login.DouyinDownloader"
        ) as downloader:
            normalize.return_value._to_dict.return_value = data
            instance = downloader.return_value.__aenter__.return_value
            instance.create_download_tasks = AsyncMock(
                side_effect=lambda *args: (args[2] / "123_video.mp4").write_bytes(b"video")
            )
            await browser_login.download_with_browser(self.options, self.root)
        options = downloader.call_args.args[0]
        self.assertEqual(options["cookie"], "")
        self.assertEqual(options["headers"], {
            "User-Agent": "TestBrowser", "Referer": "https://www.douyin.com/"
        })
        directory = self.root / "123"
        self.assertEqual(options["path"], str(directory))
        self.assertFalse(options["folderize"])
        instance.create_download_tasks.assert_awaited_once_with(options, data, directory)

    async def test_unsupported_and_missing_media_report_errors(self):
        for item in [
            {"aweme_id": "123", "aweme_type": 999},
            {"aweme_id": "123", "aweme_type": 68},
            {"aweme_id": "123", "aweme_type": 68, "images": [
                {"url_list": ["https://cdn.example/image"]},
            ], "status": {"is_prohibited": True}},
            {"aweme_id": "123", "aweme_type": 0, "status": {"is_prohibited": True}},
        ]:
            with patch("browser_login.capture_video", new=AsyncMock(
                return_value=(item, "TestBrowser")
            )), self.assertRaises(browser_login.BrowserDownloadError):
                await browser_login.download_with_browser(self.options, self.root)
        with patch("browser_login.capture_video", new=AsyncMock(
            return_value=({"aweme_id": "123", "aweme_type": 0}, "TestBrowser")
        )), patch("browser_login.PostDetailFilter") as normalize:
            normalize.return_value._to_dict.return_value = {}
            with self.assertRaises(browser_login.BrowserDownloadError):
                await browser_login.download_with_browser(self.options, self.root)

    async def test_gallery_downloads_images_without_using_video_downloader(self):
        item = {"aweme_id": "123", "aweme_type": 68, "images": [
            {"url_list": ["https://cdn.example/first"]},
            {"url_list": ["https://cdn.example/second"]},
        ]}
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=b"\xff\xd8\xffimage")

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("browser_login.capture_video", new=AsyncMock(
            return_value=(item, "TestBrowser")
        )), patch("video_archive.httpx.AsyncClient", return_value=client), patch(
            "browser_login.DouyinDownloader"
        ) as downloader, patch("browser_login.PostDetailFilter") as normalize:
            saved = await browser_login.download_with_browser(self.options, self.root)
        self.assertEqual(saved, self.root / "123" / "123_archive.json")
        self.assertTrue(saved.is_file())
        self.assertEqual([request.url.path for request in requests], ["/first", "/second"])
        self.assertTrue((saved.parent / "123_images_0.jpeg").is_file())
        self.assertTrue((saved.parent / "123_images_1.jpeg").is_file())
        self.assertFalse(list(saved.parent.glob("*.mp4")))
        downloader.assert_not_called()
        normalize.assert_not_called()

    async def test_gallery_download_failure_keeps_partial_archive_and_raises(self):
        item = {"aweme_id": "123", "aweme_type": 68, "images": [
            {"url_list": ["https://cdn.example/blocked"]},
        ]}
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403)
        ))
        with patch("video_archive.httpx.AsyncClient", return_value=client), self.assertRaisesRegex(
            browser_login.BrowserDownloadError, "图片缺失或下载失败"
        ):
            await browser_login.download_captured_video(
                self.options, self.root, item, "TestBrowser",
            )
        self.assertTrue((self.root / "123" / "123_archive.json").is_file())
        self.assertTrue((self.root / "123" / "123_detail_raw.json").is_file())

    async def test_download_without_file_is_failure(self):
        with patch("browser_login.capture_video", new=AsyncMock(
            return_value=({"aweme_id": "123", "aweme_type": 0}, "TestBrowser")
        )), patch("browser_login.PostDetailFilter") as normalize, patch(
            "browser_login.DouyinDownloader"
        ) as downloader:
            normalize.return_value._to_dict.return_value = {"video_play_addr": ["https://example.com"]}
            downloader.return_value.__aenter__.return_value.create_download_tasks = AsyncMock()
            with self.assertRaisesRegex(browser_login.BrowserDownloadError, "非空 MP4"):
                await browser_login.download_with_browser(self.options, self.root)

    async def test_existing_video_refreshes_product_info_without_redownload(self):
        path = self.root / "123_video.mp4"
        path.write_bytes(b"existing")
        before = path.stat().st_mtime_ns
        directory = self.root / "123"
        moved_path = directory / path.name
        with patch("browser_login.capture_video", new=AsyncMock(return_value=(
            {"aweme_id": "123", "aweme_type": 0}, "TestBrowser"
        ))) as capture, patch("browser_login.DouyinDownloader") as downloader, self.assertLogs(
            "product_links", level="WARNING"
        ):
            self.assertEqual(await browser_login.download_with_browser(self.options, self.root), moved_path)
        capture.assert_awaited_once()
        downloader.assert_not_called()
        self.assertFalse(path.exists())
        self.assertEqual(moved_path.stat().st_mtime_ns, before)
        self.assertEqual(moved_path.read_bytes(), b"existing")
        self.assertTrue((directory / "123_products.json").is_file())
        self.assertTrue((directory / "123_products.txt").is_file())
        self.assertTrue((directory / "123_products_raw.json").is_file())
        self.assertTrue((directory / "123_detail_raw.json").is_file())
        self.assertTrue((directory / "123_metadata.json").is_file())
        self.assertTrue((directory / "123_archive.json").is_file())

    async def test_different_videos_use_separate_directories(self):
        with patch("browser_login.PostDetailFilter") as normalize, patch(
            "browser_login.DouyinDownloader"
        ) as downloader:
            instance = downloader.return_value.__aenter__.return_value
            instance.create_download_tasks = AsyncMock(side_effect=lambda options, data, directory: (
                directory / f"{data['aweme_id']}_video.mp4"
            ).write_bytes(b"video"))
            for target_id in ("123", "456"):
                normalize.return_value._to_dict.return_value = {
                    "aweme_id": target_id, "video_play_addr": ["https://cdn.example/video"],
                }
                saved = await browser_login.download_captured_video(
                    self.options, self.root, {"aweme_id": target_id, "aweme_type": 0}, "TestBrowser",
                )
                directory = self.root / target_id
                self.assertEqual(saved, directory / f"{target_id}_video.mp4")
                self.assertTrue((directory / f"{target_id}_archive.json").is_file())
                self.assertTrue(all(path.name.startswith(f"{target_id}_") for path in directory.iterdir()))
            self.assertEqual({path.name for path in self.root.iterdir()}, {"123", "456"})

    async def test_same_browser_is_reused_and_response_listeners_are_removed(self):
        page = MagicMock()
        page.url = self.options["url"]
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(return_value="TestBrowser")
        context = MagicMock()
        context.pages = [page]
        context.cookies = AsyncMock(return_value=[{"name": "sessionid", "value": "private"}])
        context.close = AsyncMock()

        async def navigate(url, **kwargs):
            if "/video/" in url:
                response = MagicMock()
                response.url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                response.status = 200
                response.headers = {"content-type": "application/json"}
                response.json = AsyncMock(return_value={
                    "aweme_detail": {"aweme_id": url.rsplit("/", 1)[1]}
                })
                page.on.call_args.args[1](response)

        page.goto = AsyncMock(side_effect=navigate)
        with patch("browser_login.async_playwright") as playwright, patch(
            "browser_login.PROFILE_DIR", self.root / "isolated"
        ):
            launch = playwright.return_value.__aenter__.return_value.chromium.launch_persistent_context
            launch.return_value = context
            async with browser_login.open_browser_session() as session:
                first, _ = await session.capture_video(self.options)
                second, _ = await session.capture_video({
                    **self.options, "url": "https://www.douyin.com/video/456"
                })
                self.assertEqual(first["aweme_id"], "123")
                self.assertEqual(second["aweme_id"], "456")
                self.assertEqual(session.last_capture_source, {
                    "host": "www.douyin.com",
                    "path": "/aweme/v1/web/aweme/detail/", "http_status": 200,
                })
                context.close.assert_not_awaited()
                self.assertEqual(page.remove_listener.call_count, 2)
            launch.assert_awaited_once()
        context.close.assert_awaited_once()

    async def test_session_continues_after_failure_and_exits_once(self):
        session = MagicMock()
        session.ensure_open.return_value = None
        with patch("browser_login.open_browser_session") as open_session, patch(
            "browser_login.read_next_link", new=AsyncMock(side_effect=[
                "not-a-url", self.options["url"], "https://www.douyin.com/video/456", "exit"
            ])
        ), patch("browser_login.download_with_browser", new=AsyncMock(side_effect=[
            browser_login.BrowserDownloadError("下载失败"), self.root / "456_video.mp4"
        ])) as download, self.assertLogs("browser_login", level="ERROR"):
            open_session.return_value.__aenter__.return_value = session
            await browser_login.run_browser_session({**self.options, "url": None}, self.root)
        self.assertEqual(download.await_count, 2)
        self.assertTrue(all(call.args[2] is session for call in download.await_args_list))
        open_session.return_value.__aexit__.assert_awaited_once()

    async def test_session_first_url_and_eof(self):
        with patch("browser_login.open_browser_session") as open_session, patch(
            "browser_login.read_next_link", new=AsyncMock(return_value=None)
        ), patch("browser_login.download_with_browser", new=AsyncMock()) as download:
            open_session.return_value.__aenter__.return_value = MagicMock()
            await browser_login.run_browser_session(self.options, self.root)
        download.assert_awaited_once()
        self.assertEqual(download.call_args.args[0]["url"], self.options["url"])
        open_session.return_value.__aexit__.assert_awaited_once()

    async def test_closed_browser_ends_session_without_waiting_for_input(self):
        session = MagicMock()
        session.ensure_open.side_effect = browser_login.BrowserClosedError("浏览器已关闭")
        with patch("browser_login.open_browser_session") as open_session, patch(
            "browser_login.read_next_link", new=AsyncMock()
        ) as read:
            open_session.return_value.__aenter__.return_value = session
            await browser_login.run_browser_session({**self.options, "url": None}, self.root)
        read.assert_not_awaited()
        open_session.return_value.__aexit__.assert_awaited_once()

    async def test_console_wait_detects_browser_closure(self):
        session = MagicMock()
        session.ensure_open.side_effect = [
            None, browser_login.BrowserClosedError("浏览器已关闭")
        ]
        with patch("browser_login.sys.stdin.isatty", return_value=False), patch(
            "browser_login.Thread"
        ), self.assertRaises(browser_login.BrowserClosedError):
            await browser_login.read_next_link(session)

    async def test_console_eof(self):
        session = MagicMock()
        with patch("browser_login.sys.stdin.isatty", return_value=False), patch(
            "builtins.input", side_effect=EOFError
        ):
            self.assertIsNone(await browser_login.read_next_link(session))

    async def test_interactive_input_uses_async_editor_without_input_thread(self):
        session = MagicMock()
        session.prompt = None
        url = "https://www.douyin.com/video/123"
        with patch("browser_login.sys.stdin.isatty", return_value=True), patch(
            "browser_login.PromptSession"
        ) as editor, patch("browser_login.Thread") as thread:
            editor.return_value.prompt_async = AsyncMock(return_value=url)
            self.assertEqual(await browser_login.read_next_link(session), url)
            self.assertEqual(await browser_login.read_next_link(session), url)
        editor.assert_called_once()
        thread.assert_not_called()
        self.assertEqual(editor.return_value.prompt_async.await_count, 2)

    async def test_interactive_eof(self):
        session = MagicMock()
        session.prompt = MagicMock()
        session.prompt.prompt_async = AsyncMock(side_effect=EOFError)
        with patch("browser_login.sys.stdin.isatty", return_value=True):
            self.assertIsNone(await browser_login.read_next_link(session))

    async def test_interactive_close_cancels_pending_prompt(self):
        import asyncio

        session = MagicMock()
        cancelled = asyncio.Event()

        async def wait_for_input(prompt):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session.prompt = MagicMock()
        session.prompt.prompt_async = wait_for_input
        session.ensure_open.side_effect = browser_login.BrowserClosedError("窗口已关闭")
        with patch("browser_login.sys.stdin.isatty", return_value=True):
            with self.assertRaises(browser_login.BrowserClosedError):
                await browser_login.read_next_link(session)
        self.assertTrue(cancelled.is_set())

    async def test_real_async_editor_accepts_pasted_url_and_next_link(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        session = MagicMock()
        with create_pipe_input() as pipe:
            session.prompt = PromptSession(input=pipe, output=DummyOutput())
            with patch("browser_login.sys.stdin.isatty", return_value=True):
                pipe.send_text("\x1b[200~https://www.douyin.com/video/123\x1b[201~\r")
                self.assertEqual(
                    await browser_login.read_next_link(session),
                    "https://www.douyin.com/video/123",
                )
                pipe.send_text("exit\r")
                self.assertEqual(await browser_login.read_next_link(session), "exit")

    async def test_cancellation_closes_session(self):
        import asyncio

        with patch("browser_login.open_browser_session") as open_session, patch(
            "browser_login.read_next_link", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            open_session.return_value.__aenter__.return_value = MagicMock()
            with self.assertRaises(asyncio.CancelledError):
                await browser_login.run_browser_session({**self.options, "url": None}, self.root)
        open_session.return_value.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
