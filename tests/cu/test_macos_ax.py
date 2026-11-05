"""macOS 后端层的本机可验证部分。

真正的 AX/CoreGraphics 调用只能在 Mac 上跑（Spec 第 84 条同类：这里不假装验收 macOS 桌面），
但有一类缺陷不需要硬件就能守住：键码表缺键、UTF-16 计数错、帧编码越界、框架缺失时的错误码。
键码表缺键尤其危险——desktop.act 校验通过的键，注入层必须一个不漏地都能按出来。
"""
import base64
import ctypes
import io
import unittest
from unittest import mock

from companion import ax
from companion.desktop import _validate_keys
from companion.security import Denied

# desktop.act 允许的字集：与 _validate_keys 的文法保持一致，逐项喂给注入层。
NAMED_KEYS = ("escape", "tab", "enter", "up", "down", "left", "right", "home", "end",
              "pageup", "pagedown", "backspace", "delete")


def allowed_keys():
    keys = [chr(code) for code in range(ord("a"), ord("z") + 1)]
    keys += [str(digit) for digit in range(10)]
    keys += ["f%s" % number for number in range(1, 13)]
    keys += list(NAMED_KEYS)
    return keys


class KeycodeTableTest(unittest.TestCase):
    def test_every_admissible_key_has_a_virtual_keycode(self):
        missing = [key for key in allowed_keys() if key not in ax.KEYCODES]
        self.assertEqual(missing, [], "注入层缺键码：%s" % ",".join(missing))

    def test_validated_key_combinations_reach_the_injection_layer(self):
        # 校验层放行的组合（修饰键 + 主键）必须能被 press 组装出来，不能运行期才发现缺表。
        for combo in (["enter"], ["shift", "a"], ["ctrl", "alt", "home"],
                      ["ctrl", "f12"], ["shift", "up"], ["alt", "pagedown"], ["ctrl", "m"]):
            _validate_keys(combo)  # 远端能提交
            for key in combo:
                table = ax.MODIFIER_FLAGS if key in ("ctrl", "alt", "shift") else ax.KEYCODES
                self.assertIn(key, table, msg=combo)

    def test_modifier_keys_have_their_own_keycodes(self):
        self.assertEqual(sorted(ax.MODIFIER_KEYCODES), ["alt", "ctrl", "shift"])
        self.assertEqual(ax.MODIFIER_KEYCODES["ctrl"], 0x3B)   # kVK_CONTROL
        self.assertEqual(ax.MODIFIER_KEYCODES["alt"], 0x3A)    # kVK_OPTION
        self.assertEqual(ax.MODIFIER_KEYCODES["shift"], 0x38)  # kVK_SHIFT

    def test_stop_hotkey_is_reachable_and_command_bearing(self):
        self.assertEqual(ax.HOTKEY_KEYCODE, ax.KEYCODES["s"])
        self.assertEqual(ax.HOTKEY_MODIFIERS,
                         ax.FLAG_CONTROL | ax.FLAG_OPTION | ax.FLAG_COMMAND)
        self.assertIn("S", ax.HOTKEY_TEXT_MAC)


class Utf16CountTest(unittest.TestCase):
    def test_unicode_unit_count_matches_utf16(self):
        self.assertEqual(ax._utf16_units("a"), 1)
        self.assertEqual(ax._utf16_units("中"), 1)
        self.assertEqual(ax._utf16_units("\U0001F600"), 2)  # 代理对算两个 UniChar


