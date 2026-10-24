import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)
LINK_FIELDS = ("product_detail_schema", "jump_schema", "web_url", "open_url", "mp_url")
RAW_FIELDS = ("anchor_info", "anchors", "promotions", "commerce_config_data")


def raw_fields_hash(fields: dict) -> str:
    content = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(content.encode("utf-8")).hexdigest()


def build_product_evidence(item: dict, source: dict | None = None) -> dict:
    fields = {key: deepcopy(item[key]) for key in RAW_FIELDS if key in item}
    presence = {}
    for key in RAW_FIELDS:
        if key not in fields:
            presence[key] = "missing"
        elif fields[key] is None:
            presence[key] = "null"
        elif fields[key] in ("", [], {}):
            presence[key] = "empty"
        else:
            presence[key] = "present"
    decoded = {}
    anchors = []
    if isinstance(fields.get("anchor_info"), dict):
        anchors.append(("anchor_info", fields["anchor_info"]))
    if isinstance(fields.get("anchors"), list):
        anchors.extend(
            (f"anchors[{index}]", anchor) for index, anchor in enumerate(fields["anchors"])
            if isinstance(anchor, dict)
        )
    for path, anchor in anchors:
        for field in ("extra", "content"):
            if field not in anchor:
                continue
            value = anchor[field]
            if isinstance(value, str) and value.strip():
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    decoded[f"{path}.{field}"] = {"status": "invalid_json"}
                else:
                    decoded[f"{path}.{field}"] = {"status": "decoded", "value": parsed}
            else:
                decoded[f"{path}.{field}"] = {"status": "not_encoded", "value": value}
    source = source or {}
    source_path = source.get("path")
    if isinstance(source_path, str):
        source_path = urlsplit(source_path).path
    return {
        "schema_version": 1,
        "video_id": text_value(item.get("aweme_id")),
        "scope": "commerce_fields_from_one_video_response",
        "source": {
            "host": "www.douyin.com" if source.get("host") == "www.douyin.com" else None,
            "path": source_path if isinstance(source_path, str) and source_path.startswith("/aweme/v1/web/") else None,
            "http_status": 200 if source.get("http_status") == 200 else None,
        },
        "field_presence": presence,
        "raw_fields": fields,
        "decoded_fields": decoded,
        "raw_fields_sha256": raw_fields_hash(fields),
    }


