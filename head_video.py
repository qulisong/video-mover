import argparse
import gc
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np

from head_inpaint import composite_head, offline_environment, save_png, validate_model
from klein_edit import (
    MODEL_DIR, MODEL_FILES, MODEL_ID, MODEL_REVISION, SAFETY_MODEL_DIR,
    check_safety, load_pipeline, read_mask,
)
from portrait_preview import PortraitError, run_ffmpeg, video_info
from video_archive import file_record, write_json


logger = logging.getLogger(__name__)
PROMPT = (
    "Image 1 is the target video frame. Image 2 is the approved adult Chinese woman. "
    "Replace ONLY the head, face and hairstyle in image 1 with the SAME woman as image 2. "
    "Keep her recognizable eyes, eyebrows, nose, lips, facial proportions and long straight "
    "dark chestnut hair. No glasses, sunglasses, earrings or added accessories. "
    "Crucially match image 1's EXACT head direction, camera perspective, expression, "
    "mouth opening and lighting. If image 1 shows a left-facing side profile, render a "
    "left-facing SIDE PROFILE, not a frontal face. Preserve the blue-white knitted cardigan, "
    "white inner top, neck, body, hands, detergent bottle, printed labels, overlays and "
    "kitchen background exactly. Do not change clothing. Natural photograph."
)


def load_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise PortraitError(f"无法读取图片: {path}")
    return image


def verify_record(path: Path, record: dict) -> None:
    if not path.is_file() or file_record(path) != record:
        raise PortraitError(f"文件完整性检查失败: {path}")


def validate_plan(plan: dict, frames: int, shape: tuple[int, int]) -> None:
    if plan.get("rights_confirmed") is not True or plan.get("masks_manually_reviewed") is not True:
        raise ValueError("必须确认素材授权并人工审核逐镜头头部遮罩。")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("镜头计划必须非空并完整覆盖视频。")
    cursor = 0
    for shot in shots:
        start, stop = shot.get("start"), shot.get("stop")
        if (
            type(start) is not int or type(stop) is not int
            or start != cursor or not start < stop <= frames
        ):
            raise ValueError("镜头范围必须连续、无重叠且完整覆盖视频（stop 不包含）。")
        keys = shot.get("keys")
        if "replace_with_start" in shot:
            replacement = shot["replace_with_start"]
            if (
                plan.get("shot_replacement_confirmed") is not True
                or type(replacement) is not int or replacement < 0
                or replacement + stop - start > frames
                or keys or shot.get("preserve") is True
            ):
                raise ValueError("替换镜头必须另外确认授权、来源帧范围有效，且不能同时保留或生成头部。")
        elif shot.get("preserve") is True:
            if keys:
                raise ValueError("保留原样的镜头不能同时提供生成关键帧。")
        else:
            if not isinstance(keys, list) or not keys:
                raise ValueError("人物镜头必须有关键帧，不能静默跳过。")
            indices = [key.get("frame") for key in keys]
            if any(type(i) is not int or not start <= i < stop for i in indices):
                raise ValueError("关键帧必须位于对应镜头范围内。")
            if indices != sorted(set(indices)):
                raise ValueError("关键帧必须按帧号递增且不能重复。")
            for key in keys:
                if "reuse" in key:
                    continue
                polygon = np.asarray(key.get("polygon", []))
                if (
                    polygon.ndim != 2 or polygon.shape[1:] != (2,) or len(polygon) < 3
                    or not np.issubdtype(polygon.dtype, np.integer)
                    or np.any(polygon < 0) or np.any(polygon[:, 0] >= shape[1])
                    or np.any(polygon[:, 1] >= shape[0])
                ):
                    raise ValueError("头部多边形必须为原帧内至少三个整数坐标点。")
        cursor = stop
    if cursor != frames:
        raise ValueError("镜头计划没有覆盖完整视频，拒绝截短。")


