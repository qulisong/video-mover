import argparse
import logging
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from portrait_preview import FaceDetector, PortraitError, video_info
from person_regions import SEGMENTER_DIR, person_region, prepare_segmenter, protect_regions, protected_box
from video_archive import file_record, write_json


logger = logging.getLogger(__name__)
MODEL_ID = "stable-diffusion-v1-5/stable-diffusion-inpainting"
MODEL_REVISION = "8a4288a76071f7280aedbdb3253bdb9e9d5d84bb"
MODEL_DIR = Path(__file__).resolve().parent / ".models" / "sd15-inpainting"
MODEL_FILES = (
    "model_index.json", "README.md",
    "feature_extractor/preprocessor_config.json",
    "safety_checker/config.json", "safety_checker/model.fp16.safetensors",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
    "tokenizer/merges.txt", "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json", "tokenizer/vocab.json",
    "unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors",
)
DEFAULT_PROMPT = (
    "A realistic photograph of a fictional adult Chinese woman, East Asian facial features, "
    "a distinctly different individual face, natural everyday appearance, "
    "long black hair with soft bangs, delicate thin wire-rim eyeglasses, "
    "subtle makeup, realistic skin texture, natural indoor daylight, "
    "matching the original head pose, candid portrait photograph, detailed face"
)
PERSON_PROMPT = (
    "Photorealistic fictional adult Chinese woman, natural East Asian face, "
    "black shoulder-length hair, light makeup, friendly smile, realistic skin, "
    "simple sage green long-sleeved crewneck sweater, opaque modest clothing, "
    "natural proportions, standing in a kitchen holding the original bottle, "
    "same pose and lighting, realistic hands, candid photo"
)
NEGATIVE_PROMPT = (
    "cartoon, anime, illustration, doll, plastic skin, blurry, low quality, "
    "deformed face, distorted eyes, duplicate face, extra head, text, watermark, "
    "thick eyeglass frames, child, nude, nudity, revealing clothing, lingerie, "
    "extra fingers, extra limbs, malformed hands, duplicate person"
)


def offline_environment() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def prepare_model(
    directory: Path, model_id: str = MODEL_ID, revision: str = MODEL_REVISION,
    files: tuple[str, ...] = MODEL_FILES, license_name: str = "creativeml-openrail-m",
) -> dict:
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    from huggingface_hub import snapshot_download

    logger.info("仅下载公开模型文件，不读取或上传用户视频。模型: %s，许可: %s。", model_id, license_name)
    snapshot_download(
        repo_id=model_id, revision=revision, local_dir=directory,
        allow_patterns=list(files), max_workers=2,
    )
    missing = [name for name in files if not (directory / name).is_file()]
    if missing:
        raise PortraitError(f"模型文件未下载完整: {', '.join(missing)}")
    manifest = {
        "model": model_id, "revision": revision,
        "license": license_name,
        "purpose": "local research prototype; not cleared for automatic publication",
        "files": [
            {**file_record(directory / name), "file": name} for name in files
        ],
    }
    write_json(directory / "model_source.json", manifest)
    logger.info("模型准备完成，后续 render 只读本地文件: %s", directory)
    return manifest


def validate_model(
    directory: Path, model_id: str = MODEL_ID, revision: str = MODEL_REVISION,
    files: tuple[str, ...] = MODEL_FILES,
) -> dict:
    import json

    path = directory / "model_source.json"
    if not path.is_file():
        raise PortraitError("模型尚未准备，请先运行 head_inpaint.py prepare-model。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or (
        manifest.get("model") != model_id or manifest.get("revision") != revision
    ):
        raise PortraitError("模型来源或版本与本实验不一致，请重新准备模型。")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(files):
        raise PortraitError("模型文件清单不完整，请重新准备模型。")
    if not all(isinstance(entry, dict) and isinstance(entry.get("file"), str) for entry in records):
        raise PortraitError("模型文件清单格式无效，请重新准备模型。")
    by_name = {entry["file"]: entry for entry in records}
    for name in files:
        file = directory / name
        if not file.is_file() or name not in by_name:
            raise PortraitError(f"本地模型文件缺失: {name}")
        actual = {**file_record(file), "file": name}
        if actual != by_name[name]:
            raise PortraitError(f"本地模型文件校验失败: {name}")
    return manifest