def text_value(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def product_record(data: dict, source: str, warnings: list[str]) -> dict | None:
    record = {
        "product_id": text_value(data.get("product_id")),
        "promotion_id": text_value(data.get("promotion_id")),
        "shop_id": text_value(data.get("shop_id")),
        "title": text_value(data.get("title")),
        "sources": [source],
        "links": [],
    }
    for field in LINK_FIELDS:
        value = data.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or any(char in value for char in "\r\n"):
            warnings.append(f"{source}.{field}: 商品链接格式无效，未保存该值。")
            continue
        value = value.strip()
        try:
            parsed = urlsplit(value)
            allowed = (
                parsed.scheme in ("https", "http", "sslocal", "snssdk1128", "aweme")
                and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and parsed.port is None
            )
        except ValueError:
            allowed = False
        if not allowed:
            warnings.append(f"{source}.{field}: 不是受支持的商品网页或 App 链接。")
            continue
        link_source = f"{source}.{field}"
        existing = next((link for link in record["links"] if link["url"] == value), None)
        if existing:
            existing["sources"].append(link_source)
        else:
            record["links"].append({
                "url": value,
                "kind": "web" if parsed.scheme in ("https", "http") else "app",
                "sources": [link_source],
            })
    if record["product_id"] or record["promotion_id"] or record["links"]:
        return record
    return None


def extract_product_links(item: dict) -> dict:
    target_id = text_value(item.get("aweme_id"))
    if not target_id or not target_id.isascii() or not target_id.isdigit():
        raise ValueError("商品信息缺少有效视频 ID。")
    warnings: list[str] = []
    products: list[dict] = []
    candidate_count = 0
    recognized_count = 0

    def add(data: object, source: str) -> None:
        nonlocal candidate_count, recognized_count
        candidate_count += 1
        if not isinstance(data, dict):
            warnings.append(f"{source}: 商品条目不是对象，已忽略。")
            return
        record = product_record(data, source, warnings)
        if record is None:
            if data:
                warnings.append(f"{source}: 未识别出商品 ID 或链接，原始条目会保留供核对。")
            return
        recognized_count += 1
        ids = (record["product_id"], record["promotion_id"])
        urls = {link["url"] for link in record["links"]}
        existing = next((
            product for product in products
            if (
                any(ids) and (product["product_id"], product["promotion_id"]) == ids
            ) or bool(urls.intersection(link["url"] for link in product["links"]))
        ), None)
        if existing is None:
            products.append(record)
            return
        for field in ("product_id", "promotion_id", "shop_id", "title"):
            if existing[field] is None:
                existing[field] = record[field]
        existing["sources"].extend(record["sources"])
        for link in record["links"]:
            same = next((old for old in existing["links"] if old["url"] == link["url"]), None)
            if same:
                same["sources"].extend(link["sources"])
            else:
                existing["links"].append(link)

    promotions = item.get("promotions")
    if isinstance(promotions, list):
        for index, promotion in enumerate(promotions):
            add(promotion, f"promotions[{index}]")
    elif promotions is not None:
        warnings.append("promotions: 商品列表格式无效。")

    anchors = []
    if isinstance(item.get("anchor_info"), dict):
        anchors.append(("anchor_info", item["anchor_info"]))
    if isinstance(item.get("anchors"), list):
        anchors.extend(
            (f"anchors[{index}]", anchor) for index, anchor in enumerate(item["anchors"])
            if isinstance(anchor, dict)
        )
    for source, anchor in anchors:
        if anchor.get("type") not in (3, "3"):
            continue
        extra = anchor.get("extra")
        if isinstance(extra, str) and extra.strip():
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                warnings.append(f"{source}.extra: 商品扩展信息不是有效 JSON，未解析。")
                extra = None
        entries = extra if isinstance(extra, list) else [extra] if isinstance(extra, dict) else []
        for index, entry in enumerate(entries):
            add(entry, f"{source}.extra[{index}]")
        if not entries:
            add(anchor, source)

    has_web_link = any(
        link["kind"] == "web" for product in products for link in product["links"]
    )
    all_extracted_have_web_links = bool(products) and all(
        any(link["kind"] == "web" for link in product["links"]) for product in products
    )
    return {
        "schema_version": 2,
        "video_id": target_id,
        "video_url": f"https://www.douyin.com/video/{target_id}",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "found" if all_extracted_have_web_links and not warnings else "partial" if products else "not_found",
        "has_web_link": has_web_link,
        "products": products,
        "warnings": warnings,
        "coverage": {
            "status": "not_verified",
            "scope": "captured_response_only",
            "all_video_products_verified": False,
            "page_product_count": None,
            "candidate_record_count": candidate_count,
            "recognized_record_count": recognized_count,
            "unrecognized_record_count": candidate_count - recognized_count,
            "extracted_product_count": len(products),
            "reason": "未与页面商品列表或独立商品接口核对，不能确认全部关联商品已获取。",
        },
    }


def save_product_links(item: dict, output: Path, source: dict | None = None) -> tuple[Path, Path]:
    report = extract_product_links(item)
    evidence = build_product_evidence(item, source)
    evidence["captured_at"] = report["captured_at"]
    prefix = report["video_id"] + "_products"
    json_path = output / f"{prefix}.json"
    text_path = output / f"{prefix}.txt"
    evidence_path = output / f"{prefix}_raw.json"
    report["evidence"] = {
        "file": evidence_path.name,
        "raw_fields_sha256": evidence["raw_fields_sha256"],
    }
    lines = [
        f"视频: {report['video_url']}",
        f"链接提取状态: {report['status']}（不代表所有关联商品均已获取）",
        "商品覆盖完整性: 尚未核实；仅处理本次视频接口响应，未与页面商品列表核对。",
        f"识别商品数: {len(report['products'])}",
        f"未识别候选记录数: {report['coverage']['unrecognized_record_count']}",
        f"原始商品字段及解码结果: {evidence_path.name}",
        "extra 原字符串见 raw_fields.anchor_info.extra；解码结果见 decoded_fields 中的 anchor_info.extra。",
        "",
    ]
    for index, product in enumerate(report["products"], 1):
        lines.extend([
            f"商品 {index}: {product['title'] or '名称未提供'}",
            f"商品 ID: {product['product_id'] or '未提供'}",
            f"推广 ID: {product['promotion_id'] or '未提供'}",
        ])
        for link in product["links"]:
            lines.append(f"{'网页链接' if link['kind'] == 'web' else 'App 链接'}: {link['url']}")
            lines.append(f"链接字段来源: {', '.join(link['sources'])}")
        if not any(link["kind"] == "web" for link in product["links"]):
            lines.append("未获取到该商品的网页链接；不会根据 ID 拼造链接。")
        lines.append("")
    if not report["products"]:
        lines.extend([
            "本次网页响应未提供可识别的关联商品信息。",
            "这不代表该视频没有商品；网页展示范围或当前接口响应可能不完整。", "",
        ])
    lines.extend(f"警告: {warning}" for warning in report["warnings"])
    output.mkdir(parents=True, exist_ok=True)
    for path, content in (
        (evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"),
        (json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
        (text_path, "\n".join(lines) + "\n"),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if report["status"] == "found":
        logger.info("本次响应识别出 %s 个商品及网页链接: %s", len(report["products"]), text_path)
    else:
        logger.warning(
            "商品信息提取状态为 %s；请查看 %s，不代表该视频没有商品。",
            report["status"], text_path,
        )
    logger.info("尚未核实全部关联商品；已保留原始商品字段供核对: %s", evidence_path)
    return json_path, text_path