def backward_map(previous: np.ndarray, current: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    height, width = current.shape[:2]
    size = (width // 2, height // 2)
    a, b = [
        cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), size)
        for frame in (previous, current)
    ]
    estimator = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    back = estimator.calc(b, a, None)
    forward = estimator.calc(a, b, None)
    grid_y, grid_x = np.mgrid[:size[1], :size[0]].astype(np.float32)
    reverse = cv2.remap(
        forward, grid_x + back[:, :, 0], grid_y + back[:, :, 1],
        cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
    )
    valid = np.linalg.norm(back + reverse, axis=2) <= 3
    flow = cv2.resize(back, (width, height)) * 2
    if not np.all(np.isfinite(flow)):
        raise PortraitError("光流坐标不是有限值，停止导出。")
    yy, xx = np.mgrid[:height, :width].astype(np.float32)
    mapping = np.dstack((xx, yy)) + flow
    valid = cv2.resize(valid.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
    return mapping, valid > 0


def transport(
    frames: list[np.ndarray], index: int, start: int, stop: int,
    texture: np.ndarray, mask: np.ndarray,
):
    height, width = mask.shape
    yy, xx = np.mgrid[:height, :width].astype(np.float32)
    identity = np.dstack((xx, yy))
    yield index, texture, mask, 1.0
    anchor_area = np.count_nonzero(mask)
    for direction, end in ((-1, start - 1), (1, stop)):
        coordinates = identity.copy()
        for current in range(index + direction, end, direction):
            previous = current - direction
            step, valid = backward_map(frames[previous], frames[current])
            coordinates = cv2.remap(
                coordinates, step[:, :, 0], step[:, :, 1], cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(-10000, -10000),
            )
            warped_mask = cv2.remap(
                mask, coordinates[:, :, 0], coordinates[:, :, 1], cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT,
            )
            active = warped_mask > 0
            area = np.count_nonzero(active)
            reliable = float(np.mean(valid[active])) if area else 0.0
            if not .35 * anchor_area <= area <= 2.8 * anchor_area or reliable < .5:
                raise PortraitError(
                    f"第 {current} 帧头部跟踪失败（面积 {area}/{anchor_area}，"
                    f"光流一致率 {reliable:.3f}）；需要补充关键帧，不会静默保留原脸。"
                )
            warped = cv2.remap(
                texture, coordinates[:, :, 0], coordinates[:, :, 1], cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
            )
            yield current, warped, warped_mask, reliable


def temporal_weight(index: int, keys: list[int], key: int) -> float:
    position = keys.index(key)
    if index < key:
        return 1.0 if position == 0 else float(np.clip(
            (index - keys[position - 1]) / (key - keys[position - 1]), 0, 1,
        ))
    return 1.0 if position == len(keys) - 1 else float(np.clip(
        (keys[position + 1] - index) / (keys[position + 1] - key), 0, 1,
    ))


def blend(frame: np.ndarray, color: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    if (
        color.shape != frame.shape or alpha.shape != frame.shape[:2]
        or not np.all(np.isfinite(color)) or not np.all(np.isfinite(alpha))
        or np.any(alpha < 0) or np.any(alpha > 1.000001)
    ):
        raise ValueError("合成颜色和透明度必须尺寸匹配、数值有限且透明度介于 0 和 1。")
    alpha = np.minimum(alpha, 1)
    result = np.rint(
        frame.astype(np.float32) * (1 - alpha[:, :, None]) + color
    ).clip(0, 255).astype(np.uint8)
    if np.any(np.any(result != frame, axis=2) & (alpha == 0)):
        raise PortraitError("头部遮罩之外发生改动，停止导出。")
    return result


def generate_keys(
    source: Path, frames: list[np.ndarray], plan_path: Path, plan: dict, cache: Path, threads: int,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    source_record, plan_record = file_record(source), file_record(plan_path)
    approved = Path(plan["approved_still"])
    approved_report = json.loads((approved / "report.json").read_text(encoding="utf-8"))
    if (
        approved_report.get("rights_confirmed") is not True
        or approved_report.get("safety_check_passed") is not True
        or any(approved_report["source"][key] != source_record[key] for key in source_record)
    ):
        raise PortraitError("已选样片授权、安全检查或源视频记录不匹配。")
    for record in approved_report["files"]:
        verify_record(approved / record["file"], record)
    reference = approved_report["identity_reference"]
    verify_record(Path(reference["path"]), reference["record"])
    approved_record = file_record(approved / "report.json")
    crop = plan.get("identity_crop")
    approved_image = load_image(approved / "generated.png")
    if (
        not isinstance(crop, list) or len(crop) != 4
        or any(type(value) is not int for value in crop)
    ):
        raise ValueError("计划必须提供人工审核的身份裁剪 [x,y,width,height]。")
    x, y, width, height = crop
    if min(x, y) < 0 or min(width, height) < 32 or x + width > approved_image.shape[1] or y + height > approved_image.shape[0]:
        raise ValueError("身份裁剪超出已审核样片范围或尺寸不足。")
    reference_image = approved_image[y:y + height, x:x + width]
    offline_environment()
    pipeline = None
    outputs = {}
    try:
        for shot in plan["shots"]:
            for key in shot.get("keys", []):
                index = key["frame"]
                original = frames[index]
                if "reuse" in key:
                    previous = Path(key["reuse"])
                    if previous != approved or index != round(approved_report["source"]["frame_seconds"] * plan["fps"]):
                        raise PortraitError("复用样片的关键帧或目录与审核记录不一致。")
                    if not np.array_equal(load_image(approved / "original.png"), original):
                        raise PortraitError("复用样片与解码视频关键帧不一致。")
                    outputs[index] = (
                        load_image(approved / "generated.png"),
                        read_mask(approved / "mask.png", original.shape[:2]),
                    )
                    continue
                destination = cache / str(index)
                if destination.exists():
                    report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
                    if (
                        report.get("source") != source_record or report.get("plan") != plan_record
                        or report.get("approved_report") != approved_record
                        or report.get("safety_check_passed") is not True
                    ):
                        raise PortraitError(f"缓存来源或安全检查不匹配: {destination}")
                    for record in report["files"]:
                        verify_record(destination / record["file"], record)
                    outputs[index] = (
                        load_image(destination / "generated.png"),
                        read_mask(destination / "mask.png", original.shape[:2]),
                    )
                    logger.info("复用已校验关键帧 %s。", index)
                    continue
                if pipeline is None:
                    import psutil
                    if psutil.virtual_memory().available < 36 * 1024 ** 3:
                        raise PortraitError("本地 CPU 生成要求至少 36 GiB 当前可用内存。")
                    validate_model(MODEL_DIR, MODEL_ID, MODEL_REVISION, MODEL_FILES)
                    validate_model(SAFETY_MODEL_DIR)
                    pipeline = load_pipeline(MODEL_DIR, threads)
                import torch
                from PIL import Image
                logger.info("本地生成姿态匹配关键帧 %s；不修改衣服。", index)
                mask = np.zeros(original.shape[:2], np.uint8)
                cv2.fillPoly(mask, [np.asarray(key["polygon"], np.int32)], 255)
                bx, by, bw, bh = cv2.boundingRect(cv2.findNonZero(mask))
                left, top = max(0, bx - 32), max(0, by - 32)
                right = min(original.shape[1], bx + bw + 32)
                bottom = min(original.shape[0], by + bh + 32)
                crop = (left, top, right - left, bottom - top)
                scale = 768 / max(crop[2:])
                resolution = tuple(max(256, round(side * scale / 16) * 16) for side in crop[2:])
                image = Image.fromarray(cv2.cvtColor(
                    original[top:bottom, left:right], cv2.COLOR_BGR2RGB,
                )).resize(resolution)
                identity = Image.fromarray(cv2.cvtColor(reference_image, cv2.COLOR_BGR2RGB))
                with torch.inference_mode():
                    prediction = pipeline(
                        image=[image, identity], prompt=PROMPT, width=resolution[0], height=resolution[1],
                        num_inference_steps=4, guidance_scale=1.0, max_sequence_length=512,
                        generator=torch.Generator(device="cpu").manual_seed(23),
                    )
                raw = cv2.cvtColor(np.asarray(prediction.images[0].convert("RGB")), cv2.COLOR_RGB2BGR)
                del prediction
                generated, metrics = composite_head(
                    original, raw, mask, crop,
                    protection=cv2.bitwise_not(mask),
                )
                check_safety(generated, SAFETY_MODEL_DIR)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".head-key-", dir=cache) as temporary:
                    staging = Path(temporary) / "key"
                    staging.mkdir()
                    for name, picture in (
                        ("original.png", original), ("generated.png", generated),
                        ("mask.png", mask), ("model_output.png", raw),
                    ):
                        save_png(staging / name, picture)
                    write_json(staging / "report.json", {
                        "source": source_record, "plan": plan_record,
                        "approved_report": approved_record, "frame": index,
                        "prompt": PROMPT, "safety_check_passed": True, "metrics": metrics,
                        "parameters": {"generation_crop": crop, "resolution": resolution,
                                       "steps": 4, "seed": 23, "threads": threads,
                                       "device": "cpu", "dtype": "float32"},
                        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
                        "files": [file_record(staging / name) for name in (
                            "original.png", "generated.png", "mask.png", "model_output.png",
                        )],
                    })
                    staging.rename(destination)
                outputs[index] = generated, mask
    finally:
        del pipeline
        gc.collect()
    verify_record(source, source_record)
    verify_record(plan_path, plan_record)
    verify_record(approved / "report.json", approved_record)
    for record in approved_report["files"]:
        verify_record(approved / record["file"], record)
    verify_record(Path(reference["path"]), reference["record"])
    return outputs


def audio_hash(executable: str, path: Path) -> str | None:
    process = subprocess.run(
        [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "hash", "-"],
        capture_output=True, text=True, check=False,
    )
    if process.returncode:
        if "matches no streams" in process.stderr:
            logger.info("视频没有音轨: %s", path)
            return None
        raise PortraitError(f"音频校验失败: {process.stderr[-2000:]}")
    return process.stdout.strip()


def align_keys(
    frames: list[np.ndarray], keys: dict, cache: Path, approved: Path, path: Path,
) -> tuple[dict, dict]:
    specification = json.loads(path.read_text(encoding="utf-8"))
    if specification.get("landmarks_manually_reviewed") is not True:
        raise ValueError("头部对齐需要人工审核对应点与遮罩。")
    result, metrics = dict(keys), {}
    for item in specification["keys"]:
        index = item["frame"]
        if index not in keys:
            raise ValueError("对齐帧必须是已有关键帧。")
        original = frames[index]
        if item["asset"] == "approved":
            canvas = load_image(approved / "generated.png")
        elif item["asset"] == "generated":
            directory = cache / str(index)
            record = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            left, top, width, height = record["parameters"]["generation_crop"]
            canvas = original.copy()
            canvas[top:top + height, left:left + width] = cv2.resize(
                load_image(directory / "model_output.png"), (width, height),
                interpolation=cv2.INTER_LANCZOS4,
            )
        else:
            raise ValueError("对齐素材只能是已审核正脸或该帧的已校验生成结果。")
        source = np.asarray(item["source_points"], np.float32)
        target = np.asarray(item["target_points"], np.float32)
        if (
            source.ndim != 2 or source.shape[1:] != (2,) or len(source) < 2
            or target.shape != source.shape
            or not np.all(np.isfinite(source)) or not np.all(np.isfinite(target))
            or np.any(source < 0) or np.any(target < 0)
            or np.any(source[:, 0] >= canvas.shape[1]) or np.any(source[:, 1] >= canvas.shape[0])
            or np.any(target[:, 0] >= original.shape[1]) or np.any(target[:, 1] >= original.shape[0])
        ):
            raise ValueError("对齐对应点必须为图片内至少两组有效坐标。")
        matrix, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.LMEDS)
        if matrix is None or inliers is None or not np.all(np.isfinite(matrix)):
            raise PortraitError("头部相似变换估计失败。")
        scale = float(np.linalg.norm(matrix[:, 0]))
        error = float(np.mean(np.linalg.norm(
            cv2.transform(source[None], matrix)[0] - target, axis=1,
        )))
        if not .4 <= scale <= 2.5 or error > 15:
            raise PortraitError("头部对齐缩放或对应点残差超出范围。")
        mask = keys[index][1].copy()
        foreground = None
        for field in ("polygon", "foreground_polygon"):
            if field not in item:
                continue
            polygon = np.asarray(item[field])
            if (
                polygon.ndim != 2 or polygon.shape[1:] != (2,) or len(polygon) < 3
                or not np.issubdtype(polygon.dtype, np.integer) or np.any(polygon < 0)
                or np.any(polygon[:, 0] >= original.shape[1]) or np.any(polygon[:, 1] >= original.shape[0])
            ):
                raise ValueError("对齐头部遮罩必须为原帧内的整数多边形。")
            region = np.zeros_like(mask)
            cv2.fillPoly(region, [polygon.astype(np.int32)], 255)
            if field == "polygon":
                mask = region
            else:
                foreground = region
        aligned = cv2.warpAffine(
            canvas, matrix, (original.shape[1], original.shape[0]),
            flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT_101,
        )
        if foreground is not None:
            background = ((mask > 0) & (foreground == 0)).astype(np.uint8) * 255
            background[int(np.min(target[:, 1])):] = 0
            if np.any(background):
                filled = cv2.inpaint(original, background, 7, cv2.INPAINT_TELEA)
                aligned[background > 0] = filled[background > 0]
        result[index] = composite_head(
            original, aligned, mask, (0, 0, original.shape[1], original.shape[0]),
            protection=cv2.bitwise_not(mask),
        )[0], mask
        metrics[str(index)] = {
            "asset": item["asset"], "scale": scale, "mean_landmark_error_pixels": error,
            "matrix": matrix.tolist(),
            "old_hair_background_inpaint": foreground is not None,
        }
    return result, metrics


def safety_check_frames(frames: list[np.ndarray], indices: list[int] | None = None) -> None:
    import torch
    from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
    from PIL import Image
    from transformers import CLIPImageProcessor

    validate_model(SAFETY_MODEL_DIR)
    offline_environment()
    checker = StableDiffusionSafetyChecker.from_pretrained(
        str(SAFETY_MODEL_DIR / "safety_checker"), variant="fp16",
        torch_dtype=torch.float32, use_safetensors=True, local_files_only=True,
    ).to("cpu").eval()
    processor = CLIPImageProcessor.from_pretrained(
        str(SAFETY_MODEL_DIR / "feature_extractor"), local_files_only=True,
    )
    for start in range(0, len(frames), 8):
        images = [Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) for frame in frames[start:start + 8]]
        inputs = processor(images=images, return_tensors="pt")
        pixels = np.stack([
            cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            for frame in frames[start:start + 8]
        ]).astype(np.float32) / 255
        with torch.inference_mode():
            _, flagged = checker(
                images=pixels, clip_input=inputs.pixel_values,
            )
        if flagged is None or len(flagged) != len(images):
            raise PortraitError("本地安全检查返回结果缺失，停止导出。")
        if any(flagged):
            blocked = [(indices[i] if indices is not None else i)
                       for i in range(start, start + len(images)) if flagged[i - start]]
            raise PortraitError(f"这些输出帧未通过安全检查，停止导出: {blocked}")


