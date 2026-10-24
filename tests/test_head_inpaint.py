import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
GENERATIVE = AVAILABLE and all(importlib.util.find_spec(name) for name in ("PIL", "torch"))
if AVAILABLE:
    import cv2
    import numpy as np
    import head_inpaint


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt")
class HeadRegionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = np.full((200, 160, 3), 90, dtype=np.uint8)
        self.face = {"box": (40, 50, 80, 90), "eyes": ((15, 25, 15, 15), (45, 25, 15, 15))}

    def model_fixture(self):
        directory = self.root / "model"
        records = []
        for name in head_inpaint.MODEL_FILES:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            records.append({**head_inpaint.file_record(path), "file": name})
        manifest = {"model": head_inpaint.MODEL_ID, "revision": head_inpaint.MODEL_REVISION,
                    "license": "creativeml-openrail-m", "files": records}
        head_inpaint.write_json(directory / "model_source.json", manifest)
        return directory, manifest

    def test_head_mask_covers_face_and_upper_hair_and_stays_in_crop(self):
        mask, crop = head_inpaint.head_region(self.frame, self.face)
        x, y, w, h = crop
        self.assertEqual(w, h)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, self.frame.shape[1])
        self.assertLessEqual(y + h, self.frame.shape[0])
        self.assertEqual(mask[65, 80], 255)
        self.assertEqual(mask[25, 80], 255)
        self.assertFalse(np.any(mask[y + h:]))
        self.assertEqual(mask[-1, -1], 0)

    def test_composite_preserves_every_outside_pixel_exactly(self):
        mask, crop = head_inpaint.head_region(self.frame, self.face)
        generated = np.full((512, 512, 3), (150, 180, 220), dtype=np.uint8)
        result, metrics = head_inpaint.composite_head(self.frame, generated, mask, crop)
        np.testing.assert_array_equal(result[mask == 0], self.frame[mask == 0])
        self.assertEqual(metrics["outside_mask_changed_pixels"], 0)
        self.assertGreater(metrics["mean_absolute_channel_delta_inside_mask"], 8)
        self.assertGreater(metrics["fraction_mask_pixels_changed_over_20_levels"], .15)
        self.assertEqual(self.frame[65, 80].tolist(), [90, 90, 90])

    def test_unchanged_generation_is_explicit_failure(self):
        mask, crop = head_inpaint.head_region(self.frame, self.face)
        with self.assertRaisesRegex(head_inpaint.PortraitError, "变化不足"):
            head_inpaint.composite_head(self.frame, self.frame.copy(), mask, crop)

    def test_opt_in_object_feather_removes_clothing_halo_without_changing_protected_pixels(self):
        mask = np.full(self.frame.shape[:2], 255, np.uint8)
        mask[:20] = 0
        protection = np.zeros_like(mask)
        protection[60:100, 60:100] = 255
        mask[protection > 0] = 0
        generated = np.full_like(self.frame, 220)
        crop = (0, 0, self.frame.shape[1], self.frame.shape[0])
        original_blend, _ = head_inpaint.composite_head(self.frame, generated, mask, crop)
        result, metrics = head_inpaint.composite_head(
            self.frame, generated, mask, crop, protection=protection,
        )
        self.assertTrue(np.all(result[80, 102] == 220))
        self.assertTrue(np.all(original_blend[80, 102] < 220))
        np.testing.assert_array_equal(result[protection > 0], self.frame[protection > 0])
        np.testing.assert_array_equal(result[mask == 0], self.frame[mask == 0])
        np.testing.assert_array_equal(result[21], original_blend[21])
        self.assertEqual(metrics["outside_mask_changed_pixels"], 0)
        with self.assertRaises(ValueError):
            head_inpaint.composite_head(self.frame, generated, mask, crop, protection=protection[:10])

    def test_empty_or_wrong_size_mask_is_rejected(self):
        for mask in (np.zeros((200, 160), np.uint8), np.ones((50, 50), np.uint8)):
            with self.assertRaises(ValueError):
                head_inpaint.composite_head(self.frame, self.frame.copy(), mask, (0, 0, 160, 160))

    def test_model_missing_corrupt_and_wrong_revision_are_rejected(self):
        with self.assertRaisesRegex(head_inpaint.PortraitError, "prepare-model"):
            head_inpaint.validate_model(self.root)
        directory, manifest = self.model_fixture()
        self.assertEqual(head_inpaint.validate_model(directory), manifest)
        weight = directory / "unet" / "diffusion_pytorch_model.fp16.safetensors"
        weight.write_bytes(b"corrupt")
        with self.assertRaisesRegex(head_inpaint.PortraitError, "校验失败"):
            head_inpaint.validate_model(directory)
        manifest["revision"] = "wrong"
        head_inpaint.write_json(directory / "model_source.json", manifest)
        with self.assertRaisesRegex(head_inpaint.PortraitError, "版本"):
            head_inpaint.validate_model(directory)

    def test_model_file_list_is_validated(self):
        directory, manifest = self.model_fixture()
        manifest["files"][0]["file"] = []
        head_inpaint.write_json(directory / "model_source.json", manifest)
        with self.assertRaisesRegex(head_inpaint.PortraitError, "格式"):
            head_inpaint.validate_model(directory)

    def test_only_safetensors_are_requested(self):
        weights = [name for name in head_inpaint.MODEL_FILES if name.endswith(".safetensors")]
        self.assertEqual(len(weights), 4)
        self.assertFalse(any(name.endswith((".bin", ".ckpt", ".py")) for name in head_inpaint.MODEL_FILES))
        self.assertIn("safety_checker/model.fp16.safetensors", weights)

    def test_default_character_is_fictional_adult_chinese(self):
        self.assertIn("fictional adult Chinese woman", head_inpaint.DEFAULT_PROMPT)
        self.assertIn("East Asian facial features", head_inpaint.DEFAULT_PROMPT)
        self.assertIn("natural everyday appearance", head_inpaint.DEFAULT_PROMPT)

    @unittest.skipUnless(GENERATIVE, "Install requirements-generative.txt")
    def test_offline_render_exports_evidence_and_preserves_source(self):
        from PIL import Image

        source = self.root / "input.mp4"
        source.write_bytes(b"unchanged video fixture")
        before = source.read_bytes(), source.stat().st_mtime_ns
        output = self.root / "result"
        pipeline = MagicMock(return_value=SimpleNamespace(
            images=[Image.new("RGB", (512, 512), (220, 180, 150))],
            nsfw_content_detected=[False],
        ))
        pipeline.tokenizer.model_max_length = 77
        pipeline.tokenizer.return_value.input_ids = [0] * 10
        with patch.dict(os.environ), patch("head_inpaint.validate_model", return_value={
            "revision": head_inpaint.MODEL_REVISION, "license": "creativeml-openrail-m",
        }), patch("head_inpaint.select_frame", return_value=(self.frame, self.face, 1.0)), patch(
            "head_inpaint.load_pipeline", return_value=pipeline
        ) as load:
            head_inpaint.render_still(source, output, steps=10)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)
        self.assertEqual(report["status"], "needs_human_review")
        self.assertFalse(report["uploaded"])
        self.assertFalse(report["video_generated"])
        self.assertTrue(report["safety_check_passed"])
        self.assertEqual(report["metrics"]["outside_mask_changed_pixels"], 0)
        for record in report["files"]:
            self.assertEqual(head_inpaint.file_record(output / record["file"]), record)
        mask = np.asarray(Image.open(output / "mask.png"))
        original = np.asarray(Image.open(output / "original.png"))
        result = np.asarray(Image.open(output / "generated.png"))
        np.testing.assert_array_equal(original[mask == 0], result[mask == 0])
        load.assert_called_once()
        self.assertFalse(list(self.root.glob(".head-still-*")))

    @unittest.skipUnless(GENERATIVE, "Install requirements-generative.txt")
    def test_failed_or_missing_safety_check_never_exports(self):
        from PIL import Image

        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        output = self.root / "result"
        for safety in (None, [], [True]):
            pipeline = MagicMock(return_value=SimpleNamespace(
                images=[Image.new("RGB", (512, 512))], nsfw_content_detected=safety,
            ))
            pipeline.tokenizer.model_max_length = 77
            pipeline.tokenizer.return_value.input_ids = [0] * 10
            with patch.dict(os.environ), patch("head_inpaint.validate_model", return_value={}), patch(
                "head_inpaint.select_frame", return_value=(self.frame, self.face, 1)
            ), patch("head_inpaint.load_pipeline", return_value=pipeline), self.assertRaisesRegex(
                head_inpaint.PortraitError, "安全检查"
            ):
                head_inpaint.render_still(source, output, steps=10)
            self.assertFalse(output.exists())

    def test_person_scope_requires_rights_and_explicit_protection(self):
        for kwargs in (
            {"scope": "person"},
            {"scope": "person", "rights_confirmed": True},
            {"scope": "person", "protected_boxes": [(0, 0, 10, 10)]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "rights-confirmed"):
                head_inpaint.render_still(self.root / "input.mp4", self.root / "out", **kwargs)

    def test_prompt_overflow_is_rejected_instead_of_truncated(self):
        pipeline = MagicMock()
        pipeline.tokenizer.model_max_length = 77
        pipeline.tokenizer.return_value.input_ids = [0] * 78
        with self.assertRaisesRegex(ValueError, "超过模型上限"):
            head_inpaint.validate_prompts(pipeline, "too long")
        pipeline.tokenizer.return_value.input_ids = [0] * 50
        self.assertFalse(head_inpaint.validate_prompts(pipeline, "short")["truncated"])

    @unittest.skipUnless(GENERATIVE, "Install requirements-generative.txt")
    def test_person_export_protects_product_and_records_review(self):
        from PIL import Image

        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        output = self.root / "result"
        raw_mask = np.ones(self.frame.shape[:2], dtype=np.uint8) * 255
        contour = np.zeros_like(raw_mask)
        contour[70:170, 30:90] = 255
        protection = self.root / "contour.png"
        self.assertTrue(cv2.imwrite(str(protection), contour))
        pipeline = MagicMock(return_value=SimpleNamespace(
            images=[Image.new("RGB", (512, 768), (220, 180, 150))],
            nsfw_content_detected=[False],
        ))
        pipeline.tokenizer.model_max_length = 77
        pipeline.tokenizer.return_value.input_ids = [0] * 50
        with patch.dict(os.environ), patch("head_inpaint.validate_model", return_value={
            "revision": head_inpaint.MODEL_REVISION, "license": "creativeml-openrail-m",
        }), patch("head_inpaint.select_frame", return_value=(self.frame, self.face, .5)), patch(
            "head_inpaint.person_region", return_value=(raw_mask, (0, 0, 160, 200), {"model": "fixture"})
        ), patch("head_inpaint.load_pipeline", return_value=pipeline):
            head_inpaint.render_still(
                source, output, steps=10, scope="person", rights_confirmed=True,
                protected_mask=protection, protected_boxes=[(100, 30, 20, 20)],
            )
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["parameters"]["scope"], "person")
        self.assertIn("sage green", report["parameters"]["prompt"])
        self.assertTrue(report["review"]["rights_confirmation_recorded"])
        self.assertFalse(report["review"]["platform_approval_verified"])
        self.assertEqual(report["metrics"]["protected_region_changed_pixels"], 0)
        self.assertEqual(report["parameters"]["protection_input"], head_inpaint.file_record(protection))
        original = np.asarray(Image.open(output / "original.png"))
        generated = np.asarray(Image.open(output / "generated.png"))
        np.testing.assert_array_equal(original[70:170, 30:90], generated[70:170, 30:90])
        np.testing.assert_array_equal(original[30:50, 100:120], generated[30:50, 100:120])
        labeled = np.asarray(Image.open(output / "generated_labeled.png"))
        self.assertEqual(labeled.shape[0], generated.shape[0] + 48)
        np.testing.assert_array_equal(labeled[48:], generated)

    def test_invalid_protection_file_fails_before_inference(self):
        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        protection = self.root / "contour.png"
        raw_mask = np.ones(self.frame.shape[:2], dtype=np.uint8) * 255
        for data in (b"not an image", cv2.imencode(".png", self.frame)[1].tobytes()):
            protection.write_bytes(data)
            with self.subTest(data_length=len(data)), patch(
                "head_inpaint.validate_model", return_value={},
            ), patch("head_inpaint.select_frame", return_value=(self.frame, self.face, .5)), patch(
                "head_inpaint.person_region", return_value=(raw_mask, (0, 0, 160, 200), {}),
            ), patch("head_inpaint.load_pipeline") as load:
                with self.assertRaisesRegex(ValueError, "PNG"):
                    head_inpaint.render_still(
                        source, self.root / "result", scope="person", rights_confirmed=True,
                        protected_mask=protection,
                    )
                load.assert_not_called()

    def test_invalid_parameters_fail_before_loading_model(self):
        for kwargs in ({"steps": 1}, {"threads": 0}, {"seed": -1}, {"prompt": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                head_inpaint.render_still(self.root / "missing.mp4", self.root / "out", **kwargs)

    def test_existing_output_is_not_overwritten(self):
        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        output = self.root / "result"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            head_inpaint.render_still(source, output)

    def test_cli_missing_source_is_clear_failure(self):
        with self.assertLogs("head_inpaint", level="ERROR"):
            self.assertEqual(head_inpaint.main(["render", str(self.root / "missing.mp4")]), 1)


if __name__ == "__main__":
    unittest.main()
