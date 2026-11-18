"""Pure geometry, UTF-16 and colour edge cases; no QApplication or engine needed."""
import math
import unittest

from manimble_linux.canvas import CanvasGeometry, Handle, Override, code_points, simplify, utf16
from manimble_linux.darkmode import ColorView, DarkMode
from manimble_linux.models import Box


class CanvasGeometryTests(unittest.TestCase):
    def setUp(self):
        self.c = CanvasGeometry()
        self.c.stop = {"visible": [[0, 100, 100, 300, 200], [1, 150, 120, 180, 140], [2, 500, 100, 600, 200]],
                       "sprites": [{"oid": 0, "z": 0}, {"oid": 2, "z": 2}],
                       "lines": {"3": [300,150,500,150,3]}}
        self.c.scene_objects = {"0": {"kind": "shape", "value": "Rectangle"}, "1": {"kind": "text"},
                                "2": {"kind": "shape", "value": "Circle"}, "3": {"kind": "line", "follows": [0,2]}}

    def test_raw_frame_replacement_and_current_boxes(self):
        self.c.overrides[0] = Override(dx=10,dy=20,scale=2)
        self.assertEqual(self.c.raw_box(0),Box(10,70,410,270))
        self.c.scene_objects["0"]["frame"] = [80,80,320,240]
        self.assertEqual(self.c.frame_box(0),Box(-30,20,450,340))
        self.c.scene_objects["0"]["format"] = {"align":"right","valign":"mid"}
        o = Override(dx=10,dy=20,replacement=(None,50,20))
        self.c.overrides[0] = o
        self.assertEqual(self.c.raw_box(0),Box(271,170,321,190))
        self.assertEqual(self.c.current_box(0),Box(90,100,330,260))
        self.c.stop["visible"].append([4,10,10,12,11])
        self.assertEqual(self.c.current_box(4),Box(6,5.5,16,15.5))

    def test_picking_parent_parts_and_z_order(self):
        self.assertEqual(self.c.objects(),[0,2])
        self.assertEqual(self.c.objects(Box(90,90,305,205)),[0])
        self.assertEqual(self.c.hit((160,130)),1)  # parent labels sit just above its sprite
        self.c.stop["visible"].append([4,155,125,175,145])
        self.c.stop["sprites"].append({"oid":4,"z":10})
        self.assertEqual(self.c.hit((160,130)),4)
        self.assertIsNone(self.c.hit((800,600)))

    def test_nearest_handle_and_snap(self):
        self.assertEqual(self.c.handle_at((100,150),Box(100,145,200,155),True),Handle.w)
        self.assertEqual(self.c.handle_at((202,157),Box(100,145,200,155)),Handle.se)
        self.assertIsNone(self.c.handle_at((150,150),Box(100,100,200,200)))
        dx,dy,guides = self.c.snap_delta(0,Box(100,100,300,200),197,0)
        self.assertEqual((dx,dy),(200,0))
        self.assertIn((True,500),guides)
        self.c.user_guides = [(True,333)]
        dx,_,_ = self.c.snap_delta(0,Box(100,100,300,200),31,0)
        self.assertEqual(dx,33)

    def test_anchor_ellipse_and_similarity_composition(self):
        points = dict((at,(x,y)) for at,x,y in self.c.anchor_points(2))
        self.assertEqual(points["RIGHT"],(600,150))
        self.assertAlmostEqual(points["UL"][0],500+(1-math.sqrt(.5))*50)
        host,anchor = self.c.line_anchor((299,150))
        self.assertEqual((host,anchor.at),(0,"RIGHT"))
        self.assertEqual(self.c.line_anchor((299,150),excluding=0),(None,None))
        self.c.overrides[0] = Override(dx=20,dy=10)
        xf = self.c.follow_xform(3,self.c.stop)
        self.assertAlmostEqual(xf[0],.9)
        self.assertAlmostEqual(xf[1],-.05)
        self.assertAlmostEqual(xf[0]*300+xf[2]*150+xf[4],320)
        self.assertAlmostEqual(xf[1]*300+xf[3]*150+xf[5],160)
        self.c.stop["xforms"] = {"3":[1,0,0,1,10,20]}
        combined = self.c.follow_xform(3,self.c.stop)
        self.assertAlmostEqual(combined[4],xf[0]*10+xf[2]*20+xf[4])
        self.assertAlmostEqual(combined[5],xf[1]*10+xf[3]*20+xf[5])

    def test_rdp_frame_pixel_tolerance(self):
        self.assertEqual(simplify([(0,0),(1,1),(2,0)],1.2),[(0,0),(2,0)])
        self.assertEqual(simplify([(0,0),(1,2),(2,0)],1.2),[(0,0),(1,2),(2,0)])
        self.assertEqual(simplify([(0,0),(0,0),(0,0)]),[(0,0),(0,0)])
        self.assertEqual(simplify([]),[])

    def test_utf16_codepoint_round_trip(self):
        text = "A😀é𝄞Z"
        self.assertEqual([utf16(text,i) for i in range(7)],[0,1,3,4,5,7,8])
        for i in range(len(text)+1):
            self.assertEqual(code_points(text,utf16(text,i)),i)
        self.assertEqual(code_points(text,2),1)
        self.assertEqual(code_points(text,6),4)
        self.assertEqual(utf16(text,999),8)


