import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import f2_download
import video_archive


JPEG = b"\xff\xd8\xff\xe0" + b"cover-data"
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + b"animated-data"
MP3 = b"ID3" + b"audio-data"


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.item = {
            "aweme_id": "123", "aweme_type": 0, "desc": "description #tag",
            "create_time": 1700000000,
            "author": {"uid": "456", "nickname": "author", "extra_field": "preserved"},
            "statistics": {"digg_count": 42}, "text_extra": [{"hashtag_name": "tag"}],
            "cha_list": [{"cha_name": "tag"}], "video_tag": [{"tag_name": "category"}],
            "video": {
                "cover": {"url_list": ["https://cdn.example/cover"]},
                "origin_cover": {"url_list": ["https://cdn.example/original"]},
                "dynamic_cover": {"url_list": ["https://cdn.example/dynamic"]},
            },
            "music": {"title": "track", "status": 1,
                      "play_url": {"url_list": ["https://cdn.example/music"]}},
            "unknown_platform_field": {"keep": True},
        }
        self.requests = []

    async def archive(self, handler=None, item=None):
        def respond(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            if request.url.path == "/music":
                return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
            if request.url.path == "/dynamic":
                return httpx.Response(200, content=WEBP, headers={"content-type": "image/webp"})
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers={
            "User-Agent": "TestBrowser", "Referer": "https://www.douyin.com/",
        }, follow_redirects=True)
        with patch("video_archive.httpx.AsyncClient", return_value=client) as factory:
            report = await video_archive.archive_video(
                self.item if item is None else item, self.root, "TestBrowser",
                {"host": "www.douyin.com", "path": "/aweme/v1/web/aweme/detail/?secret=token",
                 "http_status": 200},
            )
        self.assertEqual(factory.call_args.kwargs["headers"], {
            "User-Agent": "TestBrowser", "Referer": "https://www.douyin.com/",
        })
        return report

    async def test_raw_fidelity_metadata_assets_and_hashes(self):
        original = deepcopy(self.item)
        (self.root / "123_video.mp4").write_bytes(b"existing-video")
        report = await self.archive()
        self.assertEqual(self.item, original)
        self.assertEqual(json.loads((self.root / "123_detail_raw.json").read_text()), original)
        metadata = json.loads((self.root / "123_metadata.json").read_text())
        for key in ("author", "statistics", "text_extra", "cha_list", "video_tag", "music", "video"):
            self.assertEqual(metadata[key], original[key])
        self.assertEqual(metadata["create_time_utc"], "2023-11-14T22:13:20+00:00")
        self.assertNotIn("?", metadata["source"]["path"])
        self.assertEqual((self.root / "123_desc.txt").read_text(), self.item["desc"])
        self.assertEqual((self.root / "123_video_cover.jpeg").read_bytes(), JPEG)
        self.assertEqual((self.root / "123_video_origin_cover.jpeg").read_bytes(), JPEG)
        self.assertEqual((self.root / "123_video_dynamic_cover.webp").read_bytes(), WEBP)
        self.assertEqual((self.root / "123_music_play_url.mp3").read_bytes(), MP3)
        self.assertEqual(report["status"], "captured_response_archived")
        self.assertFalse(report["all_platform_information_verified"])
        for record in [*report["files"], report["video"], *[
            asset for asset in report["assets"] if asset["status"] == "downloaded"
        ]]:
            self.assertEqual(video_archive.file_record(self.root / record["file"]), {
                key: record[key] for key in ("file", "bytes", "sha256")
            })
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse(list(self.root.glob("*.tmp")))

    async def test_retry_uses_alternative_url_without_logging_signed_urls(self):
        self.item["video"]["cover"]["url_list"] = [
            "https://cdn.example/blocked?private-token=value", "https://cdn.example/cover",
        ]

        def handler(request):
            if request.url.path == "/blocked":
                return httpx.Response(403)
            return httpx.Response(200, content=MP3 if request.url.path == "/music" else JPEG)

        with self.assertLogs("video_archive", level="INFO") as logs:
            report = await self.archive(handler)
        cover = report["assets"][0]
        self.assertEqual(cover["status"], "downloaded")
        self.assertEqual(cover["attempt_failures"][0]["http_status"], 403)
        self.assertNotIn("private-token", "\n".join(logs.output))
        self.assertNotIn("private-token", json.dumps(report))

    def gallery_item(self):
        return {
            **self.item, "aweme_type": 68,
            "images": [
                {"url_list": ["https://cdn.example/image0"], "width": 720, "height": 1280},
                {"url_list": ["https://cdn.example/image1"], "width": 720, "height": 1280},
            ],
        }

    async def test_gallery_archives_all_images_in_order_without_requiring_video(self):
        item = self.gallery_item()
        report = await self.archive(item=item)
        self.assertEqual(report["media_type"], "image_gallery")
        self.assertEqual(report["image_count"], 2)
        self.assertEqual(report["status"], "captured_response_archived")
        self.assertEqual(report["video"], {"status": "not_applicable"})
        paths = video_archive.gallery_files(report, self.root)
        self.assertEqual([path.name for path in paths], [
            "123_images_0.jpeg", "123_images_1.jpeg",
        ])
        self.assertTrue(all(path.read_bytes() == JPEG for path in paths))
        self.assertFalse(list(self.root.glob("*.mp4")))
        metadata = json.loads((self.root / "123_metadata.json").read_text())
        self.assertEqual(metadata["images"], item["images"])
        self.assertEqual(metadata["aweme_type"], 68)
        self.assertEqual(metadata["media_type"], "image_gallery")
        self.assertEqual(metadata["image_count"], 2)
        self.requests.clear()
        second = await self.archive(item=item)
        self.assertFalse(self.requests)
        self.assertTrue(all(
            asset["status"] == "existing" for asset in second["assets"]
            if asset["source_field"].startswith("images.")
        ))
        paths[1].write_bytes(b"corrupt")
        await self.archive(item=item)
        self.assertEqual([request.url.path for request in self.requests], ["/image1"])
        self.assertEqual(paths[1].read_bytes(), JPEG)

    async def test_empty_or_malformed_gallery_is_partial_even_with_existing_mp4(self):
        (self.root / "123_video.mp4").write_bytes(b"old-video")
        for images in (None, [], {}, [{}], [None], ["not-a-url"]):
            with self.subTest(images=images):
                item = {**self.gallery_item(), "images": images}
                with self.assertLogs("video_archive", level="WARNING"):
                    report = await self.archive(item=item)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["video"]["status"], "not_applicable")
                self.assertEqual(video_archive.gallery_files(report, self.root), [])

    async def test_gallery_failed_image_is_partial_and_retried(self):
        item = self.gallery_item()
        report = await self.archive(
            lambda request: httpx.Response(
                403 if request.url.path == "/image1" else 200,
                content=MP3 if request.url.path == "/music" else JPEG,
            ), item,
        )
        self.assertEqual(report["status"], "partial")
        self.assertEqual(video_archive.gallery_files(report, self.root), [])
        self.requests.clear()
        repaired = await self.archive(item=item)
        self.assertEqual([request.url.path for request in self.requests], ["/image1"])
        self.assertEqual(len(video_archive.gallery_files(repaired, self.root)), 2)
        self.assertEqual(repaired["status"], "captured_response_archived")

    async def test_html_empty_wrong_media_and_network_errors_are_partial(self):
        for failure in ("html", "empty", "wrong-kind", "network"):
            with self.subTest(failure=failure):
                def handler(request):
                    if failure == "network":
                        raise httpx.ConnectError("https://private-token", request=request)
                    if failure == "empty":
                        return httpx.Response(200, content=b"")
                    if failure == "wrong-kind":
                        return httpx.Response(200, content=MP3)
                    return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})

                item = {"aweme_id": "123", "video": {"cover": self.item["video"]["cover"]}}
                with self.assertLogs("video_archive", level="WARNING"):
                    report = await self.archive(handler, item)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["assets"][0]["status"], "failed")
                self.assertTrue((self.root / "123_detail_raw.json").exists())
                self.assertFalse(list(self.root.glob("*.part")))

    async def test_existing_assets_reused_only_when_hash_matches(self):
        await self.archive()
        path = self.root / "123_video_cover.jpeg"
        before = path.stat().st_mtime_ns
        self.requests.clear()
        second = await self.archive()
        self.assertFalse(self.requests)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(second["assets"][0]["status"], "existing")
        path.write_bytes(b"corrupt")
        self.requests.clear()
        third = await self.archive()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertEqual(third["assets"][0]["status"], "downloaded")

    async def test_failed_asset_is_retried_next_time(self):
        await self.archive(lambda request: httpx.Response(403))
        report = await self.archive()
        self.assertEqual(report["assets"][0]["status"], "downloaded")

    async def test_incomplete_content_length_is_failure(self):
        report = await self.archive(
            lambda request: httpx.Response(200, content=JPEG, headers={"content-length": "99999"})
        )
        self.assertTrue(all(
            asset["status"] == "failed"
            and asset["attempt_failures"][0]["reason"] == "content_length_mismatch"
            for asset in report["assets"] if asset["status"] != "not_provided"
        ))
        self.assertFalse(list(self.root.glob("*.part")))

    async def test_persisted_manifest_updates_after_new_video_is_saved(self):
        report = await self.archive()
        self.assertEqual(report["video"]["status"], "missing")
        (self.root / "123_video.mp4").write_bytes(b"new-video")
        path = video_archive.save_archive_report(report, self.root)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["status"], "captured_response_archived")
        self.assertEqual(saved["video"]["status"], "present")
        self.assertEqual(saved["video"]["bytes"], len(b"new-video"))

    async def test_missing_and_unavailable_assets_not_invented(self):
        report = await self.archive(item={
            "aweme_id": "123", "music": {"status": 0, "play_url": "https://cdn.example/blocked"},
        })
        self.assertFalse(self.requests)
        self.assertEqual(report["assets"][-1]["status"], "unavailable")
        self.assertTrue(all(asset["status"] == "not_provided" for asset in report["assets"][:-1]))
        metadata = json.loads((self.root / "123_metadata.json").read_text())
        self.assertIn("author", metadata["missing_fields"])
        self.assertIsNone(metadata["author"])

    async def test_unusable_urls_are_reported_and_not_requested(self):
        item = {"aweme_id": "123", "video": {"cover": {"url_list": [
            "file:///private", "https://user:password@cdn.example/file", "javascript:invalid",
        ]}}}
        report = await self.archive(item=item)
        self.assertFalse(self.requests)
        self.assertEqual(report["assets"][0]["status"], "failed")

    async def test_corrupt_manifest_is_reported_and_rebuilt(self):
        (self.root / "123_archive.json").write_text("{bad-json")
        with self.assertLogs("video_archive", level="WARNING") as logs:
            await self.archive()
        self.assertTrue(any("旧归档清单无法解析" in message for message in logs.output))
        self.assertEqual(len(self.requests), 4)

    async def test_asset_requests_do_not_include_account_credentials(self):
        await self.archive()
        for request in self.requests:
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("uifid", request.headers)
            self.assertNotIn("signature", request.headers)

    async def test_existing_video_untouched_by_backfill(self):
        video = self.root / "123_video.mp4"
        video.write_bytes(b"existing-video")
        before = video_archive.file_record(video), video.stat().st_mtime_ns
        await self.archive()
        self.assertEqual((video_archive.file_record(video), video.stat().st_mtime_ns), before)

    async def test_invalid_video_id_cannot_write_outside_output(self):
        with self.assertRaises(ValueError):
            await self.archive(item={"aweme_id": "../invalid"})
        self.assertFalse(list(self.root.iterdir()))

    async def test_direct_mode_uses_same_archive_path(self):
        options = {"url": "https://www.douyin.com/video/123", "path": str(self.root),
                   "headers": {"User-Agent": "TestBrowser"}}
        with patch("f2_download.AwemeIdFetcher.get_aweme_id", new=AsyncMock(return_value="123")), patch(
            "f2_download.DouyinCrawler"
        ) as crawler, patch("browser_login.download_captured_video", new=AsyncMock()) as download:
            crawler.return_value.__aenter__.return_value.fetch_post_detail = AsyncMock(
                return_value={"status_code": 0, "aweme_detail": self.item}
            )
            await f2_download.run_douyin(options)
        self.assertEqual(download.call_args.args[2], self.item)
        self.assertEqual(download.call_args.args[1], self.root)

    async def test_direct_mode_rejects_unmatched_or_failed_responses(self):
        options = {"url": "https://www.douyin.com/video/123"}
        with patch("f2_download.AwemeIdFetcher.get_aweme_id", new=AsyncMock(return_value="123")), patch(
            "f2_download.DouyinCrawler"
        ) as crawler:
            crawler.return_value.__aenter__.return_value.fetch_post_detail = AsyncMock(
                return_value={"status_code": 0, "aweme_detail": {"aweme_id": "456"}}
            )
            with self.assertRaisesRegex(RuntimeError, "匹配视频"):
                await f2_download.run_douyin(options)


if __name__ == "__main__":
    unittest.main()
