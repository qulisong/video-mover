import json
import tempfile
import unittest
from pathlib import Path

from product_links import build_product_evidence, extract_product_links, raw_fields_hash, save_product_links


class ProductLinksTests(unittest.TestCase):
    def item(self, products=None):
        if products is None:
            products = [{
                "product_id": "1001", "promotion_id": "2001", "shop_id": "3001",
                "title": "测试商品",
                "product_detail_schema": "https://ecom.douyin.com/product?id=2001",
                "jump_schema": "https://ecom.douyin.com/product?id=2001",
                "card_url": "https://aweme.snssdk.com/product-card-image",
            }]
        return {
            "aweme_id": "123", "promotions": [],
            "anchor_info": {"type": 3, "extra": json.dumps(products)},
        }

    def test_verified_anchor_extra_schema_and_duplicate_urls(self):
        report = extract_product_links(self.item())
        self.assertEqual(report["status"], "found")
        self.assertTrue(report["has_web_link"])
        self.assertEqual(len(report["products"]), 1)
        product = report["products"][0]
        self.assertEqual(product["product_id"], "1001")
        self.assertEqual(product["promotion_id"], "2001")
        self.assertEqual(product["title"], "测试商品")
        self.assertEqual(len(product["links"]), 1)
        self.assertEqual(product["links"][0]["kind"], "web")
        self.assertEqual(product["links"][0]["sources"], [
            "anchor_info.extra[0].product_detail_schema", "anchor_info.extra[0].jump_schema"
        ])
        self.assertEqual(report["schema_version"], 2)
        self.assertFalse(report["coverage"]["all_video_products_verified"])
        self.assertIsNone(report["coverage"]["page_product_count"])

    def test_raw_extra_and_decoded_value_are_both_preserved(self):
        item = self.item()
        original_extra = item["anchor_info"]["extra"]
        evidence = build_product_evidence(item)
        self.assertEqual(evidence["raw_fields"]["anchor_info"]["extra"], original_extra)
        self.assertEqual(
            evidence["decoded_fields"]["anchor_info.extra"],
            {"status": "decoded", "value": json.loads(original_extra)},
        )
        item["anchor_info"]["extra"] = "changed"
        self.assertEqual(evidence["raw_fields"]["anchor_info"]["extra"], original_extra)

    def test_raw_evidence_is_scoped_and_request_secrets_are_excluded(self):
        item = self.item()
        item.update({
            "cookie": "private-cookie", "headers": {"Authorization": "private-auth"},
            "video": {"url": "private-media-url"},
        })
        evidence = build_product_evidence(item, {
            "host": "www.douyin.com",
            "path": "/aweme/v1/web/aweme/detail/?msToken=private-token",
            "http_status": 200, "cookie": "private-cookie",
        })
        self.assertEqual(evidence["scope"], "commerce_fields_from_one_video_response")
        self.assertEqual(evidence["source"], {
            "host": "www.douyin.com", "path": "/aweme/v1/web/aweme/detail/", "http_status": 200,
        })
        rendered = json.dumps(evidence)
        for value in ("private-cookie", "private-auth", "private-token", "private-media-url"):
            self.assertNotIn(value, rendered)

    def test_field_presence_distinguishes_missing_null_and_empty(self):
        evidence = build_product_evidence({
            "aweme_id": "123", "anchor_info": None, "anchors": [], "promotions": {},
        })
        self.assertEqual(evidence["field_presence"], {
            "anchor_info": "null", "anchors": "empty", "promotions": "empty",
            "commerce_config_data": "missing",
        })
        self.assertNotIn("commerce_config_data", evidence["raw_fields"])

    def test_invalid_extra_is_preserved_with_explicit_decode_failure(self):
        item = self.item()
        item["anchor_info"]["extra"] = "original invalid JSON"
        evidence = build_product_evidence(item)
        self.assertEqual(evidence["raw_fields"]["anchor_info"]["extra"], "original invalid JSON")
        self.assertEqual(
            evidence["decoded_fields"]["anchor_info.extra"], {"status": "invalid_json"}
        )

    def test_unrecognized_candidates_are_counted_and_warned(self):
        item = self.item()
        values = json.loads(item["anchor_info"]["extra"])
        values.extend([{"title": "unrecognized record"}, "unexpected record"])
        item["anchor_info"]["extra"] = json.dumps(values)
        report = extract_product_links(item)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"]["candidate_record_count"], 3)
        self.assertEqual(report["coverage"]["recognized_record_count"], 1)
        self.assertEqual(report["coverage"]["unrecognized_record_count"], 2)
        self.assertFalse(report["coverage"]["all_video_products_verified"])
        self.assertEqual(len(report["warnings"]), 2)

    def test_no_product_data_is_not_proof_of_no_products(self):
        report = extract_product_links({"aweme_id": "123", "promotions": []})
        self.assertEqual(report["status"], "not_found")
        self.assertEqual(report["products"], [])
        self.assertFalse(report["has_web_link"])

    def test_non_commerce_anchor_is_not_a_product(self):
        item = self.item()
        item["anchor_info"]["type"] = 2
        item["anchor_info"]["web_url"] = "https://example.com/not-a-product"
        self.assertEqual(extract_product_links(item)["products"], [])

    def test_ids_without_links_are_partial_and_never_generate_urls(self):
        report = extract_product_links(self.item([{
            "product_id": "1001", "promotion_id": "2001", "title": "测试商品"
        }]))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["products"][0]["links"], [])

    def test_app_link_is_not_misrepresented_as_web_link(self):
        report = extract_product_links(self.item([{
            "product_id": "1001", "product_detail_schema": "sslocal://product_detail?id=1001"
        }]))
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["has_web_link"])
        self.assertEqual(report["products"][0]["links"][0]["kind"], "app")

    def test_malformed_extra_does_not_leak_its_value(self):
        item = self.item()
        item["anchor_info"]["extra"] = "private-session-value{invalid"
        report = extract_product_links(item)
        self.assertEqual(report["status"], "not_found")
        self.assertTrue(report["warnings"])
        self.assertNotIn("private-session-value", json.dumps(report))

    def test_promotions_and_anchor_products_are_merged(self):
        item = self.item()
        item["promotions"] = [{
            "product_id": "1001", "promotion_id": "2001",
            "web_url": "https://ecom.douyin.com/another-product-link?id=2001"
        }]
        report = extract_product_links(item)
        self.assertEqual(len(report["products"]), 1)
        self.assertEqual(len(report["products"][0]["links"]), 2)
        self.assertEqual(len(report["products"][0]["sources"]), 2)

    def test_multiple_products_and_distinct_promotions_are_preserved(self):
        report = extract_product_links(self.item([
            {"product_id": "1001", "promotion_id": "2001", "web_url": "https://example.com/p1"},
            {"product_id": "1001", "promotion_id": "2002", "web_url": "https://example.com/p2"},
        ]))
        self.assertEqual(len(report["products"]), 2)
        self.assertEqual(report["status"], "found")

    def test_only_whitelisted_product_fields_are_exported(self):
        item = self.item()
        item["cookie"] = "private-cookie"
        item["video"] = {"url": "https://example.com/private-video-token"}
        item["anchor_info"]["icon"] = {"url": "https://example.com/private-image-signature"}
        rendered = json.dumps(extract_product_links(item))
        for value in ("private-cookie", "private-video-token", "private-image-signature", "product-card-image"):
            self.assertNotIn(value, rendered)

    def test_invalid_links_are_rejected_and_warned(self):
        for link in [
            "javascript:alert(1)", "file:///C:/private", "https://user:password@example.com/p",
            "https://example.com/p\r\nprivate-value", "https://example.com:invalid/p"
        ]:
            with self.subTest(link=link):
                report = extract_product_links(self.item([{
                    "product_id": "1001", "product_detail_schema": link,
                }]))
                self.assertEqual(report["products"][0]["links"], [])
                self.assertTrue(report["warnings"])
                self.assertNotIn("private-value", json.dumps(report["warnings"]))

    def test_invalid_video_id_cannot_become_a_filename(self):
        for target_id in ("..\\private", "", None, True):
            with self.subTest(target_id=target_id), self.assertRaises(ValueError):
                extract_product_links({"aweme_id": target_id})

    def test_save_json_and_readable_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            json_path, text_path = save_product_links(self.item(), root)
            report = json.loads(json_path.read_text(encoding="utf-8"))
            text = text_path.read_text(encoding="utf-8")
            self.assertEqual(json_path.name, "123_products.json")
            self.assertEqual(text_path.name, "123_products.txt")
            self.assertEqual(report["status"], "found")
            self.assertIn("https://ecom.douyin.com/product?id=2001", text)
            self.assertIn("测试商品", text)
            self.assertIn("尚未核实", text)
            self.assertIn("anchor_info.extra", text)
            self.assertIn("链接字段来源", text)
            raw_path = root / report["evidence"]["file"]
            self.assertEqual(raw_path.name, "123_products_raw.json")
            evidence = json.loads(raw_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["evidence"]["raw_fields_sha256"],
                raw_fields_hash(evidence["raw_fields"]),
            )
            evidence["raw_fields"]["anchor_info"]["extra"] = "modified"
            self.assertNotEqual(
                report["evidence"]["raw_fields_sha256"],
                raw_fields_hash(evidence["raw_fields"]),
            )
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_missing_links_are_explicit_in_saved_files(self):
        with tempfile.TemporaryDirectory() as folder, self.assertLogs(
            "product_links", level="WARNING"
        ):
            json_path, text_path = save_product_links({"aweme_id": "123"}, Path(folder))
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["status"], "not_found")
            self.assertIn("不代表该视频没有商品", text_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