class FrameEncodeTest(unittest.TestCase):
    @staticmethod
    def bgra(width, height, color):
        stride = width * 4
        rows = bytearray()
        for _ in range(height):
            for _ in range(width):
                rows += bytes(color)  # B, G, R, A
            rows += bytes(stride - width * 4)
        return bytes(rows), stride

    def test_frame_encodes_to_jpeg_with_expected_pixels(self):
        payload, stride = self.bgra(8, 6, (200, 100, 50, 255))
        encoded = ax.encode_frame(payload, 8, 6, stride)
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (8, 6))
        # JPEG 是有损的：只要求颜色通道对得上（BGRA 字节序换算成 RGB），不比特定位。
        red, green, blue = image.convert("RGB").getpixel((4, 3))
        self.assertTrue(abs(red - 50) < 6 and abs(green - 100) < 6 and abs(blue - 200) < 6,
                        (red, green, blue))

    def test_oversized_frame_is_scaled_into_the_budget(self):
        payload, stride = self.bgra(1600, 1200, (10, 20, 30, 255))
        encoded = ax.encode_frame(payload, 1600, 1200, stride, limit=48 * 1024)
        self.assertLessEqual(len(base64.b64decode(encoded)), 48 * 1024)

    def test_short_payload_is_rejected_instead_of_crashing_the_encoder(self):
        # 实机上 CFData 长度够不到 stride*height 时，必须报固定错误码，不能漏 ValueError。
        payload, stride = self.bgra(8, 8, (1, 2, 3, 255))
        with self.assertRaises(Denied) as caught:
            ax.encode_frame(payload[:stride * 2], 8, 8, stride)
        self.assertEqual(str(caught.exception), "desktop_capture_unavailable")


class FrameworkLoadTest(unittest.TestCase):
    def test_missing_framework_reports_its_own_reason(self):
        with self.assertRaises(Denied) as caught:
            ax._framework("DefinitelyNotAFramework")
        self.assertEqual(str(caught.exception), "macos_framework_unavailable")

    def test_structs_use_double_coordinates(self):
        # macOS 的 CGFloat 在 64 位系统是 double；按 float 传会让 Retina 坐标失真。
        self.assertIs(ax.CGPoint._fields_[0][1], ctypes.c_double)
        self.assertIs(ax.CGSize._fields_[0][1], ctypes.c_double)
        self.assertEqual(ctypes.sizeof(ax.CGRect), 32)


class ConstantSourceTest(unittest.TestCase):
    def test_layer_and_window_id_constants_match_the_sdk_documentation(self):
        self.assertEqual(ax.NORMAL_WINDOW_LAYER, 0)
        self.assertEqual(ax.NULL_WINDOW_ID, 0xFFFFFFFF)
        self.assertEqual(ax.WINDOW_LIST_ON_SCREEN | ax.WINDOW_LIST_NO_DESKTOP, 17)

    def test_no_prototype_is_declared_on_the_wrong_framework(self):
        # CFMachPortCreateRunLoopSource 只在 CoreFoundation 导出，挂到 CoreGraphics
        # 会在真机上以 AttributeError 收场；这里用源码扫描守住这条。
        with io.open(ax.__file__, encoding="utf-8") as stream:
            source = stream.read()
        self.assertNotIn("self.cg.CFMachPort", source)
        self.assertIn("self.cf.CFMachPortCreateRunLoopSource", source)


