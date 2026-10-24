import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
if AVAILABLE:
    import cv2
    import numpy as np
    import clothing_preview


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt")
class ClothingPreviewTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.full((200, 160, 3), (160, 140, 130), dtype=np.uint8)
        self.frame[120:180:2, 40:120] = (140, 120, 110)
        self.mask = np.zeros((200, 160), np.uint8)
        self.mask[120:180, 40:120] = 255
        self.face = {"box": (40, 20, 80, 80)}

    def test_preserves_face_geometry_outside_pixels_and_fabric_lightness(self):
        original = self.frame.copy()
        result, metrics = clothing_preview.recolor_clothing(self.frame, self.mask, self.face)
        np.testing.assert_array_equal(original, self.frame)
        np.testing.assert_array_equal(result[self.mask == 0], self.frame[self.mask == 0])
        self.assertEqual(metrics["face_changed_pixels"], 0)
        self.assertEqual(metrics["outside_mask_changed_pixels"], 0)
        self.assertGreater(metrics["changed_pixels"], 1000)
        before = cv2.cvtColor(self.frame, cv2.COLOR_BGR2LAB)[:, :, 0].astype(int)
        after = cv2.cvtColor(result, cv2.COLOR_BGR2LAB)[:, :, 0].astype(int)
        self.assertLessEqual(np.abs(before - after).max(), 2)
        self.assertGreater(after[121, 80] - after[122, 80], 10)

    def test_empty_wrong_size_color_and_face_masks_are_rejected(self):
        overlap = self.mask.copy()
        overlap[40, 50] = 255
        for mask in (np.zeros_like(self.mask), np.ones((20, 20), np.uint8),
                     np.ones_like(self.frame), overlap, self.mask.astype(np.uint16)):
            with self.subTest(shape=mask.shape), self.assertRaises(ValueError):
                clothing_preview.recolor_clothing(self.frame, mask, self.face)

    def test_export_preserves_source_and_records_exact_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, mask_path, output = root / "source.mp4", root / "mask.png", root / "result"
            source.write_bytes(b"source fixture")
            before = clothing_preview.file_record(source), source.stat().st_mtime_ns
            self.assertTrue(cv2.imwrite(str(mask_path), self.mask))
            with patch("clothing_preview.select_frame", return_value=(self.frame, self.face, .5)):
                clothing_preview.create_preview(source, output, mask_path)
            self.assertEqual((clothing_preview.file_record(source), source.stat().st_mtime_ns), before)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["generative_model_used"])
            self.assertFalse(report["hair_styling_applied"])
            self.assertFalse(report["makeup_applied"])
            self.assertEqual(report["status"], "needs_human_review")
            for record in report["files"]:
                self.assertEqual(clothing_preview.file_record(output / record["file"]), record)
            with self.assertRaises(FileExistsError):
                clothing_preview.create_preview(source, output, mask_path)
            self.assertFalse(list(root.glob(".clothing-preview-*")))

    def test_missing_source_is_logged_cli_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertLogs("clothing_preview", level="ERROR"):
                code = clothing_preview.main([
                    str(root / "missing.mp4"), "--clothing-mask", str(root / "mask.png"),
                    "--output", str(root / "result"),
                ])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
