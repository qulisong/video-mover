import importlib.util
import unittest


AVAILABLE = all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "imageio_ffmpeg"))
if AVAILABLE:
    import numpy as np
    import person_regions
    from portrait_preview import PortraitError


@unittest.skipUnless(AVAILABLE, "Install requirements-portrait.txt")
class PersonRegionTests(unittest.TestCase):
    def setUp(self):
        self.raw = np.zeros((200, 160), np.uint8)
        self.raw[20:195, 40:130] = 1
        self.face = {"box": (50, 30, 60, 60)}

    def test_component_at_face_keeps_visible_clothing_and_excludes_other_components(self):
        self.raw[30:180, :15] = 1
        mask, crop = person_regions.region_from_person_mask(self.raw, self.face)
        self.assertGreater(mask[180, 80], 0)
        self.assertEqual(mask[100, 0], 0)
        x, y, w, h = crop
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 160)
        self.assertLessEqual(y + h, 200)

    def test_face_not_in_person_is_explicit_failure(self):
        self.raw[:] = 0
        with self.assertRaisesRegex(PortraitError, "未覆盖"):
            person_regions.region_from_person_mask(self.raw, self.face)

    def test_tiny_segmentation_is_rejected(self):
        self.raw[:] = 0
        self.raw[58:62, 78:82] = 1
        with self.assertRaisesRegex(PortraitError, "过小"):
            person_regions.region_from_person_mask(self.raw, self.face)

    def test_product_and_text_protection_remove_all_editing_inside_boxes(self):
        mask, _ = person_regions.region_from_person_mask(self.raw, self.face)
        original = mask.copy()
        edited, protected = person_regions.protect_regions(
            mask, [(60, 80, 50, 70), (0, 180, 160, 20)],
        )
        self.assertFalse(np.any(edited[80:150, 60:110]))
        self.assertFalse(np.any(edited[180:]))
        self.assertTrue(np.all(protected[80:150, 60:110] == 255))
        np.testing.assert_array_equal(mask, original)

    def test_bad_or_all_covering_boxes_are_rejected(self):
        mask, _ = person_regions.region_from_person_mask(self.raw, self.face)
        for box in ((-1, 0, 20, 20), (0, 0, 0, 20), (150, 0, 20, 20)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                person_regions.protect_regions(mask, [box])
        with self.assertRaisesRegex(PortraitError, "全部"):
            person_regions.protect_regions(mask, [(0, 0, 160, 200)])

    def test_contour_mask_combines_with_boxes_and_keeps_other_clothing_editable(self):
        mask, _ = person_regions.region_from_person_mask(self.raw, self.face)
        contour = np.zeros_like(mask)
        contour[80:150, 60:75] = 1
        contour[130:150, 75:110] = 255
        edited, protected = person_regions.protect_regions(mask, [(0, 180, 160, 20)], contour)
        self.assertFalse(np.any(edited[contour > 0]))
        self.assertFalse(np.any(edited[180:]))
        self.assertTrue(np.all(protected[contour > 0] == 255))
        self.assertGreater(edited[100, 100], 0)
        self.assertEqual(contour[80, 60], 1)

    def test_invalid_empty_and_all_covering_contour_masks_are_rejected(self):
        mask, _ = person_regions.region_from_person_mask(self.raw, self.face)
        for contour in (np.ones((20, 20), np.uint8), np.zeros_like(mask)):
            with self.subTest(shape=contour.shape), self.assertRaises(ValueError):
                person_regions.protect_regions(mask, [], contour)
        with self.assertRaisesRegex(PortraitError, "全部"):
            person_regions.protect_regions(mask, [], np.ones_like(mask))

    def test_cli_box_parser(self):
        self.assertEqual(person_regions.protected_box("10, 20,30,40"), (10, 20, 30, 40))
        for value in ("1,2,3", "1,2,3,4,5", "x,2,3,4", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                person_regions.protected_box(value)


if __name__ == "__main__":
    unittest.main()