class ElementTreeTest(unittest.TestCase):
    """控件树的取舍与激活核对：这是远端唯一能“点”的东西，错一项就是越权。

    这里替掉 CF/AX 的原始调用，只跑真实的遍历与校验代码；真机上的 AX 行为仍需
    在 Mac 上用 --accept-desktop 实测，这些用例不构成验收。
    """

    TREE = {"root": {"role": "AXWindow"},
            "toolbar": {"role": "AXToolbar"},
            "save": {"role": "AXButton", "title": "保存", "enabled": True},
            "field": {"role": "AXTextField", "title": "地址"},
            "hidden": {"role": "AXButton", "title": "隐藏", "hidden": True,
                      "enabled": True},
            "unnamed": {"role": "AXButton", "enabled": True},
            "copy": {"role": "AXMenuItem", "description": "拷贝", "enabled": True}}
    CHILDREN = {"root": ["toolbar", "field"],
                "toolbar": ["save", "hidden", "unnamed", "copy"]}

    def setUp(self):
        libs = mock.MagicMock()
        # CFRetain 必须是恒等函数，否则“按住的元素”和“遍历到的元素”不是同一个对象。
        libs.cf.CFRetain.side_effect = lambda value: value
        self.backend = ax.AX(libs=libs)
        self.backend._window_element = lambda number: "root"
        self.backend._release = lambda ref: None
        self.backend._children = lambda element, limit=ax.CHILD_LIMIT: list(
            self.CHILDREN.get(element, []))
        self.backend._attribute_text = self._text
        self.backend._attribute_flag = self._flag
        self.pressed = []
        self.press_ok = True

        def perform(element, action):
            self.pressed.append((element, action))
            return self.press_ok

        self.backend._perform = perform

    def _text(self, element, name):
        node = self.TREE.get(element) or {}
        return {"AXRole": node.get("role", ""), "AXTitle": node.get("title", ""),
                "AXDescription": node.get("description", "")}.get(name, "")

    def _flag(self, element, name):
        node = self.TREE.get(element) or {}
        if name == "AXHidden":
            return bool(node.get("hidden"))
        return bool(node.get("enabled"))

    def test_only_actionable_named_visible_controls_are_listed(self):
        items = self.backend.list_elements(701, 80)
        # 文本框（不可激活）、隐藏按钮、无名字按钮都不进列表：核对不了就不能点。
        self.assertEqual([(item["index"], item["role"], item["name"]) for item in items],
                         [(0, "button", "保存"), (1, "menuitem", "拷贝")])

    def test_index_role_and_name_must_all_match_before_pressing(self):
        with self.assertRaises(Denied) as caught:
            self.backend.invoke_element(701, 0, "menuitem", "保存")
        self.assertEqual(str(caught.exception), "desktop_element_changed")
        with self.assertRaises(Denied) as caught:
            self.backend.invoke_element(701, 0, "button", "删除")
        self.assertEqual(str(caught.exception), "desktop_element_changed")
        self.assertEqual(self.pressed, [])

    def test_matching_element_is_pressed_once(self):
        self.backend.invoke_element(701, 1, "menuitem", "拷贝")
        self.assertEqual(self.pressed, [("copy", "AXPress")])

    def test_missing_index_is_reported(self):
        for index in (5, -1):
            with self.assertRaises(Denied) as caught:
                self.backend.invoke_element(701, index, "button", "保存")
            self.assertEqual(str(caught.exception), "desktop_element_missing")
        self.assertEqual(self.pressed, [])

    def test_ax_refusal_is_not_swallowed(self):
        self.press_ok = False
        with self.assertRaises(Denied) as caught:
            self.backend.invoke_element(701, 0, "button", "保存")
        self.assertEqual(str(caught.exception), "desktop_element_not_actionable")

    def test_window_without_ax_is_not_reported_as_empty(self):
        """解析不到 AX 窗口必须报错：当成“没有控件”会把权限缺失误报成应用无可点东西。"""
        self.backend._window_element = lambda number: None
        with self.assertRaises(Denied) as caught:
            self.backend.list_elements(701, 80)
        self.assertEqual(str(caught.exception), "desktop_window_ax_unavailable")

    def test_tree_traversal_is_bounded(self):
        self.TREE["many"] = {"role": "AXWindow"}
        wide = ["b%d" % number for number in range(120)]
        for key in wide:
            self.TREE[key] = {"role": "AXButton", "title": key, "enabled": True}
        children = {"many": wide}
        self.backend._window_element = lambda number: "many"
        self.backend._children = lambda element, limit=ax.CHILD_LIMIT: list(
            children.get(element, []))
        self.assertEqual(len(self.backend.list_elements(702, ax.ELEMENT_LIMIT)),
                         ax.ELEMENT_LIMIT)


if __name__ == "__main__":
    unittest.main()
