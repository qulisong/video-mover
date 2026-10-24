import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit


BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def video_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname not in ("www.douyin.com", "douyin.com"):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 2 and parts[0] in ("video", "note") and parts[1].isdigit():
        return parts[1]
    modal_id = parse_qs(parsed.query).get("modal_id", [""])[0]
    return modal_id if modal_id.isdigit() else None


def existing_video(directory: Path, target_id: str | None) -> Path | None:
    if target_id:
        for path in directory.rglob(f"{target_id}_video.mp4"):
            if path.is_file() and path.stat().st_size > 0:
                return path
    return None


def prepare_video_directory(root: Path, target_id: str) -> Path:
    if not isinstance(target_id, str) or not target_id.isascii() or not target_id.isdigit():
        raise ValueError("视频目录需要有效的数字视频 ID。")
    directory = root / target_id
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise FileExistsError(f"视频目录路径被其他文件或链接占用: {directory}")
    legacy_files = sorted(root.glob(f"{target_id}_*"))
    moves = []
    for source in legacy_files:
        if source.is_symlink():
            raise FileExistsError(f"旧版下载文件是链接，请先手动处理: {source}")
        if not source.is_file():
            continue
        destination = directory / source.name
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"视频目录中存在同名文件，未迁移或覆盖，请先手动处理: {destination}")
        moves.append((source, destination))
    directory.mkdir(parents=True, exist_ok=True)
    for source, destination in moves:
        source.rename(destination)
    if moves:
        logger.info("已将 %s 个旧版文件整理到视频目录: %s", len(moves), directory)
    return directory


def parse_video_url(text: str) -> str:
    urls = re.findall(r"https?://[^\s<>\"']+", text)
    for candidate in urls:
        candidate = candidate.rstrip(".,;!?，。；！？）)]}")
        url = urlsplit(candidate)
        if url.username or url.password or url.port not in (None, 443):
            continue
        if url.scheme != "https":
            continue
        if url.hostname == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+/?", url.path):
            return candidate
        if url.hostname in ("www.douyin.com", "douyin.com") and re.fullmatch(
            r"/(?:video|note)/\d+/?", url.path
        ):
            return candidate
        if url.hostname in ("www.douyin.com", "douyin.com"):
            modal_id = parse_qs(url.query).get("modal_id", [""])[0]
            if re.fullmatch(r"\d+", modal_id):
                return f"https://www.douyin.com/video/{modal_id}"
    raise ValueError("请输入抖音单条视频或图文链接，或包含 https://v.douyin.com/ 链接的分享文案。")


