import argparse
import gc
import json
import logging
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from head_inpaint import (
    MODEL_DIR as SAFETY_MODEL_DIR, composite_head, offline_environment,
    prepare_model, save_png, select_frame, validate_model,
)
from portrait_preview import FaceDetector, PortraitError
from video_archive import file_record, write_json


logger = logging.getLogger(__name__)
MODEL_ID = "black-forest-labs/FLUX.2-klein-4B"
MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
MODEL_DIR = Path(__file__).resolve().parent / ".models" / "flux2-klein-4b"
MODEL_FILES = (
    "README.md", "model_index.json", "scheduler/scheduler_config.json",
    "text_encoder/config.json", "text_encoder/generation_config.json",
    "text_encoder/model-00001-of-00002.safetensors",
    "text_encoder/model-00002-of-00002.safetensors", "text_encoder/model.safetensors.index.json",
    "tokenizer/added_tokens.json", "tokenizer/chat_template.jinja", "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json", "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json", "tokenizer/vocab.json",
    "transformer/config.json", "transformer/diffusion_pytorch_model.safetensors",
    "vae/config.json", "vae/diffusion_pytorch_model.safetensors",
)
PROMPT = (
    "Edit this photograph. Keep the same adult woman, exact face, expression, thin metal glasses, "
    "body proportions, pose, hands, detergent bottle, all labels, text and kitchen background. "
    "Change her hairstyle to a neat low ponytail tied behind the neck, with natural brown-black hair. "
    "Replace the striped knitted cardigan and inner top with the visible upper part of a simple "
    "modest dusty rose dress: opaque fabric, long sleeves, round neckline covering the chest, "
    "no stripes, no cardigan, no front buttons. Preserve realistic fabric folds, natural skin "
    "texture, the original lighting and camera perspective. Photorealistic everyday product presentation."
)
PERSON_PROMPT = (
    "Image 1 is the target kitchen photograph. Image 2 contains hair and clothing texture swatches "
    "only, not a person's identity. Redesign the woman in image 1 as a DIFFERENT fictional adult "
    "Chinese woman aged 30-35, with a new naturally proportioned face, softly defined jaw, "
    "warm expressive eyes, a friendly smile and realistic skin texture. Remove the original glasses "
    "and earrings. No spectacles or sunglasses. Keep her original pose, body proportions, hands, "
    "detergent bottle, printed labels, text, kitchen background, lighting and camera perspective. "
    "Replace the entire cardigan and inner top with one modest opaque long-sleeved crewneck top. "
    "No stripes, no cardigan, no buttons, no exposed chest. Photorealistic everyday product "
    "presentation, not a beauty filter. Use only these styling directions from image 2: "
)
IDENTITY_PROMPT = (
    "Image 1 defines the target kitchen scene, camera view, body pose and detergent bottle. "
    "Image 2 is the authorized identity reference. Replace the woman in image 1 with THE SAME "
    "adult Chinese woman shown in image 2. Preserve her recognizable facial identity, distinctive "
    "eye shape and spacing, eyebrow shape, nose, lip shape, small gentle closed-lip smile, "
    "soft facial proportions and natural complexion. Do not invent a different face. "
    "Use her long straight dark chestnut hair, worn loose. The raised hand and its shadow "
    "in image 2 are NOT facial features: remove that hand and render a clear naturally lit forehead. "
    "No glasses, sunglasses, jewelry, car or seatbelt. Keep image 1's exact body pose, hands, "
    "bottle, printed labels, text and background. Dress her in a modest plain cocoa-brown "
    "long-sleeved crewneck top. Match image 1's lighting. Natural photograph, no extra makeup "
    "or beauty smoothing. Identity fidelity is more important than redesign."
)