def select_frame(source: Path, seconds: float | None = None) -> tuple[np.ndarray, dict, float]:
    info = video_info(source)
    if seconds is not None and (
        not math.isfinite(seconds) or not 0 <= seconds < info["duration"]
    ):
        raise ValueError("seconds 必须位于视频时长范围内。")
    detector = FaceDetector()
    capture = cv2.VideoCapture(str(source))
    best = None
    try:
        times = [seconds] if seconds is not None else np.linspace(
            0, min(info["duration"] - 1 / info["fps"], 15), 24,
        )
        for timestamp in times:
            index = min(info["frames"] - 1, int(float(timestamp) * info["fps"]))
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok:
                raise PortraitError("无法读取选定的视频帧。")
            faces = detector.detect(frame)
            if len(faces) != 1:
                continue
            face = faces[0]
            area = face["box"][2] * face["box"][3]
            if best is None or area > best[0]:
                best = (area, frame.copy(), face, index / info["fps"])
        if best is None:
            raise PortraitError("候选帧中未找到可靠的单人正面人脸，请用 --seconds 选择其他时间。")
        return best[1], best[2], best[3]
    finally:
        capture.release()


def head_region(frame: np.ndarray, face: dict) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    x, y, w, h = face["box"]
    height, width = frame.shape[:2]
    size = min(width, height, int(math.ceil(max(w, h) * 2)))
    if size < 64:
        raise PortraitError("人脸区域过小，无法进行有效的头部重绘。")
    center = (x + w // 2, y + int(h * .25))
    left = min(max(0, center[0] - size // 2), width - size)
    top = min(max(0, center[1] - size // 2), height - size)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(mask, center, (int(w * .60), int(h * .86)), 0, 0, 360, 255, -1)
    limited = np.zeros_like(mask)
    limited[top:top + size, left:left + size] = mask[top:top + size, left:left + size]
    return limited, (left, top, size, size)


def composite_head(
    original: np.ndarray, generated_crop: np.ndarray, mask: np.ndarray,
    crop: tuple[int, int, int, int],
    protection: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    if mask.shape != original.shape[:2] or not np.any(mask):
        raise ValueError("编辑遮罩必须与原图尺寸一致且非空。")
    x, y, w, h = crop
    generated = cv2.resize(generated_crop, (w, h), interpolation=cv2.INTER_LANCZOS4)
    crop_mask = mask[y:y + h, x:x + w]
    distance = cv2.distanceTransform((crop_mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
    alpha = np.minimum(distance / max(3, w * .025), 1)[:, :, None]
    if protection is not None:
        if protection.shape != mask.shape:
            raise ValueError("保护遮罩必须与编辑遮罩尺寸一致。")
        protected_crop = protection[y:y + h, x:x + w]
        outer = ((crop_mask > 0) | (protected_crop > 0)).astype(np.uint8)
        outer_distance = cv2.distanceTransform(outer, cv2.DIST_L2, 3)
        object_distance = cv2.distanceTransform((protected_crop == 0).astype(np.uint8), cv2.DIST_L2, 3)
        # Tight object feathering avoids retaining old clothing around protected hands/products.
        alpha = np.minimum(
            np.minimum(outer_distance / max(3, w * .025), 1),
            np.minimum(object_distance / max(2, w * .006), 1),
        )[:, :, None]
        alpha[crop_mask == 0] = 0
    roi = original[y:y + h, x:x + w]
    result = original.copy()
    result[y:y + h, x:x + w] = np.rint(
        roi.astype(np.float32) * (1 - alpha) + generated.astype(np.float32) * alpha
    ).astype(np.uint8)
    delta = np.abs(result.astype(np.float32) - original.astype(np.float32))
    outside_changes = int(np.count_nonzero(np.any(delta > 0, axis=2) & (mask == 0)))
    region = mask > 0
    metrics = {
        "outside_mask_changed_pixels": outside_changes,
        "mask_pixels": int(np.count_nonzero(region)),
        "mean_absolute_channel_delta_inside_mask": float(delta[region].mean()),
        "fraction_mask_pixels_changed_over_20_levels": float(
            (np.max(delta, axis=2)[region] > 20).mean()
        ),
    }
    if protection is not None:
        metrics["protection_feather_pixels"] = max(2, w * .006)
    if outside_changes:
        raise PortraitError("编辑遮罩之外发生像素改动，已停止导出。")
    if (
        metrics["mean_absolute_channel_delta_inside_mask"] < 8
        or metrics["fraction_mask_pixels_changed_over_20_levels"] < .15
    ):
        raise PortraitError("重绘变化不足，未生成成功样图；请调整提示词或随机种子。")
    return result, metrics


def load_pipeline(directory: Path, threads: int):
    offline_environment()
    import torch
    from diffusers import DPMSolverMultistepScheduler, StableDiffusionInpaintPipeline

    torch.set_num_threads(threads)
    pipeline = StableDiffusionInpaintPipeline.from_pretrained(
        str(directory), variant="fp16", torch_dtype=torch.float32,
        use_safetensors=True, local_files_only=True,
    )
    if pipeline.safety_checker is None:
        raise PortraitError("模型未启用安全检查，拒绝运行。")
    pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
        pipeline.scheduler.config, use_karras_sigmas=True,
    )
    return pipeline.to("cpu")


def save_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise PortraitError(f"无法编码图片: {path.name}")
    path.write_bytes(encoded.tobytes())


def validate_prompts(pipeline, prompt: str) -> dict:
    maximum = pipeline.tokenizer.model_max_length
    if not isinstance(maximum, int) or not 1 <= maximum <= 10000:
        raise PortraitError("模型文本编码长度不可用，拒绝静默截断提示词。")
    lengths = {}
    for name, text in (("prompt", prompt), ("negative_prompt", NEGATIVE_PROMPT)):
        length = len(pipeline.tokenizer(text, truncation=False).input_ids)
        if length > maximum:
            raise ValueError(f"{name} 含 {length} 个 token，超过模型上限 {maximum}；请缩短描述。")
        lengths[name] = length
    return {**lengths, "maximum": maximum, "truncated": False}


def render_still(
    source: Path, output: Path, model_dir: Path = MODEL_DIR,
    seconds: float | None = None, prompt: str | None = None,
    seed: int = 42, steps: int = 24, threads: int = 8,
    scope: str = "head", protected_boxes: list[tuple[int, int, int, int]] | None = None,
    rights_confirmed: bool = False, segmenter_dir: Path = SEGMENTER_DIR,
    protected_mask: Path | None = None,
) -> Path:
    if scope not in ("head", "person"):
        raise ValueError("scope 必须是 head 或 person。")
    if scope == "person" and (not rights_confirmed or (not protected_boxes and protected_mask is None)):
        raise ValueError("人物重塑必须提供 --rights-confirmed 和 --protect-box 或 --protect-mask 商品/文字保护区。")
    default_brief = prompt is None
    if prompt is None:
        prompt = PERSON_PROMPT if scope == "person" else DEFAULT_PROMPT
    if not prompt.strip():
        raise ValueError("prompt 不能为空。")
    if not 0 <= seed < 2 ** 32 or not 10 <= steps <= 100 or not 1 <= threads <= 16:
        raise ValueError("seed 需在 [0, 2^32)，steps 需在 [10, 100]，threads 需在 [1, 16]。")
    if not source.is_file():
        raise FileNotFoundError(f"输入视频不存在: {source}")
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖: {output}")
    manifest = validate_model(model_dir)
    before = file_record(source), source.stat().st_mtime_ns
    frame, face, timestamp = select_frame(source, seconds)
    segmentation = None
    if scope == "person":
        mask, crop, segmentation = person_region(frame, face, segmenter_dir, threads)
    else:
        mask, crop = head_region(frame, face)
    protection_record = None
    extra_mask = None
    if protected_mask is not None:
        if protected_mask.suffix.lower() != ".png" or not protected_mask.is_file():
            raise ValueError("--protect-mask 必须是本地 PNG 文件。")
        extra_mask = cv2.imdecode(np.frombuffer(protected_mask.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        if extra_mask is None:
            raise ValueError("无法解码保护遮罩 PNG。")
        if extra_mask.ndim != 2 or extra_mask.dtype != np.uint8:
            raise ValueError("保护遮罩必须是 8 位灰度 PNG，不能包含颜色或透明通道。")
        protection_record = file_record(protected_mask)
    mask, protected = protect_regions(mask, protected_boxes or [], extra_mask)
    x, y, w, h = crop
    if scope == "person":
        scale = 768 / max(w, h)
        resolution = (max(256, int(round(w * scale / 64)) * 64),
                      max(256, int(round(h * scale / 64)) * 64))
    else:
        resolution = (512, 512)
    offline_environment()
    from PIL import Image
    import torch

    image = Image.fromarray(cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2RGB))
    model_image = image.resize(resolution, Image.Resampling.LANCZOS)
    model_mask = Image.fromarray(mask[y:y + h, x:x + w]).resize(resolution, Image.Resampling.NEAREST)
    logger.info("选定 %.3f 秒单帧，CPU 本地 %s 重绘 %s×%s，未上传素材。",
                timestamp, scope, *resolution)
    start = time.monotonic()
    pipeline = load_pipeline(model_dir, threads)
    prompt_tokens = validate_prompts(pipeline, prompt)
    loaded = time.monotonic()
    with torch.inference_mode():
        prediction = pipeline(
            prompt=prompt, negative_prompt=NEGATIVE_PROMPT,
            image=model_image, mask_image=model_mask, height=resolution[1], width=resolution[0],
            num_inference_steps=steps, strength=1.0, guidance_scale=7.5,
            generator=torch.Generator(device="cpu").manual_seed(seed),
        )
    finished = time.monotonic()
    safety = prediction.nsfw_content_detected
    if safety is None or len(safety) != 1 or safety[0]:
        raise PortraitError("安全检查未通过或未返回有效结果，未导出生成图片。")
    generated = cv2.cvtColor(np.asarray(prediction.images[0].convert("RGB")), cv2.COLOR_RGB2BGR)
    result, metrics = composite_head(frame, generated, mask, crop)
    protected_changes = int(np.count_nonzero(
        np.any(result != frame, axis=2) & (protected > 0)
    ))
    if protected_changes:
        raise PortraitError("商品或文字保护区发生改动，拒绝导出。")
    metrics["protected_region_changed_pixels"] = protected_changes
    if (file_record(source), source.stat().st_mtime_ns) != before:
        raise PortraitError("处理期间原视频发生变化，未导出结果。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".head-still-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        pair = np.hstack((frame, result))
        header = np.zeros((48, pair.shape[1], 3), dtype=np.uint8)
        for title, offset in (("ORIGINAL", 12), ("AI GENERATED - SINGLE FRAME", frame.shape[1] + 12)):
            cv2.putText(header, title, (offset, 31), cv2.FONT_HERSHEY_SIMPLEX, .60,
                        (255, 255, 255), 1, cv2.LINE_AA)
        label = np.zeros((48, frame.shape[1], 3), dtype=np.uint8)
        cv2.putText(label, "AI GENERATED - RESEARCH PREVIEW", (12, 31),
                    cv2.FONT_HERSHEY_SIMPLEX, .60, (255, 255, 255), 1, cv2.LINE_AA)
        pictures = {
            "original.png": frame, "mask.png": mask, "generated.png": result,
            "generated_labeled.png": np.vstack((label, result)),
            "protected.png": protected,
            "comparison.png": np.vstack((header, pair)),
            "model_input.png": cv2.cvtColor(np.asarray(model_image), cv2.COLOR_RGB2BGR),
            "model_generated_crop.png": generated,
        }
        for name, picture in pictures.items():
            save_png(staging / name, picture)
        report = {
            "schema_version": 1, "mode": f"local_cpu_single_frame_{scope}_inpainting",
            "status": "needs_human_review", "uploaded": False, "video_generated": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {**before[0], "mtime_ns": before[1], "frame_seconds": timestamp},
            "face": face, "crop": crop, "segmentation_model": segmentation,
            "model": {"id": MODEL_ID, "revision": manifest["revision"],
                      "license": manifest["license"], "dtype": "float32", "weight_format": "safetensors"},
            "parameters": {"prompt": prompt, "negative_prompt": NEGATIVE_PROMPT,
                           "seed": seed, "steps": steps, "guidance_scale": 7.5,
                           "threads": threads, "device": "cpu", "resolution": list(resolution),
                           "scope": scope, "protected_boxes": protected_boxes or [],
                           "protection_input": protection_record,
                           "prompt_tokens": prompt_tokens},
            "review": {
                "rights_confirmation_recorded": rights_confirmed,
                "fictional_adult_character_requested": True if default_brief else None,
                "recommended_display_file": "generated_labeled.png",
                "platform_approval_verified": False, "auto_publication": False,
                "requires_checks": ["appearance/anatomy", "product and text coverage",
                                    "advertising claims", "rights/licenses", "platform AI labeling"],
            },
            "torch_version": torch.__version__,
            "safety_check_passed": True, "metrics": metrics,
            "timing_seconds": {"load": loaded - start, "inference": finished - loaded},
            "limitations": [
                "Single-frame research prototype, not video character replacement.",
                "The new character is fictional; realism and aesthetic quality require human review.",
                (
                    "Person segmentation/dilation is approximate; protection masks/boxes are manually supplied, "
                    "not automatic product detection or OCR. Unmarked product/text areas are not guaranteed."
                    if scope == "person" else
                    "Automatic head/hair ellipse is approximate; long hair outside it remains original."
                ),
                "Only visible masked regions can change; the source silhouette and pose still constrain results.",
                "Hands, grasping, clothing, lighting and face quality require human review.",
                "Pixel-change metrics prove editing, not successful identity change or good anatomy.",
                "No temporal consistency, lip synchronization, publication, or commercial-use clearance.",
            ],
            "files": [file_record(staging / name) for name in pictures],
        }
        write_json(staging / "report.json", report)
        staging.rename(output)
    logger.info("单帧样图已保存，需人工审核，不代表视频换人成功: %s", output / "comparison.png")
    logger.info("CPU 推理耗时 %.1f 秒；遮罩外改动 %s 像素。", finished - loaded, metrics["outside_mask_changed_pixels"])
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CPU 本地头部/人物重绘静帧实验；不是完整视频换人。")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare-model", help="仅下载公开模型与许可说明，不读取视频")
    prepare.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    prepare_person = sub.add_parser("prepare-person", help="仅下载 PyTorch 官方人物分割权重")
    prepare_person.add_argument("--segmenter-dir", type=Path, default=SEGMENTER_DIR)
    render = sub.add_parser("render", help="仅使用本地模型进行单帧推理")
    render.add_argument("video", type=Path)
    render.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    render.add_argument("--output", type=Path)
    render.add_argument("--seconds", type=float)
    render.add_argument("--prompt", help="默认按 scope 选择虚构中国成年女性的头部或穿搭描述")
    render.add_argument("--scope", choices=("head", "person"), default="head")
    render.add_argument("--protect-box", action="append", type=protected_box, default=[],
                        help="保护区 x,y,width,height，原帧像素；可重复指定")
    render.add_argument("--protect-mask", type=Path, help="原帧同尺寸 8 位灰度 PNG；非黑像素不允许修改")
    render.add_argument("--rights-confirmed", action="store_true", help="确认拥有原素材改编权限")
    render.add_argument("--segmenter-dir", type=Path, default=SEGMENTER_DIR)
    render.add_argument("--seed", type=int, default=42)
    render.add_argument("--steps", type=int, default=24)
    render.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare-model":
            prepare_model(args.model_dir.resolve())
        elif args.action == "prepare-person":
            prepare_segmenter(args.segmenter_dir.resolve())
        else:
            source = args.video.resolve()
            output = args.output.resolve() if args.output else (
                source.parent / "edits" / datetime.now(timezone.utc).strftime(
                    f"{args.scope}-still-%Y%m%dT%H%M%S%fZ"
                )
            )
            render_still(
                source, output, args.model_dir.resolve(), args.seconds,
                args.prompt, args.seed, args.steps, args.threads,
                scope=args.scope, protected_boxes=args.protect_box,
                rights_confirmed=args.rights_confirmed, segmenter_dir=args.segmenter_dir.resolve(),
                protected_mask=args.protect_mask.resolve() if args.protect_mask is not None else None,
            )
    except ImportError as error:
        logger.error("缺少生成依赖（%s），请安装 requirements-generative.txt。", error.name)
        return 1
    except (PortraitError, OSError, ValueError, cv2.error) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
