import json
import logging
from pathlib import Path

import cv2
import numpy as np

from portrait_preview import PortraitError
from video_archive import file_record, write_json


logger = logging.getLogger(__name__)
SEGMENTER_DIR = Path(__file__).resolve().parent / ".models" / "person-lraspp"


def prepare_segmenter(directory: Path) -> dict:
    import torch
    import torchvision
    from torchvision.models.segmentation import LRASPP_MobileNet_V3_Large_Weights

    weights = LRASPP_MobileNet_V3_Large_Weights.COCO_WITH_VOC_LABELS_V1
    logger.info("仅下载 PyTorch 官方人物分割权重，不读取或上传视频。")
    torch.hub.load_state_dict_from_url(
        weights.url, model_dir=str(directory), progress=True, check_hash=True, weights_only=True,
    )
    name = weights.url.rsplit("/", 1)[1]
    manifest = {
        "model": "lraspp_mobilenet_v3_large", "weights": weights.name,
        "url": weights.url, "torchvision": torchvision.__version__,
        "file": file_record(directory / name),
        "notes": "Official PyTorch weights; weight/data terms require separate publication review.",
    }
    write_json(directory / "model_source.json", manifest)
    return manifest


def person_region(
    frame: np.ndarray, face: dict, directory: Path, threads: int,
) -> tuple[np.ndarray, tuple[int, int, int, int], dict]:
    import torch
    from PIL import Image
    from torchvision.models.segmentation import (
        LRASPP_MobileNet_V3_Large_Weights, lraspp_mobilenet_v3_large,
    )

    manifest_path = directory / "model_source.json"
    if not manifest_path.is_file():
        raise PortraitError("人物分割模型尚未准备，请先运行 head_inpaint.py prepare-person。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    weights = LRASPP_MobileNet_V3_Large_Weights.COCO_WITH_VOC_LABELS_V1
    name = weights.url.rsplit("/", 1)[1]
    if not isinstance(manifest, dict) or (
        manifest.get("url") != weights.url or manifest.get("weights") != weights.name
    ):
        raise PortraitError("人物分割模型来源不一致，请重新准备模型。")
    path = directory / name
    if not path.is_file() or file_record(path) != manifest.get("file"):
        raise PortraitError("人物分割权重缺失或校验失败。")
    torch.set_num_threads(threads)
    model = lraspp_mobilenet_v3_large(weights=None, weights_backbone=None).eval().to("cpu")
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    tensor = weights.transforms()(image).unsqueeze(0)
    with torch.inference_mode():
        labels = model(tensor)["out"][0].argmax(0).numpy()
    person_id = weights.meta["categories"].index("person")
    raw = cv2.resize((labels == person_id).astype(np.uint8), (frame.shape[1], frame.shape[0]),
                     interpolation=cv2.INTER_NEAREST)
    mask, crop = region_from_person_mask(raw, face)
    return mask, crop, manifest


def region_from_person_mask(
    raw: np.ndarray, face: dict,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    _, components = cv2.connectedComponents((raw > 0).astype(np.uint8))
    x, y, w, h = face["box"]
    center_x, center_y = x + w // 2, y + h // 2
    if not (0 <= center_y < raw.shape[0] and 0 <= center_x < raw.shape[1]):
        raise PortraitError("人脸坐标超出人物分割图。")
    label = components[center_y, center_x]
    if label == 0:
        raise PortraitError("人物分割未覆盖选定人脸，请改选帧，不会退回整图重绘。")
    mask = (components == label).astype(np.uint8) * 255
    if np.count_nonzero(mask) < raw.size * .08:
        raise PortraitError("可见人物分割区域过小，拒绝整个人物重绘。")
    radius = max(3, int(min(raw.shape) * .018))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    mask = cv2.dilate(mask, kernel)
    points = cv2.findNonZero(mask)
    x, y, w, h = cv2.boundingRect(points)
    pad = max(8, radius * 2)
    left, top = max(0, x - pad), max(0, y - pad)
    right, bottom = min(raw.shape[1], x + w + pad), min(raw.shape[0], y + h + pad)
    return mask, (left, top, right - left, bottom - top)


def protect_regions(
    mask: np.ndarray, boxes: list[tuple[int, int, int, int]], extra_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    protected = np.zeros_like(mask)
    if extra_mask is not None:
        if extra_mask.shape != mask.shape or not np.any(extra_mask):
            raise ValueError("精细保护遮罩必须与原帧尺寸一致且非空。")
        protected[extra_mask > 0] = 255
    height, width = mask.shape
    for x, y, w, h in boxes:
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
            raise ValueError("保护框必须完整位于原帧内，且宽高大于零。")
        protected[y:y + h, x:x + w] = 255
    result = mask.copy()
    result[protected > 0] = 0
    if not np.any(result):
        raise PortraitError("保护区覆盖了全部可编辑人物，请调整保护框。")
    return result, protected


def protected_box(text: str) -> tuple[int, int, int, int]:
    try:
        values = tuple(int(value.strip()) for value in text.split(","))
    except ValueError:
        raise ValueError("保护框格式为 x,y,width,height（原帧像素整数）。") from None
    if len(values) != 4:
        raise ValueError("保护框格式为 x,y,width,height（原帧像素整数）。")
    return values
