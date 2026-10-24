import json
import logging
from datetime import datetime, timezone
from hashlib import file_digest
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from main import existing_video
from product_links import build_product_evidence, save_product_links


logger = logging.getLogger(__name__)
COVER_FIELDS = ("cover", "origin_cover", "dynamic_cover", "animated_cover")


def write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def file_record(path: Path) -> dict:
    with path.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest}


def gallery_files(report: dict, output: Path) -> list[Path]:
    count = report.get("image_count")
    assets = report.get("assets")
    if (
        report.get("media_type") != "image_gallery"
        or not isinstance(count, int) or isinstance(count, bool) or count <= 0
        or not isinstance(assets, list)
    ):
        return []
    images = [
        asset for asset in assets if isinstance(asset, dict)
        and isinstance(asset.get("source_field"), str)
        and asset["source_field"].startswith("images.")
    ]
    if len(images) != count:
        return []
    paths = []
    for index, asset in enumerate(images):
        name = asset.get("file")
        if (
            asset["source_field"] != f"images.{index}"
            or asset.get("status") not in ("downloaded", "existing")
            or not isinstance(name, str) or name != Path(name).name
        ):
            return []
        path = output / name
        if not path.is_file() or path.stat().st_size == 0:
            return []
        if file_record(path) != {
            key: asset.get(key) for key in ("file", "bytes", "sha256")
        }:
            return []
        paths.append(path)
    return paths


def media_extension(header: bytes, kind: str) -> str | None:
    if kind == "image":
        if header.startswith(b"\xff\xd8\xff"):
            return ".jpeg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return ".webp"
        if header[4:8] == b"ftyp" and header[8:12] in (b"avif", b"avis"):
            return ".avif"
    else:
        if header.startswith(b"ID3"):
            return ".mp3"
        if len(header) >= 2 and header[0] == 0xFF:
            if header[1] & 0xF6 == 0xF0:
                return ".aac"
            if header[1] & 0xE0 == 0xE0 and header[1] & 0x06:
                return ".mp3"
        if header[4:8] == b"ftyp":
            return ".m4a"
        if header.startswith(b"OggS"):
            return ".ogg"
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            return ".wav"
    return None