def video_snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    result = {}
    for path in directory.rglob("*.mp4"):
        stat = path.stat()
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def download_video(url: str, output: Path, config: Path, browser_login: bool = False) -> int:
    url = parse_video_url(url)
    if not config.is_file():
        raise FileNotFoundError(f"下载配置文件不存在: {config}")
    if importlib.util.find_spec("f2") is None:
        raise RuntimeError(
            f'缺少 F2，请执行: "{sys.executable}" -m pip install -r requirements-download.txt'
        )
    existing = existing_video(output, video_id(url))
    output.mkdir(parents=True, exist_ok=True)
    before = video_snapshot(output)
    before_archives = {
        path: path.stat().st_mtime_ns for path in output.rglob("*_archive.json")
    }
    command = [
        sys.executable, "-X", "utf8", str(BASE_DIR / "f2_download.py"),
        "--config", str(config), "--url", url, "--output", str(output),
    ]
    if browser_login:
        command.append("--browser-login")
    logger.info("开始下载抖音作品，保存目录: %s", output)
    process = subprocess.run(
        command, cwd=BASE_DIR, env={**os.environ, "PYTHONUTF8": "1"}, check=False
    )
    if process.returncode != 0:
        logger.error("F2 下载失败，退出码: %s；请检查上方错误、Cookie 和网络。", process.returncode)
        return 1
    if browser_login:
        logger.info("浏览器作品归档处理完成；附属素材状态及缺失项见归档清单和上方日志。")
        return 0
    target_id = video_id(url)
    manifests = (
        [output / target_id / f"{target_id}_archive.json"] if target_id else [
            path for path in output.rglob("*_archive.json")
            if before_archives.get(path) != path.stat().st_mtime_ns
        ]
    )
    for manifest in manifests:
        if not manifest.is_file():
            continue
        try:
            report = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            logger.error("下载后的归档清单无法解析: %s", manifest)
            return 1
        if not isinstance(report, dict):
            logger.error("下载后的归档清单格式无效: %s", manifest)
            return 1
        if report.get("media_type") == "image_gallery":
            from video_archive import gallery_files

            images = gallery_files(report, manifest.parent)
            if not images:
                logger.error("图文作品没有完整且校验通过的图片，请检查归档清单: %s", manifest)
                return 1
            logger.info("已保存图文作品的 %s 张图片，归档清单: %s", len(images), manifest)
            return 0
    if existing:
        saved = existing_video(output, video_id(url))
        if saved is None:
            logger.error("处理结束后未找到原有视频，请检查视频目录和上方日志。")
            return 1
        logger.info("视频已存在，已请求更新作品资料及缺失素材: %s", saved)
        return 0
    after = video_snapshot(output)
    downloaded = [
        path for path, state in after.items()
        if state[0] > 0 and before.get(path) != state
    ]
    if not downloaded:
        logger.error(
            "F2 已退出，但未发现新增或更新的非空 MP4。"
            "也未找到本次图文作品的完整归档；请检查 Cookie、平台验证和上方日志。"
        )
        return 1
    for path in downloaded:
        logger.info("已保存视频: %s", path)
    return 0


def run_browser_session(output: Path, config: Path, initial_url: str | None = None) -> int:
    if not config.is_file():
        raise FileNotFoundError(f"下载配置文件不存在: {config}")
    for package in ("f2", "playwright", "prompt_toolkit"):
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(
                f'缺少 {package}，请执行: "{sys.executable}" -m pip install -r requirements-download.txt'
            )
    command = [
        sys.executable, "-X", "utf8", str(BASE_DIR / "f2_download.py"),
        "--browser-session", "--config", str(config), "--output", str(output),
    ]
    if initial_url is not None:
        command.extend(["--url", parse_video_url(initial_url)])
    return subprocess.run(
        command, cwd=BASE_DIR, env={**os.environ, "PYTHONUTF8": "1"}, check=False
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载抖音单条视频或图文（默认不加工、不上传）。")
    parser.add_argument("url", nargs="?", help="抖音视频、图文链接或完整分享文案")
    parser.add_argument("--output", type=Path, help="下载根目录，每个作品保存到其 ID 子目录，默认 Download\\douyin")
    parser.add_argument("--config", type=Path, help="F2 配置，默认 douyin.example.yaml")
    parser.add_argument(
        "--browser-login", action="store_true",
        help="启动常驻浏览器会话，登录后连续输入视频或图文链接，输入 exit 退出",
    )
    parser.add_argument(
        "--pipeline", action="store_true",
        help="运行旧版 TikTok 下载、加工和视频号上传流水线（需另外配置）",
    )
    args = parser.parse_args(argv)
    if args.pipeline:
        if args.url or args.output is not None or args.config is not None or args.browser_login:
            parser.error("--pipeline 不能与抖音下载参数一起使用")
        from pipeline import run_pipeline
        run_pipeline()
        return 0
    try:
        output = (args.output if args.output is not None else BASE_DIR / "Download" / "douyin").resolve()
        config = (args.config if args.config is not None else BASE_DIR / "douyin.example.yaml").resolve()
        if args.browser_login:
            return run_browser_session(output, config, args.url)
        text = args.url if args.url is not None else input("请输入抖音视频或图文链接或分享文案: ")
        return download_video(text, output, config)
    except (ValueError, OSError, RuntimeError) as error:
        logger.error("%s", error)
        return 1
    except EOFError:
        logger.error("未收到视频链接。也可以运行: python main.py \"抖音视频链接\"")
        return 1
    except KeyboardInterrupt:
        logger.info("已退出下载会话。")
        return 130


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