def prepare_identity_reference(path: Path) -> tuple[np.ndarray, dict]:
    record = file_record(path)
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("身份参考图片无法解码。")
    faces = FaceDetector().detect(image)
    if len(faces) != 1:
        raise PortraitError("身份参考必须能检测到且仅有一张带双眼的正面人脸。")
    x, y, width, height = faces[0]["box"]
    margin = round(max(width, height) * .08)
    x1, y1 = max(0, x - margin), max(0, y - margin)
    x2, y2 = min(image.shape[1], x + width + margin), min(image.shape[0], y + height + margin)
    return cv2.resize(image[y1:y2, x1:x2], (512, 512), interpolation=cv2.INTER_AREA), {
        "path": str(path), "record": record, "crop": [x1, y1, x2 - x1, y2 - y1],
        "reference_use": "same_authorized_person_identity",
        "identity_consistency_verified": False,
    }


def prepare_style_reference(brief_path: Path) -> tuple[np.ndarray, dict]:
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    if (
        not isinstance(brief, dict) or brief.get("rights_confirmed") is not True
        or brief.get("reference_use") != "style_only"
        or brief.get("face_free_crops_reviewed") is not True
    ):
        raise ValueError("风格简报必须确认参考素材授权、仅参考风格及人工审核裁剪不含人脸。")
    description = brief.get("style_description")
    references = brief.get("references")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 600:
        raise ValueError("style_description 必须为 1..600 字符的造型描述。")
    if not isinstance(references, list) or not 1 <= len(references) <= 2:
        raise ValueError("风格简报需要 1..2 张已审核的局部参考。")
    tiles, records = [], []
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ValueError("每张风格参考必须提供图片 path 和 crop。")
        path = (brief_path.parent / reference["path"]).resolve()
        record = file_record(path)
        image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        crop = reference.get("crop")
        if (
            image is None or not isinstance(crop, list) or len(crop) != 4
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in crop)
        ):
            raise ValueError("参考图片无法解码，或 crop 不是 [x,y,width,height] 整数数组。")
        x, y, width, height = crop
        if x < 0 or y < 0 or min(width, height) < 32 or x + width > image.shape[1] or y + height > image.shape[0]:
            raise ValueError("风格裁剪超出图片范围，或尺寸小于 32 像素。")
        tiles.append(cv2.resize(image[y:y + height, x:x + width], (256, 256), interpolation=cv2.INTER_AREA))
        records.append({"path": str(path), "record": record, "crop": crop})
    return np.hstack(tiles), {
        "brief": {"path": str(brief_path), "record": file_record(brief_path)},
        "references": records, "style_description": description.strip(),
        "reference_use": "style_only_face_free_crops",
        "face_free_crops_manually_reviewed": True,
        "reference_identity_distinctness_verified": False,
    }


def validate_style_inputs(reference: dict) -> None:
    for item in [reference["brief"], *reference["references"]]:
        if file_record(Path(item["path"])) != item["record"]:
            raise PortraitError("处理期间风格简报或参考图片发生变化，拒绝导出。")


def read_mask(path: Path, shape: tuple[int, int]) -> np.ndarray:
    if path.suffix.lower() != ".png":
        raise ValueError("遮罩必须是 PNG。")
    mask = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    if mask is None or mask.shape != shape or mask.dtype != np.uint8 or not np.any(mask):
        raise ValueError("遮罩必须是原帧同尺寸、非空的 8 位灰度 PNG。")
    return mask


