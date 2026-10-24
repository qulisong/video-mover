import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from urllib.parse import urlsplit

import httpx
from f2.apps.douyin.dl import DouyinDownloader
from f2.apps.douyin.filter import PostDetailFilter
from f2.exceptions.api_exceptions import APIError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import BrowserContext, Page, Response, async_playwright
from prompt_toolkit import PromptSession

from main import existing_video, parse_video_url, prepare_video_directory, video_id
from video_archive import archive_video, gallery_files, save_archive_report


logger = logging.getLogger(__name__)
PROFILE_DIR = Path(__file__).resolve().parent / ".browser-profile" / "douyin"


class BrowserDownloadError(RuntimeError):
    pass


class BrowserClosedError(BrowserDownloadError):
    pass


def select_video(payload: object, target_id: str) -> dict | None:
    if not isinstance(payload, dict) or payload.get("status_code", 0) != 0:
        return None
    candidates = [payload.get("aweme_detail")]
    aweme_list = payload.get("aweme_list")
    if isinstance(aweme_list, list):
        candidates.extend(aweme_list)
    for item in candidates:
        if isinstance(item, dict) and str(item.get("aweme_id")) == target_id:
            return item
    return None


class BrowserSession:
    def __init__(self, context: BrowserContext, page: Page, timeout: float = 300):
        self.context = context
        self.page = page
        self.timeout = timeout
        self.prompt: PromptSession[str] | None = None
        self.last_capture_source: dict | None = None

    def ensure_open(self) -> None:
        if self.page.is_closed():
            raise BrowserClosedError("浏览器窗口已关闭，会话结束。")

    async def wait_for_login(self) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.ensure_open()
            try:
                cookies = await self.context.cookies("https://www.douyin.com/")
            except PlaywrightError:
                self.ensure_open()
                raise BrowserDownloadError("无法读取浏览器登录状态，请检查浏览器窗口。") from None
            if any(
                cookie["name"] in ("sessionid", "sessionid_ss") and cookie["value"]
                for cookie in cookies
            ):
                return
            await asyncio.sleep(1)
        raise BrowserDownloadError("等待抖音登录超时，请在浏览器完成登录后重试。")

    async def capture_video(self, options: dict) -> tuple[dict, str]:
        self.last_capture_source = None
        self.ensure_open()
        logger.info("正在检查登录态；如已退出登录，请在保留的浏览器窗口中重新登录。")
        await self.wait_for_login()
        page = self.page
        pending: set[asyncio.Task] = set()
        schedule_response = None
        try:
            deadline = time.monotonic() + self.timeout
            target_id = video_id(options["url"])
            target_url = options["url"]
            if target_id is None:
                await page.goto(target_url, wait_until="domcontentloaded")
                target_id = video_id(page.url)
                if target_id is None:
                    raise BrowserDownloadError("短链接未跳转到可识别的作品，请改用标准视频或图文链接。")
                target_url = parse_video_url(page.url)
            queue: asyncio.Queue[tuple[dict, dict]] = asyncio.Queue(maxsize=1)
            failures = 0

            async def on_response(response: Response) -> None:
                nonlocal failures
                parsed = urlsplit(response.url)
                if (
                    parsed.hostname != "www.douyin.com"
                    or not parsed.path.startswith("/aweme/v1/web/")
                    or "json" not in response.headers.get("content-type", "")
                ):
                    return
                if response.status != 200:
                    failures += 1
                    return
                try:
                    payload = await response.json()
                except (PlaywrightError, ValueError):
                    failures += 1
                    return
                item = select_video(payload, target_id)
                if item is not None and queue.empty():
                    queue.put_nowait((item, {
                        "host": parsed.hostname, "path": parsed.path, "http_status": response.status,
                    }))

            def schedule_response(response: Response) -> None:
                task = asyncio.create_task(on_response(response))
                pending.add(task)
                task.add_done_callback(pending.discard)

            page.on("response", schedule_response)
            await page.goto(target_url, wait_until="domcontentloaded")
            logger.info(
                "已打开目标作品，等待正常网页返回作品数据。"
                "如有登录或验证提示，请手动完成；必要时刷新目标作品页面。"
            )
            while time.monotonic() < deadline:
                self.ensure_open()
                try:
                    item, source = await asyncio.wait_for(queue.get(), timeout=1)
                    self.last_capture_source = source
                    user_agent = await page.evaluate("navigator.userAgent")
                    return item, user_agent
                except asyncio.TimeoutError:
                    continue
            raise BrowserDownloadError(
                f"未获取到目标作品的正常网页数据（失败或无法解析的接口响应: {failures}）。"
                "请确认浏览器中该作品可正常查看；程序不会绕过验证或访问限制。"
            )
        except PlaywrightError:
            self.ensure_open()
            raise BrowserDownloadError(
                "浏览器访问失败或连接已断开，请检查浏览器窗口和网络后重试。"
            ) from None
        finally:
            if schedule_response is not None:
                page.remove_listener("response", schedule_response)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


@asynccontextmanager
async def open_browser_session(timeout: float = 300):
    if timeout <= 0:
        raise BrowserDownloadError("浏览器等待时间必须大于零。")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), channel="msedge", headless=False,
            )
        except PlaywrightError:
            raise BrowserDownloadError(
                "无法启动 Edge。请确认已安装 Microsoft Edge，且没有其他下载进程占用独立浏览器目录。"
            ) from None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            session = BrowserSession(context, page, timeout)
            logger.info(
                "已打开独立 Edge。请手动登录抖音；浏览器会保持打开，"
                "登录态只保存在本机。等待登录或单条作品数据最多 5 分钟。"
            )
            await session.wait_for_login()
            yield session
        except PlaywrightError:
            raise BrowserDownloadError("浏览器会话连接失败，请检查窗口和网络。") from None
        finally:
            try:
                await context.close()
            except PlaywrightError:
                logger.warning("浏览器会话已断开，关闭时无法确认登录态是否已保存。")


