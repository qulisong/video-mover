import argparse
import logging
import math
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np

from video_archive import file_record, write_json


logger = logging.getLogger(__name__)
PROFILES = {
    "natural": {"diameter": 9, "sigma_color": 32, "sigma_space": 32, "passes": 1, "luma_lift": 6},
    "strong": {"diameter": 15, "sigma_color": 72, "sigma_space": 32, "passes": 2, "luma_lift": 26},
}


class PortraitError(RuntimeError):
    pass


class FaceDetector:
    def __init__(self):
        self.face = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self.eyes = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml"
        )
        if self.face.empty() or self.eyes.empty():
            raise PortraitError("OpenCV 自带的人脸/眼睛检测文件不可用。")

    def detect(self, frame: np.ndarray) -> list[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        scale = min(1.0, 960 / max(gray.shape))
        small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1 else gray
        faces = self.face.detectMultiScale(small, 1.1, 6, minSize=(36, 36))
        confirmed = []
        for box in faces:
            x, y, w, h = (int(round(int(value) / scale)) for value in box)
            w, h = min(w, gray.shape[1] - x), min(h, gray.shape[0] - y)
            eyes = self.eyes.detectMultiScale(
                gray[y:y + int(h * .65), x:x + w], 1.08, 4,
                minSize=(max(12, int(w * .1)), max(12, int(h * .1))),
            )
            eyes = [tuple(map(int, eye)) for eye in eyes]
            pair = next((
                (left, right) for left in eyes for right in eyes
                if left[0] + left[2] / 2 < .5 * w < right[0] + right[2] / 2
                and .2 * w < (right[0] + right[2] / 2) - (left[0] + left[2] / 2) < .7 * w
                and abs((left[1] + left[3] / 2) - (right[1] + right[3] / 2)) < .15 * h
            ), None)
            # Product packaging can resemble a face; require a plausible pair of eyes.
            if pair:
                confirmed.append({"box": (x, y, w, h), "eyes": pair})
        return confirmed


def retouch_frame(
    frame: np.ndarray, faces: list[dict], strength: float, profile: str = "natural",
) -> tuple[np.ndarray, np.ndarray]:
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("strength 必须介于 0 和 1。")
    if profile not in PROFILES:
        raise ValueError("profile 必须是 natural 或 strong。")
    settings = PROFILES[profile]
    result = frame.copy()
    full_mask = np.zeros(frame.shape[:2], dtype=np.float32)
    for face in faces:
        x, y, w, h = face["box"]
        roi = frame[y:y + h, x:x + w]
        if roi.size == 0:
            continue
        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 180, 135))
        ellipse = np.zeros((h, w), dtype=np.uint8)
        cv2.ellipse(ellipse, (w // 2, int(h * .53)), (int(w * .43), int(h * .46)),
                    0, 0, 360, 255, -1)
        mask = cv2.bitwise_and(skin, ellipse).astype(np.float32) / 255
        mask = cv2.GaussianBlur(mask, (0, 0), max(1, w * .015))
        protection = np.ones((h, w), dtype=np.uint8)
        for ex, ey, ew, eh in face["eyes"]:
            margin = max(2, int(w * .025))
            protection[max(0, ey - margin):min(h, ey + eh + margin),
                       max(0, ex - margin):min(w, ex + ew + margin)] = 0
        protection[int(h * .68):int(h * .90), int(w * .23):int(w * .77)] = 0
        if profile == "strong":
            protection[[0, -1], :] = 0
            protection[:, [0, -1]] = 0
            distance = cv2.distanceTransform(protection, cv2.DIST_L2, 3)
            mask *= np.minimum(distance / max(2, w * .035), 1)
        else:
            mask *= protection
        smoothed = roi
        for _ in range(settings["passes"]):
            smoothed = cv2.bilateralFilter(
                smoothed, settings["diameter"], settings["sigma_color"], settings["sigma_space"],
            )
        adjusted = cv2.cvtColor(smoothed, cv2.COLOR_BGR2YCrCb)
        if profile == "strong":
            luma = adjusted[:, :, 0].astype(np.float32)
            # Roll off the lift in highlights; reduce skin chroma without changing geometry.
            lift = settings["luma_lift"] * (1 - .65 * (luma / 255) ** 2)
            adjusted[:, :, 0] = np.rint(np.clip(luma + lift, 0, 255)).astype(np.uint8)
            for channel, factor in ((1, .78), (2, .82)):
                chroma = adjusted[:, :, channel].astype(np.float32)
                adjusted[:, :, channel] = np.rint(128 + (chroma - 128) * factor).astype(np.uint8)
        else:
            adjusted[:, :, 0] = np.minimum(
                adjusted[:, :, 0].astype(np.uint16) + settings["luma_lift"], 255,
            ).astype(np.uint8)
        adjusted = cv2.cvtColor(adjusted, cv2.COLOR_YCrCb2BGR)
        alpha = mask[:, :, None] * strength
        result[y:y + h, x:x + w] = np.rint(
            roi.astype(np.float32) * (1 - alpha) + adjusted.astype(np.float32) * alpha
        ).astype(np.uint8)
        full_mask[y:y + h, x:x + w] = np.maximum(full_mask[y:y + h, x:x + w], mask)
    return result, full_mask


def comparison_frame(
    original: np.ndarray, processed: np.ndarray, profile: str = "natural",
) -> np.ndarray:
    pair = np.hstack((original, processed))
    header = np.zeros((44, pair.shape[1], 3), dtype=np.uint8)
    size = max(.35, min(.65, original.shape[1] / 1100))
    label = "STRONG RETOUCH - SAME PERSON" if profile == "strong" else "FACE RETOUCH - NOT FACE SWAP"
    for label, x in (("ORIGINAL", 12), (label, original.shape[1] + 12)):
        cv2.putText(header, label, (x, 29), cv2.FONT_HERSHEY_SIMPLEX, size, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack((header, pair))


def run_ffmpeg(executable: str, args: list[str]) -> None:
    process = subprocess.run(
        [executable, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if process.returncode:
        raise PortraitError(f"FFmpeg 处理失败: {process.stderr[-3000:].strip()}")


def video_info(path: Path, decode: bool = False) -> dict:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise PortraitError(f"无法读取视频: {path}")
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not math.isfinite(fps) or fps <= 0 or frames <= 0 or min(width, height) <= 0:
            raise PortraitError(f"视频时长、帧率或尺寸不可用: {path}")
        if decode:
            decoded = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame.shape[:2] != (height, width):
                    raise PortraitError(f"解码后视频帧尺寸发生变化: {path}")
                decoded += 1
            if decoded != frames:
                raise PortraitError(f"视频未完整解码: {path}（{decoded}/{frames}）")
        return {"fps": fps, "frames": frames, "width": width, "height": height, "duration": frames / fps}
    finally:
        capture.release()


def create_preview(
    source: Path, output: Path, start: float = 0, duration: float = 8,
    strength: float = .6, profile: str = "natural",
) -> Path:
    if not all(math.isfinite(value) for value in (start, duration, strength)):
        raise ValueError("时间和强度参数必须是有限数字。")
    if start < 0 or duration <= 0 or not 0 < strength <= 1:
        raise ValueError("start 必须非负，duration 必须大于零，strength 必须大于零且不超过 1。")
    if profile not in PROFILES:
        raise ValueError("profile 必须是 natural 或 strong。")
    if not source.is_file():
        raise FileNotFoundError(f"输入视频不存在: {source}")
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖，请换用新的 --output: {output}")
    info = video_info(source)
    if start >= info["duration"]:
        raise ValueError("开始时间已超出原视频时长。")
    requested = duration
    duration = min(duration, info["duration"] - start)
    if duration < requested:
        logger.warning("样片时长超过剩余视频，将截取实际剩余的 %.3f 秒。", duration)
    source_record = file_record(source)
    source_mtime = source.stat().st_mtime_ns
    detector = FaceDetector()
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".portrait-", dir=output.parent) as temporary:
        work = Path(temporary)
        result_dir = work / "result"
        result_dir.mkdir()
        original_path = result_dir / "original.mp4"
        enhanced_path = result_dir / "retouched.mp4"
        comparison_path = result_dir / "comparison.mp4"
        run_ffmpeg(executable, [
            "-n", "-ss", str(start), "-i", str(source), "-t", str(duration),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", "fps=24,scale=trunc(min(720\\,iw)/2)*2:-2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(original_path),
        ])
        clip = video_info(original_path)
        capture = cv2.VideoCapture(str(original_path))
        writers = []
        rows = []
        best = None
        best_changed = 0
        try:
            for name, size in (
                ("retouched", (clip["width"], clip["height"])),
                ("comparison", (clip["width"] * 2, clip["height"] + 44)),
            ):
                writer = cv2.VideoWriter(
                    str(work / f"{name}.avi"), cv2.VideoWriter_fourcc(*"FFV1"), clip["fps"], size,
                )
                writers.append(writer)
                if not writer.isOpened():
                    raise PortraitError("无法创建本地无损中间视频，请检查 OpenCV FFV1 支持。")
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                faces = detector.detect(frame)
                enhanced, mask = retouch_frame(frame, faces, strength, profile)
                changed = np.any(enhanced != frame, axis=2)
                if np.any(changed & (mask == 0)):
                    raise PortraitError("检测到人脸遮罩之外的像素被处理，已停止导出。")
                count = int(np.count_nonzero(changed))
                delta = np.abs(enhanced.astype(np.float32) - frame.astype(np.float32))
                luma_delta = (
                    cv2.cvtColor(enhanced, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(np.float32)
                    - cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(np.float32)
                )
                comparison = comparison_frame(frame, enhanced, profile)
                writers[0].write(enhanced)
                writers[1].write(comparison)
                rows.append({
                    "frame": len(rows), "seconds": len(rows) / clip["fps"],
                    "faces": [{"box": face["box"], "eyes": face["eyes"]} for face in faces],
                    "edited_pixels": count,
                    "mean_absolute_channel_delta": float(delta[changed].mean()) if count else 0,
                    "mean_luma_delta": float(luma_delta[changed].mean()) if count else 0,
                })
                if count > best_changed:
                    best_changed, best = count, comparison.copy()
            if len(rows) != clip["frames"]:
                raise PortraitError("原片段未完整读取，停止导出。")
        finally:
            capture.release()
            for writer in writers:
                writer.release()
        modified_frames = sum(row["edited_pixels"] > 0 for row in rows)
        if not modified_frames or best is None:
            raise PortraitError("没有检测到可处理的正面人脸/肤色区域；未生成成功样片，请改选片段。")
        for name, destination in (("retouched", enhanced_path), ("comparison", comparison_path)):
            run_ffmpeg(executable, [
                "-n", "-i", str(work / f"{name}.avi"), "-i", str(original_path),
                "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264",
                "-preset", "fast", "-crf", "18", "-c:a", "copy",
                "-shortest", "-movflags", "+faststart", str(destination),
            ])
        image_path = result_dir / "comparison.jpg"
        ok, encoded = cv2.imencode(".jpg", best, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise PortraitError("无法编码预览图片。")
        image_path.write_bytes(encoded.tobytes())
        validation = {}
        for path in (original_path, enhanced_path, comparison_path):
            validation[path.name] = video_info(path, decode=True)
            if validation[path.name]["frames"] != len(rows):
                raise PortraitError(f"导出视频帧数不一致: {path.name}")
            run_ffmpeg(executable, ["-i", str(path), "-f", "null", "-"])
        if file_record(source) != source_record or source.stat().st_mtime_ns != source_mtime:
            raise PortraitError("处理期间输入文件发生变化，未提交输出。")
        report = {
            "schema_version": 1, "mode": "local_face_retouch_not_identity_replacement",
            "status": "needs_human_review", "uploaded": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tools": {"opencv": cv2.__version__, "numpy": np.__version__,
                      "imageio_ffmpeg": imageio_ffmpeg.__version__},
            "source": {**source_record, "mtime_ns": source_mtime},
            "parameters": {"start": start, "requested_duration": requested, "profile": profile,
                           "profile_settings": dict(PROFILES[profile]),
                           "actual_duration": clip["duration"], "strength": strength,
                           "output_fps": clip["fps"], "max_width": 720},
            "processing": {
                "detector": "OpenCV Haar face with eye-pair validation",
                "effect": (
                    "strong skin-local smoothing, highlight-aware luma lift and reduced skin chroma"
                    if profile == "strong" else "skin-local bilateral smoothing and small luma lift"
                ),
                "frames": len(rows), "edited_frames": modified_frames,
                "unchanged_frames": len(rows) - modified_frames,
                "pixels_outside_mask_edited_before_encoding": 0,
                "mean_absolute_channel_delta_on_edited_pixels": sum(
                    row["edited_pixels"] * row["mean_absolute_channel_delta"] for row in rows
                ) / sum(row["edited_pixels"] for row in rows),
                "mean_luma_delta_on_edited_pixels": sum(
                    row["edited_pixels"] * row["mean_luma_delta"] for row in rows
                ) / sum(row["edited_pixels"] for row in rows),
                "audio": "source clip audio retained; clip creation re-encodes available audio to AAC",
            },
            "limitations": [
                "Not AI generation, identity replacement, or a guarantee of appearance improvement.",
                "Frontal face/skin detection can miss faces or misclassify objects; review before use.",
                "Eyes and central mouth are excluded; no geometric or body changes.",
                "Mask protection is verified before encoding; lossy MP4 encoding can change other pixels.",
                "No temporal tracking or flicker correction in this first preview.",
                "No automatic upload or publication; rights and content still require human review.",
            ],
            "validation": validation, "frame_analysis": rows,
            "files": [file_record(path) for path in (
                original_path, enhanced_path, comparison_path, image_path,
            )],
        }
        write_json(result_dir / "report.json", report)
        result_dir.rename(output)
    logger.info("样片已生成，需人工审核。左右对比（左原片、右优化）: %s", output / "comparison.mp4")
    logger.info("实际处理 %s/%s 帧；完整报告: %s", modified_frames, len(rows), output / "report.json")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地原人物优化样片，支持自然/明显增强，不是 AI 换脸或换人。")
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, help="新的输出目录，已存在时拒绝覆盖")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--strength", type=float, default=.6)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="natural",
                        help="natural 保留旧版轻度效果；strong 明显增强磨皮、肤色与提亮")
    args = parser.parse_args(argv)
    source = args.video.resolve()
    output = args.output.resolve() if args.output else (
        source.parent / "edits" / datetime.now(timezone.utc).strftime("portrait-%Y%m%dT%H%M%S%fZ")
    )
    try:
        create_preview(source, output, args.start, args.duration, args.strength, args.profile)
    except (PortraitError, ValueError, OSError, cv2.error) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