class DarkModeTests(unittest.TestCase):
    def test_thresholds_and_neutral_lightness(self):
        self.assertEqual((DarkMode.lo,DarkMode.hi),(.12,.28))
        self.assertEqual(DarkMode.map(.2,.2,.2,ColorView.dark),(.8,.8,.8))
        mapped = DarkMode.map(.3,.2,.18,ColorView.dark)
        for actual,expected in zip(mapped,(.82,.72,.70)):
            self.assertAlmostEqual(actual,expected)
        self.assertEqual(DarkMode.map(.5,.22,.22,ColorView.dark),(.5,.22,.22))
        for actual,expected in zip(DarkMode.map(.4,.2,.2,ColorView.dark),(.6,.4,.4)):
            self.assertAlmostEqual(actual,expected)
        self.assertEqual(DarkMode.map(.1,.4,.7,ColorView.normal),(.1,.4,.7))
        self.assertEqual(DarkMode.map(.1,.4,.7,ColorView.invert),(.9,.6,.30000000000000004))


if __name__ == "__main__":
    unittest.main()


class TextTransformTest(unittest.TestCase):
    def test_bullet_and_number_round_trips_preserve_blank_lines(self):
        from manimble_linux.formatbar import TextTransform as T
        text = "Alpha\n    Beta\n\nGamma"
        bullets = T.toggle_bullets(text)
        self.assertEqual(bullets, "• Alpha\n    • Beta\n\n• Gamma")
        self.assertTrue(T.is_bulleted(bullets))
        self.assertEqual(T.toggle_bullets(bullets), text)
        numbers = T.toggle_numbers(bullets)
        self.assertEqual(numbers, "1. Alpha\n    2. Beta\n\n3. Gamma")
        self.assertTrue(T.is_numbered(numbers))
        self.assertEqual(T.toggle_numbers(numbers), text)

    def test_indent_is_four_spaces_and_only_selected_lines(self):
        from manimble_linux.formatbar import TextTransform as T
        self.assertEqual(T.indent("a\n b\nc", (1, 1), 4), "a\n     b\nc")
        self.assertEqual(T.indent("  a\n    b\n\tc", None, -4), "a\nb\n\tc")

    def test_utf16_lines_and_supplementary_characters(self):
        from manimble_linux.formatbar import TextTransform as T
        text = "😀x\n𝄞y\nz"
        self.assertEqual(T.codepoint_offset(text, 2), 1)
        self.assertEqual(T.line_range(text, (4, 4)), (1, 1))
        self.assertEqual(T.line_range(text, (2, 6)), (0, 1))
        self.assertEqual(T.line_range(text, (8, 0)), (2, 2))

    def test_unicode_script_toggle_and_typing_marks(self):
        from manimble_linux.formatbar import TextTransform as T
        self.assertEqual(T.script("x2 + 😀", True), "ˣ² ⁺ 😀")
        self.assertEqual(T.script("ˣ² ⁺ 😀", True), "x2 + 😀")
        self.assertEqual(T.raise_text("a12?", False), "ₐ₁₂?")
        self.assertIsNone(T.script("😀?", True))
        self.assertEqual(T.script("H2O", False), "H₂O")

    def test_body_bullets_and_list_continuation(self):
        from manimble_linux.formatbar import TextTransform as T
        self.assertEqual(T.newline_prefix("    • Item"), "    • ")
        self.assertEqual(T.newline_prefix("12) Item"), "13) ")
        self.assertEqual(T.newline_prefix("• "), "")
        self.assertEqual(T.newline_prefix("Body", body=True), "• ")
        self.assertEqual(T.clean_body("• \n• First\n    \n• Second"), "• First\n• Second")

    def test_hyperlink_normalization_preserves_schemes(self):
        from manimble_linux.slide_tools import HyperlinkMixin as H
        from pathlib import Path
        self.assertEqual(H.normalized_link(" example.com/x "), "https://example.com/x")
        self.assertEqual(H.normalized_link(" mailto:a@b.c "), "mailto:a@b.c")
        self.assertEqual(H.normalized_link("/a path/x"), "file:///a%20path/x")
        self.assertEqual(H.normalized_link("~/x"), (Path.home() / "x").as_uri())
        self.assertEqual(H.normalized_link("  "), "")
