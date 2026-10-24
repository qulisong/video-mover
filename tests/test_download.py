import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class VideoDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_new_video_directory_is_reused(self):
        root = self.root / "custom-output"
        first = main.prepare_video_directory(root, "123")
        second = main.prepare_video_directory(root, "123")
        self.assertEqual(first, root / "123")
        self.assertEqual(first, second)
        self.assertFalse((first / "123").exists())

    def test_flat_files_move_without_content_or_timestamp_changes(self):
        names = ["123_video.mp4", "123_desc.txt", "123_products_raw.json", "123_video_cover.jpeg"]
        original = {}
        for name in names:
            path = self.root / name
            path.write_bytes(name.encode("ascii"))
            original[name] = path.read_bytes(), path.stat().st_mtime_ns
        unrelated = self.root / "1234_video.mp4"
        unrelated.write_bytes(b"other-video")
        (self.root / "notes.txt").write_bytes(b"notes")
        directory = main.prepare_video_directory(self.root, "123")
        for name in names:
            path = directory / name
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original[name])
            self.assertFalse((self.root / name).exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue((self.root / "notes.txt").exists())
        self.assertEqual(main.existing_video(self.root, "123"), directory / "123_video.mp4")

    def test_conflict_stops_before_moving_any_files(self):
        directory = self.root / "123"
        directory.mkdir()
        (self.root / "123_desc.txt").write_bytes(b"description")
        (self.root / "123_video.mp4").write_bytes(b"old")
        (directory / "123_video.mp4").write_bytes(b"new")
        with self.assertRaisesRegex(FileExistsError, "同名文件"):
            main.prepare_video_directory(self.root, "123")
        self.assertEqual((directory / "123_video.mp4").read_bytes(), b"new")
        self.assertEqual((self.root / "123_video.mp4").read_bytes(), b"old")
        self.assertTrue((self.root / "123_desc.txt").exists())
        self.assertFalse((directory / "123_desc.txt").exists())

    def test_existing_directory_accepts_missing_legacy_files(self):
        directory = self.root / "123"
        directory.mkdir()
        (directory / "123_video.mp4").write_bytes(b"video")
        (self.root / "123_products.json").write_bytes(b"{}")
        main.prepare_video_directory(self.root, "123")
        self.assertTrue((directory / "123_products.json").exists())
        self.assertEqual((directory / "123_video.mp4").read_bytes(), b"video")

    def test_invalid_id_does_not_create_paths(self):
        for target_id in ("", "../escape", "..\\escape", "123/456", "１２３"):
            with self.subTest(target_id=target_id), self.assertRaises(ValueError):
                main.prepare_video_directory(self.root, target_id)
        self.assertFalse(list(self.root.iterdir()))

    def test_file_at_directory_path_is_not_replaced(self):
        path = self.root / "123"
        path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            main.prepare_video_directory(self.root, "123")
        self.assertEqual(path.read_bytes(), b"keep")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.yaml"
        self.config.write_text("douyin: {}", encoding="utf-8")
        self.output = self.root / "downloads"
        self.url = "https://www.douyin.com/video/123456789"

    def test_standard_and_share_urls(self):
        for text, expected in [
            (self.url, self.url),
            ("https://www.douyin.com/note/7654976739009948603",
             "https://www.douyin.com/note/7654976739009948603"),
            ("分享 https://v.douyin.com/abc123/ 复制链接", "https://v.douyin.com/abc123/"),
            ("分享 https://v.douyin.com/abc123/。", "https://v.douyin.com/abc123/"),
            (
                "https://www.douyin.com/user/abc?modal_id=123456789&vid=987654321",
                self.url,
            ),
            (
                "https://www.douyin.com/jingxuan/search/example?modal_id=123456789&type=general",
                self.url,
            ),
        ]:
            with self.subTest(text=text):
                self.assertEqual(main.parse_video_url(text), expected)

    def test_rejects_invalid_or_profile_urls(self):
        for text in [
            "", "https://www.douyin.com/user/123", "https://douyin.com.evil.com/video/123",
            "https://www.douyin.com@evil.com/video/123", "http://v.douyin.com/abc/",
            "https://v.douyin.com:8080/abc/", "https://www.douyin.com/video/no-id",
            "https://www.douyin.com/user/abc?modal_id=invalid",
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                main.parse_video_url(text)

    def test_download_creates_directory_and_uses_current_python(self):
        def run(command, **kwargs):
            self.assertTrue(self.output.is_dir())
            self.assertEqual(command[0], main.sys.executable)
            self.assertEqual(command[3], str(main.BASE_DIR / "f2_download.py"))
            self.assertEqual(command[command.index("--config") + 1], str(self.config))
            self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
            self.assertEqual(kwargs["cwd"], main.BASE_DIR)
            (self.output / "123.mp4").write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0)

        with patch("main.importlib.util.find_spec", return_value=object()), patch(
            "main.subprocess.run", side_effect=run
        ):
            self.assertEqual(main.download_video(self.url, self.output, self.config), 0)

    def test_no_false_success_without_new_video(self):
        for existing in [None, b"", b"existing video"]:
            with self.subTest(existing=existing):
                self.output.mkdir(exist_ok=True)
                if existing is not None:
                    (self.output / "old.mp4").write_bytes(existing)
                with patch("main.importlib.util.find_spec", return_value=object()), patch(
                    "main.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
                ), self.assertLogs("main", level="ERROR"):
                    self.assertEqual(main.download_video(self.url, self.output, self.config), 1)

    def write_gallery(self, content=b"\xff\xd8\xffimage"):
        from video_archive import file_record

        directory = main.prepare_video_directory(self.output, "123456789")
        image = directory / "123456789_images_0.jpeg"
        image.write_bytes(content)
        manifest = directory / "123456789_archive.json"
        manifest.write_text(json.dumps({
            "video_id": "123456789", "media_type": "image_gallery", "image_count": 1,
            "assets": [{
                "source_field": "images.0", "kind": "image", "status": "downloaded",
                **file_record(image),
            }],
        }), encoding="utf-8")
        return image

    def test_direct_gallery_success_without_mp4_for_note_popup_and_short_links(self):
        for url in [
            "https://www.douyin.com/note/123456789", self.url,
            "https://v.douyin.com/example/",
        ]:
            with self.subTest(url=url):
                def run(*args, **kwargs):
                    self.write_gallery()
                    return subprocess.CompletedProcess([], 0)

                with patch("main.importlib.util.find_spec", return_value=object()), patch(
                    "main.subprocess.run", side_effect=run
                ):
                    self.assertEqual(main.download_video(url, self.output, self.config), 0)
                self.assertFalse(list(self.output.rglob("*.mp4")))

    def test_gallery_missing_or_corrupt_images_cannot_report_success(self):
        for failure in ("missing", "corrupt", "empty"):
            with self.subTest(failure=failure):
                def run(*args, **kwargs):
                    image = self.write_gallery()
                    if failure == "missing":
                        image.unlink()
                    else:
                        image.write_bytes(b"changed" if failure == "corrupt" else b"")
                    return subprocess.CompletedProcess([], 0)

                with patch("main.importlib.util.find_spec", return_value=object()), patch(
                    "main.subprocess.run", side_effect=run
                ), self.assertLogs("main", level="ERROR"):
                    self.assertEqual(main.download_video(
                        "https://www.douyin.com/note/123456789", self.output, self.config,
                    ), 1)

    def test_short_link_cannot_report_success_from_an_unchanged_gallery(self):
        self.write_gallery()
        with patch("main.importlib.util.find_spec", return_value=object()), patch(
            "main.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ), self.assertLogs("main", level="ERROR"):
            self.assertEqual(main.download_video(
                "https://v.douyin.com/example/", self.output, self.config,
            ), 1)

    def test_backend_failure(self):
        with patch("main.importlib.util.find_spec", return_value=object()), patch(
            "main.subprocess.run", return_value=subprocess.CompletedProcess([], 2)
        ), self.assertLogs("main", level="ERROR"):
            self.assertEqual(main.download_video(self.url, self.output, self.config), 1)

    def test_existing_target_refreshes_archive_without_requiring_new_video(self):
        self.output.mkdir()
        path = self.output / "123456789_video.mp4"
        path.write_bytes(b"existing")
        with patch("main.importlib.util.find_spec", return_value=object()), patch(
            "main.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run, self.assertLogs("main", level="INFO") as logs:
            self.assertEqual(main.download_video(self.url, self.output, self.config), 0)
        run.assert_called_once()
        self.assertTrue(any("视频已存在" in line for line in logs.output))

    def test_browser_once_does_not_skip_product_refresh_for_existing_video(self):
        self.output.mkdir()
        (self.output / "123456789_video.mp4").write_bytes(b"existing")
        with patch("main.importlib.util.find_spec", return_value=object()), patch(
            "main.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            self.assertEqual(main.download_video(self.url, self.output, self.config, True), 0)
        run.assert_called_once()
        self.assertIn("--browser-login", run.call_args.args[0])
    def test_missing_config(self):
        with self.assertRaises(FileNotFoundError):
            main.download_video(self.url, self.output, self.root / "absent.yaml")
        self.assertFalse(self.output.exists())

    def test_missing_f2(self):
        with patch("main.importlib.util.find_spec", return_value=None), self.assertRaisesRegex(
            RuntimeError, "requirements-download"
        ):
            main.download_video(self.url, self.output, self.config)

    def test_default_cli_prompts_without_loading_pipeline(self):
        with patch("builtins.input", return_value=self.url), patch(
            "main.download_video", return_value=0
        ) as download:
            self.assertEqual(main.main([]), 0)
        download.assert_called_once_with(
            self.url, main.BASE_DIR / "Download" / "douyin",
            main.BASE_DIR / "douyin.example.yaml",
        )

    def test_cli_custom_paths(self):
        with patch("main.download_video", return_value=0) as download:
            self.assertEqual(main.main([
                self.url, "--output", str(self.output), "--config", str(self.config)
            ]), 0)
        download.assert_called_once_with(self.url, self.output, self.config)

    def test_eof_and_invalid_input_report_failure(self):
        with patch("builtins.input", side_effect=EOFError), self.assertLogs("main", level="ERROR"):
            self.assertEqual(main.main([]), 1)
        with self.assertLogs("main", level="ERROR"):
            self.assertEqual(main.main(["not a URL"]), 1)

    def test_pipeline_rejects_download_arguments(self):
        with self.assertRaises(SystemExit) as raised:
            main.main(["--pipeline", self.url])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