def render_video(
    source: Path, plan_path: Path, output: Path, cache: Path, threads: int = 8,
    alignment_path: Path | None = None, reviewed_source_shots: bool = False,
) -> Path:
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖: {output}")
    if not 1 <= threads <= 16:
        raise ValueError("threads 必须为 1..16。")
    cv2.setNumThreads(threads)
    source_record, source_mtime = file_record(source), source.stat().st_mtime_ns
    plan_record = file_record(plan_path)
    info = video_info(source)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("source_sha256") != source_record["sha256"] or plan.get("fps") != info["fps"]:
        raise PortraitError("计划与原视频哈希或帧率不匹配。")
    validate_plan(plan, info["frames"], (info["height"], info["width"]))
    frames = []
    capture = cv2.VideoCapture(str(source))
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    if len(frames) != info["frames"] or any(frame.shape[:2] != (info["height"], info["width"]) for frame in frames):
        raise PortraitError("原视频没有完整解码，停止导出。")
    keys = generate_keys(source, frames, plan_path, plan, cache, threads)
    alignment_record, alignment_metrics, pose_switches = None, {}, {}
    if alignment_path is not None:
        alignment_record = file_record(alignment_path)
        keys, alignment_metrics = align_keys(
            frames, keys, cache, Path(plan["approved_still"]), alignment_path,
        )
        pose_switches = json.loads(alignment_path.read_text(encoding="utf-8")).get("pose_switch_frames", {})
    rendered, rows = [None] * len(frames), [None] * len(frames)
    for shot in plan["shots"]:
        start, stop = shot["start"], shot["stop"]
        logger.info("处理镜头 [%s, %s)，保留原样=%s。", start, stop, shot.get("preserve", False))
        if "replace_with_start" in shot:
            for i in range(start, stop):
                replacement = shot["replace_with_start"] + i - start
                rendered[i] = frames[replacement]
                rows[i] = {
                    "frame": i, "preserved": False, "replacement_source_frame": replacement,
                    "edited_pixels": int(np.count_nonzero(np.any(frames[i] != frames[replacement], axis=2))),
                }
            continue
        if shot.get("preserve"):
            for i in range(start, stop):
                rendered[i] = frames[i]
                rows[i] = {"frame": i, "preserved": True, "edited_pixels": 0}
            continue
        indices = [key["frame"] for key in shot["keys"]]
        switches = pose_switches.get(str(start))
        if switches is not None and (
            not isinstance(switches, list) or len(switches) != len(indices) - 1
            or any(type(value) is not int or not start < value < stop for value in switches)
            or switches != sorted(set(switches))
            or any(not left <= key < right for key, left, right in zip(
                indices, [start, *switches], [*switches, stop],
            ))
        ):
            raise ValueError("姿态切换点必须递增、位于镜头内，并使每段包含自己的关键帧。")
        colors, alphas, reliability = {}, {}, {}
        for index in indices:
            position = indices.index(index)
            local_start = start if position == 0 else indices[position - 1] + 1
            local_stop = stop if position == len(indices) - 1 else indices[position + 1]
            if switches is not None:
                local_start = [start, *switches][position]
                local_stop = [*switches, stop][position]
            for i, texture, mask, confidence in transport(
                frames, index, local_start, local_stop, *keys[index],
            ):
                weight = 1.0 if switches is not None else temporal_weight(i, indices, index)
                if weight == 0:
                    continue
                alpha = np.minimum(cv2.distanceTransform(
                    (mask > 0).astype(np.uint8), cv2.DIST_L2, 3,
                ) / 4.32, 1) * weight
                if i not in colors:
                    colors[i] = np.zeros_like(texture, np.float32)
                    alphas[i] = np.zeros_like(alpha)
                    reliability[i] = []
                colors[i] += texture.astype(np.float32) * alpha[:, :, None]
                alphas[i] += alpha
                reliability[i].append(confidence)
        for i in range(start, stop):
            if i not in colors or not np.any(alphas[i] > .5):
                raise PortraitError(f"第 {i} 帧没有有效头部替换，停止导出。")
            rendered[i] = blend(frames[i], colors[i], alphas[i])
            changed = np.any(rendered[i] != frames[i], axis=2)
            if np.count_nonzero(changed) < 1000:
                raise PortraitError(f"第 {i} 帧人物修改不足，停止导出。")
            rows[i] = {
                "frame": i, "preserved": False,
                "head_edited": True,
                "edited_pixels": int(np.count_nonzero(changed)),
                "outside_propagated_mask_changed_pixels": 0,
                "flow_consistency_min": min(reliability[i]),
            }
    if any(frame is None for frame in rendered):
        raise PortraitError("未处理完整视频，停止导出。")
    checked_indices = [
        i for i, row in enumerate(rows) if not reviewed_source_shots or row.get("head_edited", False)
    ]
    logger.info("自动检查 %s 帧；人工审核的原片帧 %s。", len(checked_indices), len(rendered) - len(checked_indices))
    if checked_indices:
        safety_check_frames([rendered[i] for i in checked_indices], checked_indices)
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source_audio = audio_hash(executable, source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".head-video-", dir=output.parent) as temporary:
        work = Path(temporary)
        staging = work / "result"
        staging.mkdir()
        intermediate = work / "rendered.avi"
        writer = cv2.VideoWriter(
            str(intermediate), cv2.VideoWriter_fourcc(*"FFV1"), info["fps"],
            (info["width"], info["height"]),
        )
        if not writer.isOpened():
            writer.release()
            raise PortraitError("无法创建本地无损中间视频。")
        try:
            for frame in rendered:
                writer.write(frame)
        finally:
            writer.release()
        if video_info(intermediate, decode=True)["frames"] != len(frames):
            raise PortraitError("中间视频帧数不一致。")
        video = staging / "head-only.mp4"
        run_ffmpeg(executable, [
            "-n", "-i", str(intermediate), "-i", str(source),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(video),
        ])
        validation = video_info(video, decode=True)
        if any(validation[key] != info[key] for key in ("frames", "width", "height", "fps")):
            raise PortraitError("成片帧数、尺寸或帧率与原片不一致。")
        if audio_hash(executable, video) != source_audio:
            raise PortraitError("原音频没有完整保留，停止导出。")
        run_ffmpeg(executable, ["-i", str(video), "-f", "null", "-"])
        tiles = []
        for i in sorted(set([0, 12, 40, 60, 120, 155, 180, 203, 210, 220, 230, 244])):
            if i >= len(frames):
                continue
            pair = cv2.resize(np.hstack((frames[i], rendered[i])), (432, 384))
            cv2.putText(pair, f"{i}: ORIGINAL | EXPERIMENT", (5, 20), 0, .45, (0, 0, 255), 1)
            tiles.append(pair)
        while len(tiles) % 3:
            tiles.append(np.zeros_like(tiles[0]))
        save_png(staging / "contact-sheet.png", np.vstack([
            np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)
        ]))
        verify_record(source, source_record)
        verify_record(plan_path, plan_record)
        if alignment_path is not None:
            verify_record(alignment_path, alignment_record)
        if source.stat().st_mtime_ns != source_mtime:
            raise PortraitError("处理期间源视频时间戳发生变化，停止导出。")
        keyframe_files = []
        for index in alignment_metrics:
            picture = staging / f"keyframe-{index}.png"
            save_png(picture, keys[int(index)][0])
            keyframe_files.append(file_record(picture))
        write_json(staging / "report.json", {
            "schema_version": 1, "status": "experimental_needs_human_review",
            "mode": "local_keyframe_head_edit_with_dense_optical_flow",
            "rights_confirmed": True, "uploaded": False, "auto_publication": False,
            "source": source_record, "plan": plan_record,
            "model": {"id": MODEL_ID, "revision": MODEL_REVISION, "license": "apache-2.0"},
            "safety_checker_license": "creativeml-openrail-m",
            "safety_review": {
                "automated_scope": "ai_edited_frames" if reviewed_source_shots else "all_frames",
                "automated_checked_frames": len(checked_indices),
                "automated_check_passed": True,
                "source_frames_manually_reviewed": len(rendered) - len(checked_indices),
                "manual_review_opt_in": reviewed_source_shots,
            },
            "alignment": {"record": alignment_record, "metrics": alignment_metrics,
                          "pose_switch_frames": pose_switches},
            "validation": validation, "audio_packet_hash": source_audio,
            "processing": {
                "frames": len(frames), "edited_frames": sum(not row["preserved"] for row in rows),
                "head_edited_frames": sum(row.get("head_edited", False) for row in rows),
                "replaced_frames": sum("replacement_source_frame" in row for row in rows),
                "preserved_frames": sum(row["preserved"] for row in rows),
                "head_edit_frames_outside_propagated_masks_changed_pixels_before_encoding": 0,
                "keyframes": sorted(keys), "frame_analysis": rows,
            },
            "limitations": [
                "Experimental optical-flow texture animation, not a trained video identity or lip-sync model.",
                "Reference identity, blink/mouth animation, profile-to-front consistency and aesthetics are not verified.",
                "Manual frame-specific head masks are propagated; automatic semantic garment/hand/occlusion protection is not established.",
                "No edits outside propagated masks before encoding; lossy MP4 encoding may change other pixels.",
                "Explicitly approved shot replacements change whole frames; they are separately recorded, not head-only edits.",
                "No automatic upload/publication; human review is required.",
            ],
            "files": [file_record(video), file_record(staging / "contact-sheet.png"), *keyframe_files],
        })
        staging.rename(output)
    logger.info("完整实验视频已生成，需要人工审核: %s", output / "head-only.mp4")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地逐镜头头部替换实验；不是已验证的身份保真或口型同步。")
    parser.add_argument("video", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keyframes-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--alignment", type=Path)
    parser.add_argument(
        "--reviewed-source-shots", action="store_true",
        help="明确确认原片商品镜头已人工审核；仅 AI 改动帧自动检查，默认仍检查全部帧",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        render_video(
            args.video, args.plan, args.output, args.keyframes_dir, args.threads,
            args.alignment, args.reviewed_source_shots,
        )
    except (PortraitError, ValueError, OSError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
