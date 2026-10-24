import copy
import importlib.util
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
if AVAILABLE:
    import cv2
    import imageio_ffmpeg
    import numpy as np
    import head_video
    from portrait_preview import PortraitError, run_ffmpeg, video_info
    from video_archive import file_record


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt for head video tests")
class HeadVideoTests(unittest.TestCase):
    def plan(self):
        return {
            "rights_confirmed": True, "masks_manually_reviewed": True,
            "shots": [
                {"start": 0, "stop": 3, "keys": [{"frame": 1, "polygon": [[10, 10], [80, 10], [80, 80]]}]},
                {"start": 3, "stop": 5, "preserve": True},
            ],
        }

    def test_plan_requires_rights_and_review(self):
        for field in ("rights_confirmed", "masks_manually_reviewed"):
            plan = self.plan()
            plan[field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                head_video.validate_plan(plan, 5, (128, 128))

    def test_plan_rejects_gaps_overlap_truncation_and_bad_keys(self):
        plans = []
        for start, stop in ((4, 5), (2, 5), (3, 4), (3, 6)):
            plan = self.plan()
            plan["shots"][1].update(start=start, stop=stop)
            plans.append(plan)
        plan = self.plan()
        plan["shots"][0]["keys"][0]["frame"] = 3
        plans.append(plan)
        plan = self.plan()
        plan["shots"][0]["keys"] *= 2
        plans.append(plan)
        plan = self.plan()
        plan["shots"][0]["keys"][0]["polygon"][0][0] = -1
        plans.append(plan)
        plan = self.plan()
        del plan["shots"][0]["keys"]
        plans.append(plan)
        for plan in plans:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                head_video.validate_plan(plan, 5, (128, 128))
        head_video.validate_plan(self.plan(), 5, (128, 128))

    def test_temporal_weights_form_partition_without_cross_shot_anchors(self):
        keys = [10, 20, 35]
        for frame in range(45):
            weights = [head_video.temporal_weight(frame, keys, key) for key in keys]
            self.assertAlmostEqual(sum(weights), 1)
            self.assertTrue(all(0 <= weight <= 1 for weight in weights))
        self.assertEqual(head_video.temporal_weight(10, keys, 10), 1)
        self.assertEqual(head_video.temporal_weight(35, keys, 10), 0)

    def test_blend_preserves_every_pixel_outside_mask(self):
        frame = np.full((64, 64, 3), 60, np.uint8)
        alpha = np.zeros((64, 64), np.float32)
        alpha[20:40, 20:40] = .75
        color = np.full_like(frame, 200, dtype=np.float32) * alpha[:, :, None]
        result = head_video.blend(frame, color, alpha)
        np.testing.assert_array_equal(frame[alpha == 0], result[alpha == 0])
        self.assertTrue(np.all(result[alpha > 0] == 165))

    def test_blend_rejects_invalid_alpha_and_outside_changes(self):
        frame = np.zeros((8, 8, 3), np.uint8)
        for value in (-.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                head_video.blend(frame, frame.astype(np.float32), np.full((8, 8), value))
        with self.assertRaises(PortraitError):
            head_video.blend(frame, np.ones_like(frame, dtype=np.float32) * 10, np.zeros((8, 8)))

    def test_transport_composes_coordinates_instead_of_blurring_texture(self):
        frames = [np.full((64, 64, 3), i, np.uint8) for i in range(3)]
        texture = np.zeros_like(frames[0])
        texture[25, 25] = 255
        mask = np.zeros((64, 64), np.uint8)
        mask[15:40, 15:40] = 255
        yy, xx = np.mgrid[:64, :64].astype(np.float32)

        def mapping(previous, current):
            shift = int(previous[0, 0, 0]) - int(current[0, 0, 0])
            return np.dstack((xx + shift, yy)), np.ones_like(mask, bool)

        with patch.object(head_video, "backward_map", side_effect=mapping):
            outputs = {i: (image, region) for i, image, region, _ in head_video.transport(
                frames, 1, 0, 3, texture, mask,
            )}
        self.assertEqual(set(outputs), {0, 1, 2})
        for i, (image, region) in outputs.items():
            self.assertTrue(np.all(image[25, 25 + i - 1] == 255))
            self.assertEqual(np.count_nonzero(region), np.count_nonzero(mask))

    def test_transport_fails_explicitly_when_tracking_is_unreliable(self):
        frame = np.zeros((64, 64, 3), np.uint8)
        mask = np.full((64, 64), 255, np.uint8)
        yy, xx = np.mgrid[:64, :64].astype(np.float32)
        with patch.object(head_video, "backward_map", return_value=(
            np.dstack((xx, yy)), np.zeros_like(mask, bool),
        )):
            with self.assertRaisesRegex(PortraitError, "跟踪失败"):
                list(head_video.transport([frame, frame], 0, 0, 2, frame, mask))

    def test_real_dis_tracks_small_translation(self):
        rng = np.random.default_rng(7)
        previous = rng.integers(0, 256, (128, 128, 3), np.uint8)
        current = cv2.warpAffine(previous, np.float32([[1, 0, 4], [0, 1, 0]]), (128, 128))
        mapping, valid = head_video.backward_map(previous, current)
        yy, xx = np.mgrid[:128, :128]
        self.assertAlmostEqual(float(np.median(mapping[20:100, 20:100, 0] - xx[20:100, 20:100])), -4, delta=.8)
        self.assertGreater(float(np.mean(valid[20:100, 20:100])), .8)

    def test_full_export_preserves_frames_audio_and_non_head_shot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            executable = imageio_ffmpeg.get_ffmpeg_exe()
            run_ffmpeg(executable, [
                "-n", "-f", "lavfi", "-i", "color=c=gray:s=128x128:r=24:d=0.25",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(source),
            ])
            info = video_info(source)
            plan = self.plan()
            plan["shots"][1]["stop"] = info["frames"]
            plan.update(source_sha256=file_record(source)["sha256"], fps=24.0)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            mask = np.zeros((128, 128), np.uint8)
            mask[25:100, 25:100] = 255
            texture = np.full((128, 128, 3), 230, np.uint8)
            output = root / "result"
            before = file_record(source)
            with patch.object(head_video, "generate_keys", return_value={1: (texture, mask)}), patch.object(
                head_video, "safety_check_frames",
            ) as safety:
                head_video.render_video(source, plan_path, output, root / "cache", threads=1)
            safety.assert_called_once()
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["processing"]["frames"], info["frames"])
            self.assertEqual(report["processing"]["edited_frames"], 3)
            self.assertEqual(report["processing"]["preserved_frames"], info["frames"] - 3)
            self.assertEqual(head_video.audio_hash(executable, source), head_video.audio_hash(executable, output / "head-only.mp4"))
            self.assertEqual(file_record(source), before)
            self.assertEqual(video_info(output / "head-only.mp4", decode=True)["frames"], info["frames"])
            for record in report["files"]:
                head_video.verify_record(output / record["file"], record)
            with patch.object(head_video, "generate_keys", return_value={1: (texture, mask)}), patch.object(
                head_video, "safety_check_frames",
            ) as safety:
                head_video.render_video(
                    source, plan_path, root / "reviewed", root / "cache", threads=1,
                    reviewed_source_shots=True,
                )
            self.assertEqual(safety.call_args.args[1], [0, 1, 2])
            reviewed = json.loads((root / "reviewed" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(reviewed["safety_review"]["automated_checked_frames"], 3)
            self.assertEqual(reviewed["safety_review"]["source_frames_manually_reviewed"], info["frames"] - 3)
            with self.assertRaises(FileExistsError):
                head_video.render_video(source, plan_path, output, root / "cache")

    def test_preserved_shot_cannot_also_supply_generation_keys(self):
        plan = copy.deepcopy(self.plan())
        plan["shots"][1]["keys"] = [{"frame": 4}]
        with self.assertRaises(ValueError):
            head_video.validate_plan(plan, 5, (128, 128))

    def test_shot_replacement_requires_separate_confirmation_and_valid_source_range(self):
        plan = {
            "rights_confirmed": True, "masks_manually_reviewed": True,
            "shots": [{"start": 0, "stop": 5, "replace_with_start": 0}],
        }
        with self.assertRaises(ValueError):
            head_video.validate_plan(plan, 5, (128, 128))
        plan["shot_replacement_confirmed"] = True
        head_video.validate_plan(plan, 5, (128, 128))
        for value in (-1, 1, True):
            invalid = copy.deepcopy(plan)
            invalid["shots"][0]["replace_with_start"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                head_video.validate_plan(invalid, 5, (128, 128))

    def test_replacement_uses_exact_source_frames_and_preserves_duration_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            executable = imageio_ffmpeg.get_ffmpeg_exe()
            run_ffmpeg(executable, [
                "-n", "-f", "lavfi", "-i", "testsrc2=s=128x128:r=24:d=0.25",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(source),
            ])
            plan = {
                "rights_confirmed": True, "masks_manually_reviewed": True,
                "source_sha256": file_record(source)["sha256"], "fps": 24.0,
                "shot_replacement_confirmed": True,
                "shots": [
                    {"start": 0, "stop": 3, "replace_with_start": 3},
                    {"start": 3, "stop": 6, "preserve": True},
                ],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            original = []
            capture = cv2.VideoCapture(str(source))
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    original.append(frame)
            finally:
                capture.release()
            self.assertFalse(np.array_equal(original[0], original[3]))
            with patch.object(head_video, "generate_keys", return_value={}), patch.object(
                head_video, "safety_check_frames",
            ) as safety:
                head_video.render_video(source, plan_path, root / "result", root / "cache", 1)
            checked = safety.call_args.args[0]
            for i in range(3):
                np.testing.assert_array_equal(checked[i], original[i + 3])
                np.testing.assert_array_equal(checked[i + 3], original[i + 3])
            report = json.loads((root / "result" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["processing"]["replaced_frames"], 3)
            self.assertEqual(report["processing"]["head_edited_frames"], 0)
            self.assertEqual(report["validation"]["frames"], 6)
            self.assertEqual(
                head_video.audio_hash(executable, source),
                head_video.audio_hash(executable, root / "result" / "head-only.mp4"),
            )

    def test_audio_validation_failure_does_not_publish_partial_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            run_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), [
                "-n", "-f", "lavfi", "-i", "color=c=gray:s=128x128:r=24:d=0.25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ])
            plan = self.plan()
            plan["shots"][1]["stop"] = video_info(source)["frames"]
            plan.update(source_sha256=file_record(source)["sha256"], fps=24.0)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            mask = np.zeros((128, 128), np.uint8)
            mask[25:100, 25:100] = 255
            texture = np.full((128, 128, 3), 230, np.uint8)
            output = root / "result"
            with patch.object(head_video, "generate_keys", return_value={1: (texture, mask)}), patch.object(
                head_video, "safety_check_frames",
            ), patch.object(head_video, "audio_hash", side_effect=["original", "changed"]):
                with self.assertRaisesRegex(PortraitError, "音频"):
                    head_video.render_video(source, plan_path, output, root / "cache", threads=1)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".head-video-*")), [])

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) for name in ("torch", "psutil", "PIL")),
        "Install generative dependencies for keyframe tests",
    )
    def test_generation_is_head_cropped_but_safety_checks_full_frame_and_cache_is_verified(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source record fixture")
            approved = root / "approved"
            approved.mkdir()
            frame = np.full((128, 128, 3), 50, np.uint8)
            head_video.save_png(approved / "generated.png", frame)
            reference = root / "reference.png"
            head_video.save_png(reference, frame)
            report = {
                "rights_confirmed": True, "safety_check_passed": True,
                "source": file_record(source),
                "identity_reference": {"path": str(reference), "record": file_record(reference)},
                "files": [file_record(approved / "generated.png")],
            }
            (approved / "report.json").write_text(json.dumps(report), encoding="utf-8")
            plan = {
                "approved_still": str(approved), "identity_crop": [10, 10, 64, 64],
                "shots": [{"start": 0, "stop": 1, "keys": [{
                    "frame": 0, "polygon": [[40, 40], [90, 40], [90, 100], [40, 100]],
                }]}],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            def infer(**kwargs):
                self.assertEqual(kwargs["image"][0].size, (kwargs["width"], kwargs["height"]))
                return SimpleNamespace(images=[Image.new(
                    "RGB", (kwargs["width"], kwargs["height"]), (200, 200, 200),
                )])

            cache = root / "cache"
            with patch.object(head_video, "load_pipeline", return_value=infer) as loader, patch.object(
                head_video, "validate_model",
            ), patch.object(head_video, "check_safety") as safety, patch(
                "psutil.virtual_memory", return_value=SimpleNamespace(available=40 * 1024 ** 3),
            ):
                outputs = head_video.generate_keys(source, [frame], plan_path, plan, cache, 1)
                loader.assert_called_once()
                safety.assert_called_once()
                self.assertEqual(safety.call_args.args[0].shape, frame.shape)
            generated, mask = outputs[0]
            np.testing.assert_array_equal(generated[mask == 0], frame[mask == 0])
            key_report = json.loads((cache / "0" / "report.json").read_text(encoding="utf-8"))
            self.assertLess(key_report["parameters"]["generation_crop"][2], frame.shape[1])
            with patch.object(head_video, "load_pipeline") as loader:
                replay = head_video.generate_keys(source, [frame], plan_path, plan, cache, 1)
                loader.assert_not_called()
            np.testing.assert_array_equal(replay[0][0], generated)
            with (cache / "0" / "generated.png").open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(PortraitError, "完整性"):
                head_video.generate_keys(source, [frame], plan_path, plan, cache, 1)

    def test_manual_similarity_alignment_preserves_outside_head_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = np.full((128, 128, 3), 50, np.uint8)
            template = np.full_like(original, 200)
            head_video.save_png(root / "generated.png", template)
            mask = np.zeros((128, 128), np.uint8)
            mask[25:95, 25:95] = 255
            path = root / "alignment.json"
            spec = {
                "landmarks_manually_reviewed": True,
                "keys": [{
                    "frame": 0, "asset": "approved",
                    "source_points": [[30, 30], [60, 30]],
                    "target_points": [[40, 35], [70, 35]],
                    "foreground_polygon": [[35, 35], [85, 35], [85, 90], [35, 90]],
                }],
            }
            path.write_text(json.dumps(spec), encoding="utf-8")
            keys, metrics = head_video.align_keys([original], {0: (template, mask)}, root, root, path)
            np.testing.assert_array_equal(keys[0][0][mask == 0], original[mask == 0])
            self.assertLess(metrics["0"]["mean_landmark_error_pixels"], .001)
            self.assertAlmostEqual(metrics["0"]["scale"], 1)
            self.assertTrue(metrics["0"]["old_hair_background_inpaint"])
            spec["keys"][0]["target_points"][0][0] = -1
            path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaises(ValueError):
                head_video.align_keys([original], {0: (template, mask)}, root, root, path)

    def test_pose_switch_does_not_blend_two_different_faces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            run_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), [
                "-n", "-f", "lavfi", "-i", "color=c=gray:s=128x128:r=24:d=0.25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ])
            plan = {
                "rights_confirmed": True, "masks_manually_reviewed": True,
                "source_sha256": file_record(source)["sha256"], "fps": 24.0,
                "approved_still": str(root),
                "shots": [{"start": 0, "stop": 6, "keys": [
                    {"frame": i, "polygon": [[25, 25], [100, 25], [100, 100], [25, 100]]}
                    for i in (1, 4)
                ]}],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            alignment = root / "alignment.json"
            alignment.write_text(json.dumps({
                "landmarks_manually_reviewed": True, "keys": [],
                "pose_switch_frames": {"0": [3]},
            }), encoding="utf-8")
            mask = np.zeros((128, 128), np.uint8)
            mask[25:100, 25:100] = 255
            keys = {i: (np.full((128, 128, 3), color, np.uint8), mask) for i, color in ((1, 230), (4, 180))}
            with patch.object(head_video, "generate_keys", return_value=keys), patch.object(
                head_video, "safety_check_frames",
            ) as safety:
                head_video.render_video(source, plan_path, root / "out", root / "cache", 1, alignment)
            frames = safety.call_args.args[0]
            self.assertTrue(np.all(frames[2][64, 64] == 230))
            self.assertTrue(np.all(frames[3][64, 64] == 180))


if __name__ == "__main__":
    unittest.main()
