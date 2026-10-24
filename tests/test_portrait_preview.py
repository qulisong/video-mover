import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
if AVAILABLE:
    import cv2
    import imageio_ffmpeg
    import numpy as np
    import portrait_preview


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt for portrait processing tests")
class PortraitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.face = {"box": (30, 20, 100, 140), "eyes": ((18, 32, 20, 16), (62, 32, 20, 16))}
        noise = np.random.default_rng(42).integers(-8, 9, (200, 160, 3))
        skin = np.full((200, 160, 3), (155, 150, 105), dtype=np.uint8)
        self.frame = np.clip(cv2.cvtColor(skin, cv2.COLOR_YCrCb2BGR).astype(int) + noise, 0, 255).astype(np.uint8)

    def make_source(self):
        source = self.root / "input.mp4"
        portrait_preview.run_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), [
            "-n", "-f", "lavfi", "-i", "color=c=0xc39b82:s=160x200:r=24:d=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ])
        return source

    def test_no_faces_or_zero_strength_leave_frame_identical(self):
        for profile in ("natural", "strong"):
            for faces, strength in (([], .6), ([self.face], 0)):
                output, _ = portrait_preview.retouch_frame(self.frame, faces, strength, profile)
                np.testing.assert_array_equal(output, self.frame)

    def test_changes_are_local_and_eyes_mouth_are_preserved(self):
        original = self.frame.copy()
        result, mask = portrait_preview.retouch_frame(self.frame, [self.face], .6)
        np.testing.assert_array_equal(original, self.frame)
        changed = np.any(result != original, axis=2)
        self.assertGreater(np.count_nonzero(changed), 100)
        self.assertFalse(np.any(changed & (mask == 0)))
        self.assertFalse(np.any(changed[:20]))
        self.assertFalse(np.any(changed[:, :30]))
        for x, y, w, h in self.face["eyes"]:
            self.assertFalse(np.any(changed[20 + y:20 + y + h, 30 + x:30 + x + w]))
        self.assertFalse(np.any(changed[20 + int(140 * .68):20 + int(140 * .90),
                                        30 + int(100 * .23):30 + int(100 * .77)]))

    def test_invalid_strength_is_rejected(self):
        for value in (-1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                portrait_preview.retouch_frame(self.frame, [self.face], value)

    def test_strong_profile_is_measurably_stronger_without_touching_protected_regions(self):
        natural, _ = portrait_preview.retouch_frame(self.frame, [self.face], .6)
        strong, mask = portrait_preview.retouch_frame(self.frame, [self.face], .9, "strong")
        support = mask > .5
        natural_delta = np.abs(natural.astype(float) - self.frame)[support].mean()
        strong_delta = np.abs(strong.astype(float) - self.frame)[support].mean()
        self.assertGreater(strong_delta, natural_delta * 2.5)
        luma_delta = (
            cv2.cvtColor(strong, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(float)
            - cv2.cvtColor(self.frame, cv2.COLOR_BGR2YCrCb)[:, :, 0]
        )
        self.assertGreater(luma_delta[support].mean(), 8)
        self.assertFalse(np.any(np.any(strong != self.frame, axis=2) & (mask == 0)))
        for x, y, w, h in self.face["eyes"]:
            np.testing.assert_array_equal(
                strong[20 + y:20 + y + h, 30 + x:30 + x + w],
                self.frame[20 + y:20 + y + h, 30 + x:30 + x + w],
            )
        np.testing.assert_array_equal(
            strong[20 + int(140 * .68):20 + int(140 * .90), 30 + 23:30 + 77],
            self.frame[20 + int(140 * .68):20 + int(140 * .90), 30 + 23:30 + 77],
        )
        self.assertEqual(strong.dtype, np.uint8)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            portrait_preview.retouch_frame(self.frame, [], .6, "unknown")
        with self.assertRaises(ValueError):
            portrait_preview.create_preview(self.root / "missing.mp4", self.root / "out", profile="unknown")

    def test_face_like_product_without_eye_pair_is_rejected(self):
        detector = portrait_preview.FaceDetector()
        with patch.object(detector, "face") as face, patch.object(detector, "eyes") as eyes:
            face.detectMultiScale.return_value = np.array([[30, 20, 100, 100]])
            eyes.detectMultiScale.return_value = ()
            self.assertEqual(detector.detect(self.frame), [])
            eyes.detectMultiScale.return_value = np.array([[10, 20, 10, 10], [15, 22, 10, 10]])
            self.assertEqual(detector.detect(self.frame), [])
            eyes.detectMultiScale.return_value = np.array([[20, 25, 15, 15], [65, 27, 15, 15]])
            self.assertEqual(len(detector.detect(self.frame)), 1)

    def test_comparison_labels_do_not_cover_source_pixels(self):
        pair = portrait_preview.comparison_frame(self.frame, self.frame)
        self.assertEqual(pair.shape, (244, 320, 3))
        np.testing.assert_array_equal(pair[44:, :160], self.frame)
        np.testing.assert_array_equal(pair[44:, 160:], self.frame)

    def test_creates_decodable_outputs_and_preserves_source_and_audio(self):
        source = self.make_source()
        original = portrait_preview.file_record(source), source.stat().st_mtime_ns
        output = self.root / "sample"
        with patch.object(portrait_preview.FaceDetector, "detect", return_value=[self.face]):
            portrait_preview.create_preview(source, output, duration=.5)
        self.assertEqual((portrait_preview.file_record(source), source.stat().st_mtime_ns), original)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "needs_human_review")
        self.assertFalse(report["uploaded"])
        self.assertEqual(report["processing"]["frames"], 12)
        self.assertEqual(report["processing"]["edited_frames"], 12)
        self.assertEqual(report["processing"]["pixels_outside_mask_edited_before_encoding"], 0)
        self.assertEqual(set(report["validation"]), {"original.mp4", "retouched.mp4", "comparison.mp4"})
        for record in report["files"]:
            self.assertEqual(portrait_preview.file_record(output / record["file"]), record)
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        import subprocess
        hashes = []
        for name in ("original.mp4", "retouched.mp4", "comparison.mp4"):
            process = subprocess.run([
                executable, "-v", "error", "-i", str(output / name),
                "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-",
            ], capture_output=True, check=True)
            hashes.append(process.stdout)
        self.assertEqual(len(set(hashes)), 1)
        self.assertFalse(list(self.root.glob(".portrait-*")))

    def test_no_face_is_explicit_failure_without_success_artifacts(self):
        source = self.make_source()
        output = self.root / "sample"
        with patch.object(portrait_preview.FaceDetector, "detect", return_value=[]):
            with self.assertRaisesRegex(portrait_preview.PortraitError, "没有检测"):
                portrait_preview.create_preview(source, output, duration=.5)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob(".portrait-*")))

    def test_strong_export_records_profile_and_change_measurements(self):
        source = self.make_source()
        output = self.root / "strong"
        with patch.object(portrait_preview.FaceDetector, "detect", return_value=[self.face]):
            portrait_preview.create_preview(source, output, duration=.5, strength=.9, profile="strong")
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["parameters"]["profile"], "strong")
        self.assertEqual(report["parameters"]["profile_settings"]["passes"], 2)
        self.assertGreater(report["processing"]["mean_luma_delta_on_edited_pixels"], 8)
        self.assertEqual(report["processing"]["pixels_outside_mask_edited_before_encoding"], 0)
        self.assertEqual(report["status"], "needs_human_review")

    def test_cli_passes_selected_profile_and_strength(self):
        source = self.root / "input.mp4"
        output = self.root / "out"
        with patch("portrait_preview.create_preview") as create:
            self.assertEqual(portrait_preview.main([
                str(source), "--output", str(output), "--profile", "strong", "--strength", ".9",
            ]), 0)
        self.assertEqual(create.call_args.args, (source, output, 0, 8, .9, "strong"))

    def test_existing_output_is_not_overwritten(self):
        source = self.make_source()
        output = self.root / "sample"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            portrait_preview.create_preview(source, output)
        self.assertEqual(marker.read_text(), "keep")

    def test_invalid_times_and_parameters_fail(self):
        source = self.make_source()
        for parameters in (
            {"start": -1}, {"duration": 0}, {"start": 10},
            {"start": float("nan")}, {"duration": float("inf")}, {"strength": 0},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                portrait_preview.create_preview(source, self.root / "sample", **parameters)
        self.assertFalse((self.root / "sample").exists())

    def test_ffmpeg_errors_are_reported(self):
        with self.assertRaisesRegex(portrait_preview.PortraitError, "FFmpeg"):
            portrait_preview.run_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), ["-not-a-real-option"])

    def test_cli_returns_failure_for_missing_input(self):
        with self.assertLogs("portrait_preview", level="ERROR"):
            self.assertEqual(portrait_preview.main([str(self.root / "missing.mp4")]), 1)


if __name__ == "__main__":
    unittest.main()