async def capture_video(options: dict, timeout: float = 300) -> tuple[dict, str]:
    async with open_browser_session(timeout) as session:
        return await session.capture_video(options)


async def download_with_browser(
    options: dict, output: Path, session: BrowserSession | None = None
) -> Path:
    if session is None:
        item, user_agent = await capture_video(options)
    else:
        item, user_agent = await session.capture_video(options)
    return await download_captured_video(
        options, output, item, user_agent,
        session.last_capture_source if session is not None else None,
    )


async def download_captured_video(
    options: dict, output: Path, item: dict, user_agent: str, source: dict | None = None,
) -> Path:
    if item.get("aweme_type") not in (0, 55, 61, 68, 109, 201):
        raise BrowserDownloadError("目标作品不是当前支持的普通视频或图文类型。")
    if item.get("status", {}).get("is_prohibited"):
        raise BrowserDownloadError("平台标记该作品不可下载。")
    output = prepare_video_directory(output, str(item.get("aweme_id", "")))
    report = await archive_video(item, output, user_agent, source)
    if report["media_type"] == "image_gallery":
        images = gallery_files(report, output)
        if not images:
            raise BrowserDownloadError(
                "图文作品图片缺失或下载失败，已保留作品资料和归档清单；请重新输入链接重试。"
            )
        manifest = output / f"{item['aweme_id']}_archive.json"
        logger.info("已保存图文作品的 %s 张图片（不生成 MP4），归档清单: %s", len(images), manifest)
        return manifest
    existing = existing_video(output, str(item["aweme_id"]))
    if existing:
        logger.info("视频已存在，已更新作品资料并补充封面/音乐，跳过视频下载: %s", existing)
        return existing
    data = PostDetailFilter({"aweme_detail": item, "status_code": 0})._to_dict()
    if not data.get("video_play_addr"):
        raise BrowserDownloadError("浏览器响应中没有可用的视频地址，无法交给 F2 下载。")
    # Account cookies and API signatures are not needed by the media CDN.
    media_options = {
        **options, "cookie": "", "interval": "all", "music": False, "cover": False, "desc": False,
        "path": str(output), "folderize": False, "naming": "{aweme_id}",
        "headers": {"User-Agent": user_agent, "Referer": "https://www.douyin.com/"},
    }
    output.mkdir(parents=True, exist_ok=True)
    logger.info("已从浏览器获取目标视频数据，交由 F2 下载；不复制或伪造网页接口签名。")
    async with DouyinDownloader(media_options) as downloader:
        await downloader.create_download_tasks(media_options, data, output)
    saved = existing_video(output, str(item["aweme_id"]))
    if saved is None:
        raise BrowserDownloadError("F2 未生成目标视频的非空 MP4，请检查上方下载日志后重试。")
    save_archive_report(report, output)
    logger.info("已保存视频: %s", saved)
    return saved


async def read_next_link(session: BrowserSession) -> str | None:
    if sys.stdin.isatty():
        if session.prompt is None:
            session.prompt = PromptSession()

        async def wait_for_close() -> None:
            while True:
                session.ensure_open()
                await asyncio.sleep(0.1)

        read_task = asyncio.create_task(
            session.prompt.prompt_async("请输入抖音视频或图文链接（exit 退出）: ")
        )
        close_task = asyncio.create_task(wait_for_close())
        try:
            done, _ = await asyncio.wait(
                (read_task, close_task), return_when=asyncio.FIRST_COMPLETED
            )
            if close_task in done:
                await close_task
            try:
                return await read_task
            except EOFError:
                return None
        finally:
            for task in (read_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read_task, close_task, return_exceptions=True)

    result: Queue[str | None | OSError] = Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(input("请输入抖音视频或图文链接（exit 退出）: "))
        except EOFError:
            result.put(None)
        except OSError as error:
            result.put(error)

    # A closed browser must not wait for a blocked console reader during shutdown.
    Thread(target=read, daemon=True).start()
    while True:
        session.ensure_open()
        try:
            value = result.get_nowait()
        except Empty:
            await asyncio.sleep(0.1)
            continue
        if isinstance(value, OSError):
            raise BrowserDownloadError("无法读取终端输入，请从交互式终端启动。") from None
        return value


async def run_browser_session(options: dict, output: Path) -> None:
    async with open_browser_session() as session:
        logger.info("浏览器会话已就绪，可连续输入链接；输入 exit 或按 Ctrl+C 退出。")
        text = options.get("url")
        while True:
            try:
                session.ensure_open()
                if text is None:
                    text = await read_next_link(session)
                if text is None or text.strip().lower() in ("exit", "quit"):
                    logger.info("已退出会话，正在关闭浏览器并保留本地登录态。")
                    return
                if not text.strip():
                    text = None
                    continue
                url = parse_video_url(text)
                await download_with_browser({**options, "url": url}, output, session)
            except BrowserClosedError as error:
                logger.info("%s", error)
                return
            except BrowserDownloadError as error:
                logger.error("%s；浏览器保持打开，可继续输入链接重试。", error)
            except (APIError, httpx.HTTPError, OSError) as error:
                logger.error("下载失败（%s）；浏览器保持打开，可重试或输入下一条。", type(error).__name__)
            except ValueError as error:
                logger.error("%s", error)
            text = None
