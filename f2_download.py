import argparse
import asyncio
import logging
from pathlib import Path

import f2
import httpx
from f2.apps.douyin.crawler import DouyinCrawler
from f2.apps.douyin.model import PostDetail
from f2.apps.douyin.utils import AwemeIdFetcher, ClientConfManager
from f2.exceptions.api_exceptions import APIError
from f2.cli.cli_console import RichConsoleManager
from f2.utils._signal import SignalManager
from f2.utils.conf_manager import ConfigManager
from f2.utils.utils import merge_config
from yaml import YAMLError


logger = logging.getLogger(__name__)


async def run_douyin(options: dict) -> None:
    from browser_login import BrowserDownloadError, download_captured_video, select_video

    target_id = str(await AwemeIdFetcher.get_aweme_id(options["url"]))
    async with DouyinCrawler(options) as crawler:
        payload = await crawler.fetch_post_detail(PostDetail(aweme_id=target_id))
    item = select_video(payload, target_id)
    if item is None:
        raise BrowserDownloadError("详情接口未返回匹配视频的数据，请检查登录态或改用 --browser-login。")
    await download_captured_video(
        options, Path(options["path"]), item, options["headers"]["User-Agent"],
        {"host": "www.douyin.com", "path": "/aweme/v1/web/aweme/detail/"},
    )


def build_options(config: Path, url: str | None, output: Path) -> dict:
    base = ConfigManager(f2.APP_CONFIG_FILE_PATH).get_config("douyin")
    local = ConfigManager(str(config)).get_config("douyin")
    if not isinstance(local, dict) or not local:
        raise ValueError("配置必须包含非空的 douyin 配置段。")
    custom_headers = local.get("headers", {})
    if not isinstance(custom_headers, dict):
        raise ValueError("douyin.headers 必须是请求头映射。")
    headers = {
        "user-agent": ClientConfManager.user_agent(),
        "referer": ClientConfManager.referer(),
    }
    for key, value in custom_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("请求头名称和值必须是字符串。")
        if not key or any(char in key for char in "\r\n:") or any(
            char in value for char in "\r\n"
        ):
            raise ValueError("请求头名称或值包含非法字符。")
        if key.lower() == "cookie":
            raise ValueError("请通过 douyin.cookie 配置 Cookie，不要放在 headers 中。")
        headers[key.lower()] = value
    uifid = local.get("uifid")
    if uifid is not None and uifid != "":
        if not isinstance(uifid, str) or not uifid.strip() or any(
            char in uifid for char in "\r\n"
        ):
            raise ValueError("douyin.uifid 必须是非空的单行字符串。")
        headers["uifid"] = uifid.strip()
    headers["User-Agent"] = headers.pop("user-agent")
    headers["Referer"] = headers.pop("referer")
    options = merge_config(
        base, local, headers=headers, app_name="douyin",
        url=url, mode="one", path=str(output), naming="{aweme_id}",
        folderize=False, music=False, cover=False, desc=True,
    )
    options["url"] = url
    return options


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("f2").propagate = False
    parser = argparse.ArgumentParser(description="项目内的 F2 抖音下载适配入口。")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--url")
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browser-login", action="store_true")
    mode.add_argument("--browser-session", action="store_true")
    args = parser.parse_args(argv)
    if not args.browser_session and not args.url:
        parser.error("单作品模式必须提供 --url")
    try:
        options = build_options(args.config, args.url, args.output)
    except (ValueError, OSError, YAMLError):
        logger.error("无法加载下载配置，请检查文件、YAML 格式及字段类型；配置值不会输出。")
        return 1
    if args.browser_login or args.browser_session:
        logger.info("浏览器模式：复用独立浏览器登录态，不依赖手填 Cookie 或 UIFID。")
        from browser_login import BrowserDownloadError, download_with_browser, run_browser_session
        try:
            if args.browser_session:
                asyncio.run(run_browser_session(options, args.output))
            else:
                logger.info("已加载自定义请求头；UIFID 请求头已配置: %s", bool(options["headers"].get("uifid")))
                asyncio.run(download_with_browser(options, args.output))
        except BrowserDownloadError as error:
            logger.error("%s", error)
            return 1
        except KeyboardInterrupt:
            logger.info("已退出会话，浏览器已关闭，本地登录态保留。")
            return 130
    else:
        from browser_login import BrowserDownloadError

        SignalManager().register_shutdown_signal()
        try:
            with RichConsoleManager().progress:
                asyncio.run(run_douyin(options))
        except (APIError, OSError, ValueError, BrowserDownloadError, httpx.HTTPError) as error:
            logger.error("视频归档失败（%s），请检查上方日志或改用 --browser-login。", type(error).__name__)
            return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
