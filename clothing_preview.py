import argparse
import logging
import tempfile
from pathlib import Path

import cv2
import numpy as np

from head_inpaint import save_png, select_frame
from portrait_preview import PortraitError
from video_archive import file_record, write_json


logger = logging.getLogger(__name__)


def recolor_clothing(frame: np.ndarray, mask: np.ndarray, face: dict) -> tuple[np.ndarray, dict]:
    if mask.shape != frame.shape[:2] or mask.dtype != np.uint8 or not np.any(mask):
        raise ValueError("衣服遮罩必须是原帧同尺寸、非空的 8 位灰度图片。")
    x, y, w, h = face["box"]
    if np.any(mask[y:y + h, x:x + w]):
        raise ValueError("衣服遮罩覆盖人脸，拒绝改动原五官。")
    region = mask > 0
    distance = cv2.distanceTransform(region.astype(np.uint8), cv2.DIST_L2, 3)
    alpha = np.minimum(distance / 5, 1) * (mask.astype(np.float32) / 255)
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)
    # Adjust chroma only; retain the original fabric's lightness and texture.
    lab[:, :, 1] = 128 + (lab[:, :, 1] - 128) * .75
    lab[:, :, 2] = 128 + (lab[:, :, 2] - 128) * .60
    colored = cv2.cvtColor(np.clip(np.rint(lab), 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    blended = np.rint(frame * (1 - alpha[:, :, None]) + colored * alpha[:, :, None]).astype(np.uint8)
    result = frame.copy()
    result[region] = blended[region]
    changed = np.any(result != frame, axis=2)
    if not np.any(changed):
        raise PortraitError("衣服配色未产生有效变化，未导出成功结果。")
    metrics = {
        "changed_pixels": int(np.count_nonzero(changed)),
        "face_changed_pixels": int(np.count_nonzero(changed[y:y + h, x:x + w])),
        "outside_mask_changed_pixels": int(np.count_nonzero(changed & ~region)),
    }
    if metrics["face_changed_pixels"] or metrics["outside_mask_changed_pixels"]:
        raise PortraitError("原脸或衣服遮罩外发生改动，拒绝导出。")
    return result, metrics


def create_preview(source: Path, output: Path, mask_path: Path, seconds: float = .5) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"输入视频不存在: {source}")
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖: {output}")
    before = file_record(source), source.stat().st_mtime_ns
    frame, face, timestamp = select_frame(source, seconds)
    data = mask_path.read_bytes()
    mask = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if mask is None:
        raise ValueError("无法解码衣服遮罩。")
    if mask_path.suffix.lower() != ".png":
        raise ValueError("衣服遮罩必须是 PNG。")
    styled, metrics = recolor_clothing(frame, mask, face)
    if (file_record(source), source.stat().st_mtime_ns) != before:
        raise PortraitError("处理期间原视频发生变化，未导出。")
    pair = np.hstack((frame, styled))
    header = np.zeros((48, pair.shape[1], 3), dtype=np.uint8)
    for title, offset in (("ORIGINAL", 12), ("COLOR EDIT - ORIGINAL FACE", frame.shape[1] + 12)):
        cv2.putText(header, title, (offset, 31), cv2.FONT_HERSHEY_SIMPLEX, .60,
                    (255, 255, 255), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".clothing-preview-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        for name, picture in {
            "original.png": frame, "styled.png": styled, "mask.png": mask,
            "comparison.png": np.vstack((header, pair)),
        }.items():
            save_png(staging / name, picture)
        write_json(staging / "report.json", {
            "schema_version": 1, "status": "needs_human_review",
            "mode": "local_clothing_chroma_recolor", "generative_model_used": False,
            "color_brief": "soft muted original blue; no garment reconstruction",
            "uploaded": False, "video_generated": False, "auto_publication": False,
            "source": {**before[0], "mtime_ns": before[1], "frame_seconds": timestamp},
            "clothing_mask": file_record(mask_path), "face": face,
            "geometry_changed": False, "hair_styling_applied": False, "makeup_applied": False,
            "metrics": metrics,
            "limitations": [
                "Color adjustment only; no new garment, hairstyle, makeup, or identity.",
                "The clothing mask is manually reviewed for this frame, not automatic detection.",
                "Products, hands and text outside the mask are unchanged; mask coverage needs review.",
                "No video tracking, aesthetic approval, or publication clearance.",
            ],
            "files": [file_record(staging / name) for name in (
                "original.png", "styled.png", "mask.png", "comparison.png",
            )],
        })
        staging.rename(output)
    logger.info("仅调整原衣服配色，未重绘人脸，需人工审美审核: %s", output / "comparison.png")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="保留原脸和衣料纹理的本地单帧配色预览，不是换装生成。")
    parser.add_argument("video", type=Path)
    parser.add_argument("--clothing-mask", type=Path, required=True, help="原帧同尺寸 8 位灰度 PNG")
    parser.add_argument("--output", type=Path, required=True, help="新的输出目录")
    parser.add_argument("--seconds", type=float, default=.5)
    args = parser.parse_args(argv)
    try:
        create_preview(args.video.resolve(), args.output.resolve(), args.clothing_mask.resolve(), args.seconds)
    except (PortraitError, ValueError, OSError, cv2.error) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