def editing_regions(
    frame: np.ndarray, edit_mask: np.ndarray, protected: np.ndarray, face: dict,
    preserve_face: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    protected = protected.copy()
    x, y, w, h = face["box"]
    if preserve_face:
        protected[y:y + h, x:x + w] = 255
    mask = edit_mask.copy()
    mask[protected > 0] = 0
    if np.count_nonzero(mask) < frame.shape[0] * frame.shape[1] * .05:
        raise PortraitError("保护后可编辑区域不足原帧 5%，拒绝无效大幅换造型实验。")
    if not preserve_face and np.count_nonzero(mask[y:y + h, x:x + w]) < .8 * w * h:
        raise PortraitError("重新设计人物要求至少 80% 的原脸区域可编辑，请检查遮罩。")
    return mask, protected


def load_pipeline(directory: Path, threads: int):
    import torch
    from diffusers import Flux2KleinPipeline

    torch.set_num_threads(threads)
    pipeline = Flux2KleinPipeline.from_pretrained(
        str(directory), torch_dtype=torch.float32, use_safetensors=True,
        local_files_only=True, low_cpu_mem_usage=True,
    ).to("cpu")
    pipeline.text_encoder.eval()
    pipeline.transformer.eval()
    pipeline.vae.eval()
    return pipeline


def check_safety(image: np.ndarray, directory: Path) -> None:
    import torch
    from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
    from PIL import Image
    from transformers import CLIPImageProcessor

    checker = StableDiffusionSafetyChecker.from_pretrained(
        str(directory / "safety_checker"), variant="fp16",
        torch_dtype=torch.float32, use_safetensors=True, local_files_only=True,
    ).to("cpu").eval()
    processor = CLIPImageProcessor.from_pretrained(str(directory / "feature_extractor"), local_files_only=True)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    inputs = processor(images=Image.fromarray(rgb), return_tensors="pt")
    with torch.inference_mode():
        _, flagged = checker(images=rgb[None].astype(np.float32) / 255, clip_input=inputs.pixel_values)
    if flagged is None or len(flagged) != 1 or flagged[0]:
        raise PortraitError("本地内容安全检查未通过或缺少结果，拒绝导出。")


def render_still(
    source: Path, output: Path, edit_path: Path, protected_path: Path,
    seconds: float = .5, rights_confirmed: bool = False, threads: int = 8,
    seed: int = 7, model_dir: Path = MODEL_DIR, safety_dir: Path = SAFETY_MODEL_DIR,
    style_brief: Path | None = None,
    identity_reference: Path | None = None,
) -> Path:
    if not rights_confirmed:
        raise ValueError("必须提供 --rights-confirmed 确认素材改编授权。")
    if not 1 <= threads <= 16 or not 0 <= seed < 2 ** 32:
        raise ValueError("threads 必须为 1..16，seed 必须为 [0,2^32)。")
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖: {output}")
    if style_brief is not None and identity_reference is not None:
        raise ValueError("风格参考和同一人物身份参考不能同时启用。")
    style_image, reference = prepare_style_reference(style_brief) if style_brief is not None else (None, None)
    identity_image, identity = (
        prepare_identity_reference(identity_reference) if identity_reference is not None else (None, None)
    )
    redesign = reference is not None or identity is not None
    import psutil
    import torch
    from PIL import Image

    if psutil.virtual_memory().available < 36 * 1024 ** 3:
        raise PortraitError("CPU float32 实验要求至少 36 GiB 当前可用内存；不会自动切换 GPU 或云服务。")
    manifest = validate_model(model_dir, MODEL_ID, MODEL_REVISION, MODEL_FILES)
    safety_manifest = validate_model(safety_dir)
    before = file_record(source), source.stat().st_mtime_ns
    frame, face, timestamp = select_frame(source, seconds)
    inputs = {"edit_mask": file_record(edit_path), "protection": file_record(protected_path)}
    mask, protected = editing_regions(
        frame, read_mask(edit_path, frame.shape[:2]), read_mask(protected_path, frame.shape[:2]), face,
        preserve_face=not redesign,
    )
    offline_environment()
    height = 1024 if redesign else 768
    width = max(256, round(frame.shape[1] * height / frame.shape[0] / 16) * 16)
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
        (width, height), Image.Resampling.LANCZOS,
    )
    prompt = IDENTITY_PROMPT if identity is not None else (
        PROMPT if reference is None else PERSON_PROMPT + reference["style_description"]
    )
    sequence_length = 512 if redesign else 256
    reference_image = identity_image if identity_image is not None else style_image
    model_images = image if reference_image is None else [
        image, Image.fromarray(cv2.cvtColor(reference_image, cv2.COLOR_BGR2RGB)),
    ]
    logger.info("CPU-only float32 单帧实验 %sx%s；4 步；全程本地，不上传素材。", width, height)
    started = time.monotonic()
    pipeline = load_pipeline(model_dir, threads)
    loaded = time.monotonic()
    with torch.inference_mode():
        prediction = pipeline(
            image=model_images, prompt=prompt, width=width, height=height,
            num_inference_steps=4, guidance_scale=1.0, max_sequence_length=sequence_length,
            generator=torch.Generator(device="cpu").manual_seed(seed),
        )
    generated = cv2.cvtColor(np.asarray(prediction.images[0].convert("RGB")), cv2.COLOR_RGB2BGR)
    del prediction, pipeline
    gc.collect()
    inferred = time.monotonic()
    result, metrics = composite_head(
        frame, generated, mask, (0, 0, frame.shape[1], frame.shape[0]),
        protection=protected if redesign else None,
    )
    metrics["protected_region_changed_pixels"] = int(np.count_nonzero(
        np.any(result != frame, axis=2) & (protected > 0)
    ))
    if metrics["protected_region_changed_pixels"]:
        raise PortraitError("指定保护区发生改动，拒绝导出。")
    x, y, w, h = face["box"]
    metrics["original_face_changed_pixels"] = int(np.count_nonzero(
        np.any(result[y:y + h, x:x + w] != frame[y:y + h, x:x + w], axis=2)
    ))
    if redesign and metrics["original_face_changed_pixels"] < .5 * w * h:
        raise PortraitError("原脸区域变化不足，未完成大幅人物重设计，拒绝导出。")
    check_safety(result, safety_dir)
    if (file_record(source), source.stat().st_mtime_ns) != before:
        raise PortraitError("处理期间原视频发生变化，拒绝导出。")
    if inputs != {"edit_mask": file_record(edit_path), "protection": file_record(protected_path)}:
        raise PortraitError("处理期间输入遮罩发生变化，拒绝导出。")
    if reference is not None:
        validate_style_inputs(reference)
    if identity is not None and file_record(Path(identity["path"])) != identity["record"]:
        raise PortraitError("处理期间身份参考图片发生变化，拒绝导出。")
    pair = np.hstack((frame, result))
    header = np.zeros((48, pair.shape[1], 3), np.uint8)
    title = "AI EDIT - IDENTITY REFERENCE" if identity is not None else (
        "AI EDIT - NEW CHARACTER" if redesign else "AI EDIT - ORIGINAL FACE"
    )
    for title, offset in (("ORIGINAL", 12), (title, frame.shape[1] + 12)):
        cv2.putText(header, title, (offset, 31), cv2.FONT_HERSHEY_SIMPLEX, .60,
                    (255, 255, 255), 1, cv2.LINE_AA)
    label = header[:, frame.shape[1]:].copy()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".klein-still-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        pictures = {
            "original.png": frame, "generated.png": result, "mask.png": mask,
            "protected.png": protected, "model_output.png": generated,
            "generated_labeled.png": np.vstack((label, result)),
            "comparison.png": np.vstack((header, pair)),
        }
        if style_image is not None:
            pictures["style_reference.png"] = style_image
        if identity_image is not None:
            pictures["identity_reference.png"] = identity_image
        for name, picture in pictures.items():
            save_png(staging / name, picture)
        memory = psutil.Process().memory_info()
        write_json(staging / "report.json", {
            "schema_version": 1, "status": "needs_human_review",
            "mode": "local_cpu_identity_guided_person_edit" if identity is not None else (
                "local_cpu_style_guided_person_redesign" if redesign else "local_cpu_original_face_klein_edit"
            ),
            "uploaded": False, "style_reference": reference,
            "identity_reference": identity,
            "rights_confirmation_scope": "source_and_identity_reference" if identity is not None else (
                "source_and_style_references" if reference is not None else "source"
            ),
            "original_face_preserved": not redesign,
            "appearance_review": {"status": "pending", "aesthetic_acceptance_verified": False},
            "video_generated": False, "auto_publication": False, "platform_approval_verified": False,
            "rights_confirmed": rights_confirmed,
            "source": {**before[0], "mtime_ns": before[1], "frame_seconds": timestamp},
            "model": {"id": MODEL_ID, "revision": MODEL_REVISION, "license": manifest["license"]},
            "safety_checker": {"model": safety_manifest["model"], "license": safety_manifest["license"]},
            "safety_check_passed": True, "face": face, "input_masks": inputs,
            "parameters": {"prompt": prompt, "resolution": [width, height], "steps": 4,
                           "guidance_scale": 1.0, "seed": seed, "threads": threads,
                           "max_sequence_length": sequence_length,
                           "device": "cpu", "dtype": "float32",
                           "protection_feather_pixels": metrics.get("protection_feather_pixels"),
                           "composite_feather_pixels": max(3, frame.shape[1] * .025)},
            "metrics": metrics,
            "timing_seconds": {"load": loaded - started, "inference": inferred - loaded},
            "process_peak_memory_bytes": getattr(memory, "peak_wset", memory.rss),
            "limitations": [
                "Single-frame experiment; CPU performance and quality are not video validation.",
                "The model edits the full reference image; exact preservation is enforced by final compositing.",
                "Manually reviewed masks are frame-specific, not automatic product/hand/hair detection.",
                (
                    "The same reference person is requested, not guaranteed; identity fidelity needs human review."
                    if identity is not None else (
                        "A new fictional adult character is requested; visual identity distinctness needs human review."
                        if redesign else
                        "Original face and glasses stay original; hairline, neck and clothing transitions need review."
                    )
                ),
                "Safety checks and pixel changes do not establish aesthetics or publication compliance.",
            ],
            "files": [file_record(staging / name) for name in pictures],
        })
        staging.rename(output)
    logger.info("已导出待审核样图，尚未判定造型合格: %s", output / "comparison.png")
    return output