def asset_urls(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("url_list", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    urls = []
    for url in value:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in ("https", "http") and parsed.hostname
                and not parsed.username and not parsed.password and parsed.port in (None, 80, 443)
            )
        except ValueError:
            valid = False
        if valid and url not in urls:
            urls.append(url)
    return urls


async def download_asset(
    client: httpx.AsyncClient, output: Path, target_id: str,
    field: str, value: object, kind: str, previous: dict,
) -> dict:
    result = {"source_field": field, "kind": kind}
    urls = asset_urls(value)
    if not urls:
        status = "not_provided" if not value else "failed"
        reason = "no_media_url" if not value else "no_supported_http_url"
        logger.warning("素材 %s: %s", field, reason)
        return {**result, "status": status, "reason": reason}
    stem = f"{target_id}_{field.replace('.', '_')}"
    old_file = previous.get("file")
    if isinstance(old_file, str) and old_file == Path(old_file).name:
        path = output / old_file
        if path.stem == stem and path.is_file() and path.stat().st_size > 0:
            saved = file_record(path)
            if (
                saved["sha256"] == previous.get("sha256")
                and saved["bytes"] == previous.get("bytes")
            ):
                return {**result, **saved, "status": "existing"}
    temporary = output / f"{stem}.part"
    failures = []
    try:
        for url in urls:
            try:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        failures.append({"reason": "unexpected_http_status", "http_status": response.status_code})
                        continue
                    size = 0
                    header = b""
                    with temporary.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            header = (header + chunk)[:64] if len(header) < 64 else header
                            size += len(chunk)
                            stream.write(chunk)
                    declared_size = response.headers.get("content-length")
                    if (
                        declared_size is not None and not response.headers.get("content-encoding")
                        and (not declared_size.isdigit() or int(declared_size) != size)
                    ):
                        failures.append({"reason": "content_length_mismatch"})
                        continue
                    extension = media_extension(header, kind)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if size == 0 or extension is None or content_type in (
                        "text/html", "application/json", "text/plain",
                    ):
                        failures.append({"reason": "empty_or_unrecognized_media"})
                        continue
                    path = output / f"{stem}{extension}"
                    temporary.replace(path)
                    logger.info("已保存素材 %s: %s", field, path)
                    return {**result, **file_record(path), "status": "downloaded", "attempt_failures": failures}
            except httpx.HTTPError as error:
                failure = {"reason": type(error).__name__}
                if isinstance(error, httpx.HTTPStatusError):
                    failure["http_status"] = error.response.status_code
                failures.append(failure)
        logger.warning("素材下载失败: %s（%s 个候选地址），请重新输入视频链接重试。", field, len(urls))
        return {**result, "status": "failed", "attempt_failures": failures}
    finally:
        temporary.unlink(missing_ok=True)


def save_archive_report(report: dict, output: Path) -> Path:
    target_id = report["video_id"]
    if report.get("media_type") == "image_gallery":
        report["video"] = {"status": "not_applicable"}
        missing_media = not gallery_files(report, output)
    else:
        video = existing_video(output, target_id)
        report["video"] = (
            {**file_record(video), "status": "present"}
            if video is not None else {"status": "missing"}
        )
        missing_media = video is None
    report["status"] = (
        "partial" if missing_media or any(
            asset["status"] == "failed" for asset in report["assets"]
        ) else "captured_response_archived"
    )
    path = output / f"{target_id}_archive.json"
    write_json(path, report)
    if report["status"] == "partial":
        logger.warning("作品归档尚不完整，主要媒体缺失或有素材下载失败，详见: %s", path)
    else:
        logger.info("已归档本次作品响应；未提供/不可获取的素材及覆盖限制见: %s", path)
    return path


async def archive_video(
    item: dict, output: Path, user_agent: str, source: dict | None = None,
) -> dict:
    target_id = str(item.get("aweme_id", ""))
    if not target_id.isascii() or not target_id.isdigit():
        raise ValueError("作品归档需要有效的数字视频 ID。")
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"{target_id}_archive.json"
    previous = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            logger.warning("旧归档清单无法解析，将重新下载附属素材: %s", manifest_path)
        if not isinstance(previous, dict):
            logger.warning("旧归档清单格式无效，将重新下载附属素材: %s", manifest_path)
            previous = {}
    previous_assets = previous.get("assets", [])
    if not isinstance(previous_assets, list):
        logger.warning("旧归档素材清单格式无效，将重新下载附属素材: %s", manifest_path)
        previous_assets = []
    previous_by_field = {
        asset["source_field"]: asset for asset in previous_assets
        if isinstance(asset, dict) and isinstance(asset.get("source_field"), str)
    }
    raw_path = output / f"{target_id}_detail_raw.json"
    metadata_path = output / f"{target_id}_metadata.json"
    desc_path = output / f"{target_id}_desc.txt"
    write_json(raw_path, item)
    created = item.get("create_time")
    created_utc = None
    if isinstance(created, int) and not isinstance(created, bool) and 0 <= created <= 253402300799:
        created_utc = datetime.fromtimestamp(created, timezone.utc).isoformat()
    is_gallery = item.get("aweme_type") == 68
    images = item.get("images")
    image_count = len(images) if is_gallery and isinstance(images, list) else 0
    media_type = "image_gallery" if is_gallery else "video"
    metadata = {
        "schema_version": 1, "video_id": target_id,
        "aweme_type": item.get("aweme_type"), "media_type": media_type,
        "image_count": image_count, "images": images,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": build_product_evidence(item, source)["source"],
        "scope": "one_matched_image_post_response" if is_gallery else "one_matched_video_response",
        "description": item.get("desc"), "create_time": created,
        "create_time_utc": created_utc,
        "author": item.get("author"), "statistics": item.get("statistics"),
        "text_extra": item.get("text_extra"), "cha_list": item.get("cha_list"),
        "video_tag": item.get("video_tag"), "music": item.get("music"),
        "video": item.get("video"),
        "missing_fields": [
            key for key in ("desc", "create_time", "author", "statistics", "text_extra",
                            "cha_list", "video_tag", "music",
                            "images" if is_gallery else "video") if key not in item
        ],
        "raw_file": raw_path.name,
    }
    write_json(metadata_path, metadata)
    description = item.get("desc")
    write_text(desc_path, description if isinstance(description, str) else "")
    save_product_links(item, output, source)
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    music = item.get("music") if isinstance(item.get("music"), dict) else {}
    specs = [
        (f"video.{field}", video.get(field), "image") for field in COVER_FIELDS
    ]
    if is_gallery:
        if image_count == 0:
            logger.warning("图文作品没有提供可用的图片列表，保留作品资料但归档不完整。")
        else:
            specs.extend((f"images.{index}", image, "image") for index, image in enumerate(images))
    specs.append(("music.play_url", music.get("play_url"), "audio"))
    assets = []
    # No login cookies or API security headers are sent to asset hosts.
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Referer": "https://www.douyin.com/"},
        follow_redirects=True, timeout=httpx.Timeout(60, connect=20),
    ) as client:
        for field, value, kind in specs:
            if field == "music.play_url" and music.get("status") not in (None, 1):
                logger.warning("平台标记音乐不可下载，保留音乐信息但不请求音频。")
                assets.append({"source_field": field, "kind": kind, "status": "unavailable",
                               "reason": "platform_music_status", "platform_status": music.get("status")})
                continue
            assets.append(await download_asset(
                client, output, target_id, field, value, kind, previous_by_field.get(field, {}),
            ))
    report = {
        "schema_version": 1, "video_id": target_id,
        "media_type": media_type, "image_count": image_count,
        "captured_at": metadata["captured_at"], "source": metadata["source"],
        "scope": (
            "one_matched_image_post_response_and_available_images_cover_music_assets"
            if is_gallery else "one_matched_video_response_and_available_cover_music_assets"
        ),
        "all_platform_information_verified": False,
        "limitations": [
            "Only fields in the matched work response are archived; missing fields are not fabricated.",
            "No comments, other author works, or independent product-list reconciliation.",
            "Music is the platform-provided track, not audio extracted from the MP4.",
            "Image galleries archive supplied still images, not Live Photo motion clips or a synthesized MP4.",
        ],
        "files": [file_record(path) for path in (
            raw_path, metadata_path, desc_path,
            output / f"{target_id}_products.json", output / f"{target_id}_products.txt",
            output / f"{target_id}_products_raw.json",
        )],
        "assets": assets,
    }
    save_archive_report(report, output)
    return report
