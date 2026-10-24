import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
GENERATIVE = AVAILABLE and all(importlib.util.find_spec(name) for name in ("PIL", "torch", "psutil"))
if AVAILABLE:
    import cv2
    import numpy as np
    import klein_edit


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt")
class KleinEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = np.full((200, 160, 3), 90, np.uint8)
        self.face = {"box": (40, 20, 80, 80)}
        self.mask = np.full((200, 160), 255, np.uint8)
        self.protected = np.zeros_like(self.mask)
        self.protected[130:180, 20:60] = 255

    def test_face_is_always_protected_even_if_not_in_input_protection(self):
        mask, protected = klein_edit.editing_regions(self.frame, self.mask, self.protected, self.face)
        self.assertFalse(np.any(mask[20:100, 40:120]))
        self.assertTrue(np.all(protected[20:100, 40:120] == 255))
        self.assertFalse(np.any(mask[self.protected > 0]))
        self.assertEqual(self.mask[30, 50], 255)
        self.assertEqual(self.protected[30, 50], 0)

    def test_tiny_editable_area_is_rejected(self):
        with self.assertRaisesRegex(klein_edit.PortraitError, "5%"):
            klein_edit.editing_regions(self.frame, self.mask, self.mask, self.face)

    def style_brief(self):
        reference = self.root / "reference.png"
        picture = self.frame.copy()
        picture[:110] = (0, 0, 255)
        self.assertTrue(cv2.imwrite(str(reference), picture))
        path = self.root / "style.json"
        path.write_text(json.dumps({
            "rights_confirmed": True, "reference_use": "style_only",
            "face_free_crops_reviewed": True,
            "style_description": "Chestnut long hair and a plain cocoa crewneck top.",
            "references": [{"path": reference.name, "crop": [0, 110, 80, 80]}],
        }), encoding="utf-8")
        return path

    def test_style_reference_requires_rights_review_and_valid_crops(self):
        brief = self.style_brief()
        data = json.loads(brief.read_text())
        for updates in (
            {"rights_confirmed": False}, {"face_free_crops_reviewed": False},
            {"reference_use": "identity"},
            {"references": [{"path": "reference.png", "crop": [0, 190, 80, 80]}]},
            {"style_description": ""},
        ):
            brief.write_text(json.dumps({**data, **updates}), encoding="utf-8")
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                klein_edit.prepare_style_reference(brief)
        brief.write_text(json.dumps(data), encoding="utf-8")
        board, provenance = klein_edit.prepare_style_reference(brief)
        self.assertEqual(board.shape, (256, 256, 3))
        self.assertTrue(np.all(board == 90))
        klein_edit.validate_style_inputs(provenance)
        (self.root / "reference.png").write_bytes(b"changed")
        with self.assertRaisesRegex(klein_edit.PortraitError, "发生变化"):
            klein_edit.validate_style_inputs(provenance)

    def test_redesign_requires_explicit_opt_in_and_editable_face(self):
        mask, protected = klein_edit.editing_regions(
            self.frame, self.mask, self.protected, self.face, preserve_face=False,
        )
        self.assertTrue(np.all(mask[20:100, 40:120] == 255))
        self.assertFalse(np.any(protected[20:100, 40:120]))
        protection = self.protected.copy()
        protection[20:100, 40:120] = 255
        with self.assertRaisesRegex(klein_edit.PortraitError, "80%"):
            klein_edit.editing_regions(
                self.frame, self.mask, protection, self.face, preserve_face=False,
            )

    def test_identity_reference_requires_one_face_and_keeps_face_pixels(self):
        self.style_brief()
        path = self.root / "reference.png"
        for faces in ([], [self.face, self.face]):
            with patch("klein_edit.FaceDetector") as detector:
                detector.return_value.detect.return_value = faces
                with self.assertRaisesRegex(klein_edit.PortraitError, "仅有一张"):
                    klein_edit.prepare_identity_reference(path)
        with patch("klein_edit.FaceDetector") as detector:
            detector.return_value.detect.return_value = [self.face]
            image, evidence = klein_edit.prepare_identity_reference(path)
        self.assertEqual(image.shape, (512, 512, 3))
        self.assertTrue(np.all(image[:, :, 2] == 255))
        self.assertTrue(np.all(image[:, :, :2] == 0))
        self.assertEqual(evidence["reference_use"], "same_authorized_person_identity")
        self.assertFalse(evidence["identity_consistency_verified"])
        with self.assertRaisesRegex(ValueError, "不能同时"):
            klein_edit.render_still(
                self.root / "video", self.root / "output", self.root / "a", self.root / "b",
                rights_confirmed=True, style_brief=self.root / "style.json", identity_reference=path,
            )

    def test_invalid_input_masks_fail_explicitly(self):
        path = self.root / "mask.png"
        for data in (
            b"bad png", cv2.imencode(".png", self.frame)[1].tobytes(),
            cv2.imencode(".png", np.zeros_like(self.mask))[1].tobytes(),
        ):
            path.write_bytes(data)
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                klein_edit.read_mask(path, self.frame.shape[:2])
        self.assertTrue(cv2.imwrite(str(path), self.mask))
        np.testing.assert_array_equal(klein_edit.read_mask(path, self.frame.shape[:2]), self.mask)

    def test_rights_confirmation_required_before_model_loading(self):
        with self.assertRaisesRegex(ValueError, "rights-confirmed"):
            klein_edit.render_still(self.root / "video", self.root / "out", self.root / "a", self.root / "b")

    @unittest.skipUnless(GENERATIVE, "Install requirements-klein.txt")
    def test_insufficient_memory_fails_without_loading_model(self):
        with patch("psutil.virtual_memory", return_value=SimpleNamespace(available=16 * 1024 ** 3)), patch(
            "klein_edit.load_pipeline",
        ) as load:
            with self.assertRaisesRegex(klein_edit.PortraitError, "36 GiB"):
                klein_edit.render_still(
                    self.root / "video", self.root / "out", self.root / "a", self.root / "b",
                    rights_confirmed=True,
                )
            load.assert_not_called()

    @unittest.skipUnless(GENERATIVE, "Install requirements-klein.txt")
    def test_export_and_safety_failure_preserve_source_and_protected_pixels(self):
        from PIL import Image

        source, edit, protection = self.root / "video.mp4", self.root / "edit.png", self.root / "protect.png"
        source.write_bytes(b"unchanged source")
        before = klein_edit.file_record(source), source.stat().st_mtime_ns
        self.assertTrue(cv2.imwrite(str(edit), self.mask))
        self.assertTrue(cv2.imwrite(str(protection), self.protected))
        pipeline = MagicMock(return_value=SimpleNamespace(images=[Image.new("RGB", (608, 768), (220, 160, 180))]))
        for blocked in (True, False):
            output = self.root / f"result-{blocked}"
            with self.subTest(blocked=blocked), patch(
                "psutil.virtual_memory", return_value=SimpleNamespace(available=64 * 1024 ** 3),
            ), patch("klein_edit.validate_model", return_value={
                "license": "fixture", "model": "fixture",
            }), patch("klein_edit.select_frame", return_value=(self.frame, self.face, .5)), patch(
                "klein_edit.load_pipeline", return_value=pipeline,
            ), patch("klein_edit.check_safety", side_effect=(
                klein_edit.PortraitError("safety blocked") if blocked else None
            )):
                if blocked:
                    with self.assertRaisesRegex(klein_edit.PortraitError, "safety blocked"):
                        klein_edit.render_still(source, output, edit, protection, rights_confirmed=True)
                    self.assertFalse(output.exists())
                    continue
                klein_edit.render_still(source, output, edit, protection, rights_confirmed=True)
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["metrics"]["protected_region_changed_pixels"], 0)
                self.assertEqual(report["parameters"]["device"], "cpu")
                self.assertFalse(report["auto_publication"])
                self.assertFalse(report["platform_approval_verified"])
                self.assertEqual(report["status"], "needs_human_review")
                result = cv2.imread(str(output / "generated.png"))
                np.testing.assert_array_equal(result[20:100, 40:120], self.frame[20:100, 40:120])
                np.testing.assert_array_equal(result[self.protected > 0], self.frame[self.protected > 0])
                for record in report["files"]:
                    self.assertEqual(klein_edit.file_record(output / record["file"]), record)
                recomposed = self.root / "recomposed"
                klein_edit.recompose_still(source, output, recomposed, edit, protection)
                replay = json.loads((recomposed / "report.json").read_text(encoding="utf-8"))
                self.assertFalse(replay["recomposition"]["model_inference_repeated"])
                np.testing.assert_array_equal(cv2.imread(str(recomposed / "generated.png")), result)
                (output / "model_output.png").write_bytes(b"corrupt")
                with self.assertRaisesRegex(klein_edit.PortraitError, "校验失败"):
                    klein_edit.recompose_still(source, output, self.root / "bad", edit, protection)
        self.assertEqual((klein_edit.file_record(source), source.stat().st_mtime_ns), before)

    @unittest.skipUnless(GENERATIVE, "Install requirements-klein.txt")
    def test_style_redesign_uses_only_crops_and_keeps_protected_pixels(self):
        from PIL import Image

        brief = self.style_brief()
        source, edit, protection = self.root / "video.mp4", self.root / "edit.png", self.root / "protect.png"
        source.write_bytes(b"unchanged source")
        self.assertTrue(cv2.imwrite(str(edit), self.mask))
        self.assertTrue(cv2.imwrite(str(protection), self.protected))
        pipeline = MagicMock(return_value=SimpleNamespace(
            images=[Image.new("RGB", (816, 1024), (220, 160, 180))],
        ))
        output = self.root / "redesign"
        with patch(
            "psutil.virtual_memory", return_value=SimpleNamespace(available=64 * 1024 ** 3),
        ), patch("klein_edit.validate_model", return_value={
            "license": "fixture", "model": "fixture",
        }), patch("klein_edit.select_frame", return_value=(self.frame, self.face, .5)), patch(
            "klein_edit.load_pipeline", return_value=pipeline,
        ), patch("klein_edit.check_safety"):
            klein_edit.render_still(
                source, output, edit, protection, rights_confirmed=True, style_brief=brief,
            )
            model_inputs = pipeline.call_args.kwargs["image"]
            self.assertEqual(len(model_inputs), 2)
            self.assertEqual(model_inputs[1].size, (256, 256))
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["mode"], "local_cpu_style_guided_person_redesign")
            self.assertFalse(report["original_face_preserved"])
            self.assertFalse(report["style_reference"]["reference_identity_distinctness_verified"])
            self.assertTrue(report["metrics"]["original_face_changed_pixels"] > 0)
            self.assertEqual(report["metrics"]["protected_region_changed_pixels"], 0)
            self.assertEqual(report["parameters"]["resolution"], [816, 1024])
            self.assertEqual(report["appearance_review"]["status"], "pending")
            result = cv2.imread(str(output / "generated.png"))
            np.testing.assert_array_equal(result[self.protected > 0], self.frame[self.protected > 0])
            klein_edit.recompose_still(
                source, output, self.root / "recomposed-redesign", edit, protection,
            )
            np.testing.assert_array_equal(
                cv2.imread(str(self.root / "recomposed-redesign" / "generated.png")), result,
            )

    @unittest.skipUnless(GENERATIVE, "Install requirements-klein.txt")
    def test_identity_mode_conditions_on_reference_face_and_recomposes(self):
        from PIL import Image

        self.style_brief()
        source, edit, protection = self.root / "video.mp4", self.root / "edit.png", self.root / "protect.png"
        source.write_bytes(b"unchanged")
        self.assertTrue(cv2.imwrite(str(edit), self.mask))
        self.assertTrue(cv2.imwrite(str(protection), self.protected))
        pipeline = MagicMock(return_value=SimpleNamespace(
            images=[Image.new("RGB", (816, 1024), (220, 160, 180))],
        ))
        output = self.root / "identity"
        with patch("psutil.virtual_memory", return_value=SimpleNamespace(available=64 * 1024 ** 3)), patch(
            "klein_edit.validate_model", return_value={"license": "fixture", "model": "fixture"},
        ), patch("klein_edit.FaceDetector") as detector, patch(
            "klein_edit.select_frame", return_value=(self.frame, self.face, .5),
        ), patch("klein_edit.load_pipeline", return_value=pipeline), patch("klein_edit.check_safety"):
            detector.return_value.detect.return_value = [self.face]
            klein_edit.render_still(
                source, output, edit, protection, rights_confirmed=True,
                identity_reference=self.root / "reference.png",
            )
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["mode"], "local_cpu_identity_guided_person_edit")
            self.assertIsNone(report["style_reference"])
            self.assertFalse(report["identity_reference"]["identity_consistency_verified"])
            self.assertEqual(report["rights_confirmation_scope"], "source_and_identity_reference")
            self.assertIn("THE SAME", pipeline.call_args.kwargs["prompt"])
            self.assertEqual(len(pipeline.call_args.kwargs["image"]), 2)
            self.assertTrue((output / "identity_reference.png").is_file())
            self.assertEqual(report["metrics"]["protected_region_changed_pixels"], 0)
            klein_edit.recompose_still(
                source, output, self.root / "identity-recomposed", edit, protection,
            )
            np.testing.assert_array_equal(
                cv2.imread(str(output / "generated.png")),
                cv2.imread(str(self.root / "identity-recomposed" / "generated.png")),
            )

if __name__ == "__main__":
    unittest.main()
