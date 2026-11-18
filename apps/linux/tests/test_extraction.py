"""Deterministic mask/fill/placement edges; no model download or QApplication."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from manimble_linux.extraction import (
    ExtractionState, ImageExtractor, Tool, build_insert_request, save_parts,
)
from manimble_linux.extract_runtime import (
    ExtractionRuntimeError, supported_python, verify_checkpoint,
)
from manimble_linux.image_fill import FillError, inpaint
from manimble_linux.models import Box


class MaskTests(unittest.TestCase):
    def test_rectangle_clipping_padding_threshold_and_alpha(self):
        mask = ImageExtractor.rectangle(20,12,(-1.2,3.2,6.4,4.1))
        self.assertEqual(int(np.count_nonzero(mask)),6*5)
        self.assertEqual(ImageExtractor.bounds(mask),(0,1,8,10))
        rgba = np.full((12,20,4),191,np.uint8)
        mask[4,3] = 128
        part = ImageExtractor.cut_out(Image.fromarray(rgba),mask)
        self.assertEqual(part.box,(0,1,8,10))
        crop = np.asarray(part.image)
        self.assertEqual(int(crop[3,3,3]),191*128//255)
        self.assertTrue(np.all(crop[:,:,:3]==191))
        self.assertEqual(int(crop[0,0,3]),0)
        faint = np.full((12,20),127,np.uint8)
        self.assertIsNone(ImageExtractor.bounds(faint))

    def test_hole_union_threshold_and_square_dilation(self):
        a,b = np.zeros((20,30),np.uint8),np.zeros((20,30),np.uint8)
        a[7,8],b[14,20],a[0,0] = 41,255,40
        hole = ImageExtractor.hole([a,b],30,20)
        self.assertEqual(int(np.count_nonzero(hole)),50)
        self.assertTrue(np.all(hole[5:10,6:11]==255))
        self.assertEqual(int(hole[0,0]),0)

    def test_continuous_stroke_keep_remove_and_history(self):
        state = ExtractionState(Image.new("RGBA",(80,40)))
        state.tool = Tool.keep
        state.begin_stroke()
        state.paint((5,20),(70,20),3)
        state.end_stroke()
        self.assertEqual(len(state.elements),1)
        self.assertTrue(np.all(state.elements[0].mask[20,5:71]==255))
        original = state.elements[0].mask.copy()
        state.selected = None
        state.tool = Tool.remove
        state.begin_stroke()
        state.paint((30,0),(30,40),4)
        state.end_stroke()
        self.assertFalse(np.any(state.elements[0].mask[:,28:32]))
        state.undo()
        np.testing.assert_array_equal(state.elements[0].mask,original)
        state.redo()
        self.assertFalse(np.any(state.elements[0].mask[:,28:32]))
        for _ in range(45):
            state.checkpoint()
        self.assertEqual(len(state.undo_stack),40)

    def test_find_elements_overlap_of_smaller_rejected(self):
        state = ExtractionState(Image.new("RGBA",(30,20)))
        big = ImageExtractor.rectangle(30,20,(2,2,12,12))
        small = ImageExtractor.rectangle(30,20,(4,4,4,4))
        other = ImageExtractor.rectangle(30,20,(20,4,4,4))
        self.assertEqual(state.add_masks([big,small,other],deduplicate=True),2)
        self.assertEqual(ImageExtractor.overlap(big,small),1)
        state.undo()
        self.assertEqual(state.elements,[])
        state.redo()
        self.assertEqual(len(state.elements),2)

    def test_stale_preview_is_rejected(self):
        state = ExtractionState(Image.new("RGBA",(30,20)))
        old = state.generation
        state.add_region((3,3,8,8))
        self.assertFalse(state.accept_preview(old,Image.new("RGBA",(30,20))))
        self.assertEqual(state.filled_for,-1)
        current = state.generation
        self.assertTrue(state.accept_preview(current,state.image))
        state.tool = Tool.keep
        state.begin_stroke()
        self.assertNotEqual(state.filled_for,state.generation)


class PlacementTests(unittest.TestCase):
    def test_non_square_mapping_and_one_undo_payload(self):
        req = build_insert_request("Welcome",4,1,9,Box(100,200,700,400),(300,100),
            [("part1.png",(30,20,90,70)),("part2.png",(0,0,30,10))],"filled.png", "generation")
        self.assertEqual(req["stop"],1)
        self.assertEqual(req["seen"],"generation")
        self.assertEqual(req["replace_image"],{"oid":9,"file":"filled.png"})
        self.assertEqual(req["items"][0],{"kind":"image","file":"part1.png","sx":160,"sy":240,"ex":280,"ey":340})
        without_fill = build_insert_request("Welcome",4,1,9,Box(100,200,700,400),(300,100),[("part.png",(0,0,30,10))])
        self.assertEqual(without_fill["stop"],4)
        self.assertNotIn("replace_image",without_fill)

    def test_assets_collision_safe_and_alpha_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Image.new("RGBA",(30,20),(42,90,150,128))
            mask = ImageExtractor.rectangle(30,20,(5,5,8,6))
            parts,background = save_parts(image,[mask],directory,"picture",image)
            again,_ = save_parts(image,[mask],directory,"picture")
            self.assertNotEqual(parts[0][0],again[0][0])
            with Image.open(Path(directory)/parts[0][0]) as part:
                self.assertEqual(part.getpixel((2,2)),(42,90,150,128))
            self.assertTrue((Path(directory)/background).is_file())


class FillTests(unittest.TestCase):
    def test_deterministic_multiscale_non_hole_bytes_unchanged(self):
        y,x = np.mgrid[:100,:120]
        rgba = np.empty((100,120,4),np.uint8)
        rgba[:,:,0],rgba[:,:,1],rgba[:,:,2],rgba[:,:,3] = x*2,y*2,(x+y)%256,(x*3+y)%256
        hole = np.zeros((100,120),np.uint8)
        hole[44:54,52:62] = 255
        before = rgba.copy()
        a,b = inpaint(rgba,hole),inpaint(rgba,hole)
        np.testing.assert_array_equal(a,b)
        np.testing.assert_array_equal(a[hole==0],before[hole==0])
        np.testing.assert_array_equal(rgba,before)
        self.assertTrue(np.any(a[hole!=0]!=before[hole!=0]))

    def test_no_valid_source_patch_is_explicit_error(self):
        hole = np.ones((12,12),np.uint8)
        hole[:2,:2] = 0
        with self.assertRaisesRegex(FillError,"7×7"):
            inpaint(np.zeros((12,12,4),np.uint8),hole)

    def test_empty_hole_returns_identical_bytes(self):
        rgba = np.arange(8*9*4,dtype=np.uint8).reshape(8,9,4)
        np.testing.assert_array_equal(inpaint(rgba,np.zeros((8,9),bool)),rgba)


class CheckpointTests(unittest.TestCase):
    def test_digest_mismatch_refused_with_small_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/"checkpoint.pt"
            file.write_bytes(b"small pinned weights")
            manifest = {"bytes":file.stat().st_size,"sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
            self.assertEqual(verify_checkpoint(file,manifest),file)
            file.write_bytes(b"small forged weights")
            with self.assertRaisesRegex(ExtractionRuntimeError,"SHA-256 mismatch"):
                verify_checkpoint(file,manifest)
            file.write_bytes(b"short")
            with self.assertRaisesRegex(ExtractionRuntimeError,"size mismatch"):
                verify_checkpoint(file,manifest)

    def test_interpreter_versions(self):
        self.assertTrue(supported_python((3,11,0)))
        self.assertTrue(supported_python((3,14,8)))
        self.assertFalse(supported_python((3,14,1)))
        self.assertFalse(supported_python((3,10,19)))
        self.assertFalse(supported_python((3,15,0)))


if __name__ == "__main__":
    unittest.main()