def recompose_still(
    source: Path, previous: Path, output: Path, edit_path: Path, protected_path: Path,
    safety_dir: Path = SAFETY_MODEL_DIR,
) -> Path:
    if output.exists():
        raise FileExistsError(f"输出目录已存在，不会覆盖: {output}")
    report_path = previous / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("model"), dict):
        raise ValueError("原结果报告格式无效。")
    if (
        report.get("model", {}).get("id") != MODEL_ID
        or report.get("model", {}).get("revision") != MODEL_REVISION
        or not report.get("rights_confirmed")
        or not report.get("safety_check_passed")
    ):
        raise ValueError("原结果缺少匹配的模型版本、授权或安全检查证据。")
    mode = report.get("mode")
    if mode not in (
        "local_cpu_original_face_klein_edit", "local_cpu_style_guided_person_redesign",
        "local_cpu_identity_guided_person_edit",
    ):
        raise ValueError("原结果的编辑模式无效。")
    redesign = mode != "local_cpu_original_face_klein_edit"
    before = file_record(source), source.stat().st_mtime_ns
    if before[0]["sha256"] != report["source"]["sha256"]:
        raise PortraitError("原视频与生成结果记录不一致，拒绝重新合成。")
    names = {
        "original.png", "generated.png", "mask.png", "protected.png", "model_output.png",
        "generated_labeled.png", "comparison.png",
    }
    if redesign:
        names.add("identity_reference.png" if mode == "local_cpu_identity_guided_person_edit" else "style_reference.png")
    records = report.get("files")
    if (
        not isinstance(records, list) or len(records) != len(names)
        or not all(isinstance(record, dict) for record in records)
        or {record.get("file") for record in records} != names
    ):
        raise ValueError("原结果图片清单不完整。")
    pictures = {}
    for record in records:
        path = previous / record["file"]
        if file_record(path) != record:
            raise PortraitError(f"原结果图片校验失败: {record['file']}")
        image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"无法解码原结果图片: {record['file']}")
        pictures[record["file"]] = image
    frame = pictures["original.png"]
    inputs = {"edit_mask": file_record(edit_path), "protection": file_record(protected_path)}
    mask, protected = editing_regions(
        frame, read_mask(edit_path, frame.shape[:2]), read_mask(protected_path, frame.shape[:2]), report["face"],
        preserve_face=not redesign,
    )
    result, metrics = composite_head(
        frame, pictures["model_output.png"], mask, (0, 0, frame.shape[1], frame.shape[0]),
        protection=protected if redesign else None,
    )
    metrics["protected_region_changed_pixels"] = int(np.count_nonzero(
        np.any(result != frame, axis=2) & (protected > 0)
    ))
    if metrics["protected_region_changed_pixels"]:
        raise PortraitError("重新合成改动了保护区，拒绝导出。")
    x, y, w, h = report["face"]["box"]
    metrics["original_face_changed_pixels"] = int(np.count_nonzero(
        np.any(result[y:y + h, x:x + w] != frame[y:y + h, x:x + w], axis=2)
    ))
    if redesign and metrics["original_face_changed_pixels"] < .5 * w * h:
        raise PortraitError("重新合成后原脸区域变化不足，拒绝导出。")
    offline_environment()
    validate_model(safety_dir)
    check_safety(result, safety_dir)
    if (file_record(source), source.stat().st_mtime_ns) != before:
        raise PortraitError("重新合成期间原视频发生变化，拒绝导出。")
    if inputs != {"edit_mask": file_record(edit_path), "protection": file_record(protected_path)}:
        raise PortraitError("重新合成期间输入遮罩发生变化，拒绝导出。")
    pictures["generated.png"] = result
    pictures["mask.png"], pictures["protected.png"] = mask, protected
    pictures["comparison.png"] = np.vstack((pictures["comparison.png"][:48], np.hstack((frame, result))))
    pictures["generated_labeled.png"] = np.vstack((pictures["generated_labeled.png"][:48], result))
    report["metrics"], report["input_masks"] = metrics, inputs
    report["appearance_review"] = {"status": "pending", "aesthetic_acceptance_verified": False}
    report["parameters"]["composite_feather_pixels"] = max(3, frame.shape[1] * .025)
    report["parameters"]["protection_feather_pixels"] = metrics.get("protection_feather_pixels")
    report["recomposition"] = {"parent_report": file_record(report_path), "model_inference_repeated": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".klein-compose-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        for name, picture in pictures.items():
            save_png(staging / name, picture)
        report["files"] = [file_record(staging / name) for name in pictures]
        write_json(staging / "report.json", report)
        staging.rename(output)
    logger.info("复用原模型输出完成遮罩修正，未再次生成: %s", output / "comparison.png")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="隔离环境中的 Klein CPU-only 单帧换造型或人物重设计实验。")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-model")
    prepare.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    render = commands.add_parser("render")
    render.add_argument("video", type=Path)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--edit-mask", type=Path, required=True)
    render.add_argument("--protect-mask", type=Path, required=True)
    render.add_argument("--seconds", type=float, default=.5)
    render.add_argument("--rights-confirmed", action="store_true", help="确认原视频及所用参考人物/风格的改编使用权")
    render.add_argument("--threads", type=int, default=8)
    render.add_argument("--seed", type=int, default=7)
    references = render.add_mutually_exclusive_group()
    references.add_argument("--style-brief", type=Path, help="已授权、人工审核的无脸局部风格简报；启用新人物模式")
    references.add_argument("--identity-reference", type=Path, help="本人或已获相应授权的人物图片；请求保留同一人物")
    recompose = commands.add_parser("recompose")
    recompose.add_argument("video", type=Path)
    recompose.add_argument("--previous-result", type=Path, required=True)
    recompose.add_argument("--output", type=Path, required=True)
    recompose.add_argument("--edit-mask", type=Path, required=True)
    recompose.add_argument("--protect-mask", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-model":
            prepare_model(args.model_dir.resolve(), MODEL_ID, MODEL_REVISION, MODEL_FILES, "apache-2.0")
        elif args.command == "recompose":
            recompose_still(
                args.video.resolve(), args.previous_result.resolve(), args.output.resolve(),
                args.edit_mask.resolve(), args.protect_mask.resolve(),
            )
        else:
            render_still(
                args.video.resolve(), args.output.resolve(), args.edit_mask.resolve(),
                args.protect_mask.resolve(), args.seconds, args.rights_confirmed, args.threads, args.seed,
                style_brief=args.style_brief.resolve() if args.style_brief is not None else None,
                identity_reference=args.identity_reference.resolve() if args.identity_reference is not None else None,
            )
    except (PortraitError, ValueError, OSError, RuntimeError, cv2.error) as error:
        logger.error("%s: %s", type(error).__name__, error)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
