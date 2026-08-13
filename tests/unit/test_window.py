# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from ctypes import wintypes
from unittest.mock import Mock, patch

from tests.support import backend as _backend  # noqa: F401

import window as window_module


class WindowEnumerationTests(unittest.TestCase):
    def test_empty_enum_result_with_no_last_error_is_retryable_absence(self) -> None:
        user32 = Mock()
        user32.EnumWindows.return_value = False
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module.ctypes, "set_last_error") as clear_error,
            patch.object(window_module.ctypes, "get_last_error", return_value=0),
        ):
            self.assertEqual(window_module.list_top_windows(10), [])
        clear_error.assert_called_once_with(0)

    def test_enum_result_with_last_error_remains_fatal(self) -> None:
        user32 = Mock()
        user32.EnumWindows.return_value = False
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module.ctypes, "set_last_error"),
            patch.object(window_module.ctypes, "get_last_error", return_value=5),
            self.assertRaisesRegex(OSError, "EnumWindows failed: 5"),
        ):
            window_module.list_top_windows(10)


class WindowRoutingTests(unittest.TestCase):
    def test_htclose_posts_one_close_intent(self) -> None:
        rect = wintypes.RECT(10, 20, 110, 120)
        geometry = {
            "width": 100,
            "height": 100,
            "client": {"x": 4, "y": 30, "width": 92, "height": 66},
            "dpi": 96,
        }
        posted: list[tuple[int, int, int, int]] = []
        with (
            patch.object(window_module, "_window_snapshot", return_value=(geometry, rect)) as snapshot,
            patch.object(window_module, "hit_test", return_value=window_module.HTCLOSE),
            patch.object(window_module, "_activate"),
            patch.object(
                window_module,
                "_post",
                side_effect=lambda hwnd, message, wparam, lparam: posted.append(
                    (hwnd, message, wparam, lparam)
                ),
            ),
        ):
            result = window_module.click(7, 90, 5)

        snapshot.assert_called_once_with(7)
        self.assertEqual(
            [item[1] for item in posted],
            [window_module.WM_NCMOUSEMOVE, window_module.WM_SYSCOMMAND],
        )
        self.assertEqual(posted[1][2], window_module.SC_CLOSE)
        self.assertEqual(result["system_command"], "close")
        self.assertEqual(result["point"]["target_space"], "screen")
        self.assertEqual(result["window"]["hwnd"], 7)

    def test_unrelated_child_hwnd_is_rejected(self) -> None:
        user32 = Mock()
        user32.ChildWindowFromPointEx.return_value = 22
        user32.IsWindow.return_value = True
        user32.GetParent.return_value = 999
        with (
            patch.object(window_module, "user32", user32),
            self.assertRaisesRegex(LookupError, "child window changed"),
        ):
            window_module._deepest_child(7, wintypes.POINT(1, 1))


class FindWindowFilterTests(unittest.TestCase):
    """PID, visibility, class, and title filtering in window listing and lookup."""

    def test_list_top_windows_filters_by_pid(self) -> None:
        user32 = Mock()
        user32.EnumWindows.side_effect = lambda proc, _: (
            [proc(v, 0) for v in (100, 200)] and True
        )
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 0
        with (
            patch.object(window_module, "user32", user32),
            patch.object(
                window_module, "_get_pid", side_effect=lambda h: 10 if h == 100 else 20
            ),
            patch.object(
                window_module,
                "window_geometry",
                return_value={
                    "width": 100,
                    "height": 100,
                    "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                    "dpi": 96,
                },
            ),
            patch.object(window_module, "_class_name", return_value="W"),
            patch.object(window_module, "_title", return_value=""),
        ):
            windows = window_module.list_top_windows(10)
        self.assertEqual([w["hwnd"] for w in windows], [100])

    def test_list_top_windows_filters_invisible(self) -> None:
        user32 = Mock()
        user32.EnumWindows.side_effect = lambda proc, _: (
            [proc(v, 0) for v in (100, 200)] and True
        )
        user32.IsWindowVisible.side_effect = lambda h: h != 100
        user32.GetParent.return_value = 0
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module, "_get_pid", return_value=10),
            patch.object(
                window_module,
                "window_geometry",
                return_value={
                    "width": 100,
                    "height": 100,
                    "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                    "dpi": 96,
                },
            ),
            patch.object(window_module, "_class_name", return_value="W"),
            patch.object(window_module, "_title", return_value=""),
        ):
            windows = window_module.list_top_windows(10)
        self.assertEqual([w["hwnd"] for w in windows], [200])

    def test_class_filter_matches(self) -> None:
        windows = [
            {
                "hwnd": 7,
                "class": "Edit",
                "title": "",
                "width": 100,
                "height": 100,
                "area": 10000,
                "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                "dpi": 96,
            },
            {
                "hwnd": 8,
                "class": "Notepad",
                "title": "",
                "width": 200,
                "height": 100,
                "area": 20000,
                "client": {"x": 0, "y": 0, "width": 200, "height": 100},
                "dpi": 96,
            },
        ]
        with patch.object(window_module, "list_top_windows", return_value=windows):
            result = window_module.find_window_in_pids([10], window_class="Notepad")
        self.assertEqual(result["hwnd"], 8)

    def test_class_mismatch_raises(self) -> None:
        windows = [
            {
                "hwnd": 7,
                "class": "Edit",
                "title": "",
                "width": 100,
                "height": 100,
                "area": 10000,
                "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                "dpi": 96,
            },
        ]
        with (
            patch.object(window_module, "list_top_windows", return_value=windows),
            self.assertRaisesRegex(LookupError, "no window in pids"),
        ):
            window_module.find_window_in_pids([10], window_class="Notepad")

    def test_title_filter_matches(self) -> None:
        windows = [
            {
                "hwnd": 7,
                "class": "Notepad",
                "title": "Untitled - Notepad",
                "width": 100,
                "height": 100,
                "area": 10000,
                "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                "dpi": 96,
            },
            {
                "hwnd": 8,
                "class": "Notepad",
                "title": "Document - Notepad",
                "width": 200,
                "height": 100,
                "area": 20000,
                "client": {"x": 0, "y": 0, "width": 200, "height": 100},
                "dpi": 96,
            },
        ]
        with patch.object(window_module, "list_top_windows", return_value=windows):
            result = window_module.find_window_in_pids(
                [10], title_contains="Document"
            )
        self.assertEqual(result["hwnd"], 8)

    def test_title_mismatch_raises(self) -> None:
        windows = [
            {
                "hwnd": 7,
                "class": "Notepad",
                "title": "Untitled - Notepad",
                "width": 100,
                "height": 100,
                "area": 10000,
                "client": {"x": 0, "y": 0, "width": 100, "height": 100},
                "dpi": 96,
            },
        ]
        with (
            patch.object(window_module, "list_top_windows", return_value=windows),
            self.assertRaisesRegex(LookupError, "no window in pids"),
        ):
            window_module.find_window_in_pids([10], title_contains="Document")

    def test_no_window_in_pids_raises(self) -> None:
        with (
            patch.object(window_module, "list_top_windows", return_value=[]),
            self.assertRaisesRegex(LookupError, "no window in pids"),
        ):
            window_module.find_window_in_pids([10])


class ExpectedHwndTests(unittest.TestCase):
    """Known HWND lookup enforces ownership, shape, and filters."""

    geometry = {
        "width": 200,
        "height": 100,
        "client": {"x": 0, "y": 0, "width": 180, "height": 80},
        "dpi": 96,
    }

    def test_expected_hwnd_matching_pid_visibility_top_level_and_filters_is_returned(
        self,
    ) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 0
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module, "_get_pid", return_value=10),
            patch.object(window_module, "_class_name", return_value="Notepad"),
            patch.object(window_module, "_title", return_value="Document - Notepad"),
            patch.object(window_module, "window_geometry", return_value=self.geometry),
            patch.object(window_module, "list_top_windows") as list_windows,
        ):
            result = window_module.find_window_in_pids(
                [10, 20],
                expected_hwnd=100,
                window_class="Notepad",
                title_contains="Document",
            )

        self.assertEqual(result["hwnd"], 100)
        self.assertEqual(result["pid"], 10)
        self.assertEqual(result["area"], 180 * 80)
        list_windows.assert_not_called()

    def test_expected_hwnd_rejects_wrong_pid(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 0
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module, "_get_pid", return_value=30),
            self.assertRaisesRegex(LookupError, "belongs to pid 30"),
        ):
            window_module.find_window_in_pids([10, 20], expected_hwnd=100)

    def test_expected_hwnd_rejects_invisible_window(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = False
        with (
            patch.object(window_module, "user32", user32),
            self.assertRaisesRegex(LookupError, "is not visible"),
        ):
            window_module.find_window_in_pids([10], expected_hwnd=100)

    def test_expected_hwnd_rejects_non_top_level_window(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 99
        with (
            patch.object(window_module, "user32", user32),
            self.assertRaisesRegex(LookupError, "is not a top-level window"),
        ):
            window_module.find_window_in_pids([10], expected_hwnd=100)

    def test_expected_hwnd_rejects_class_and_title_filter_mismatches(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 0
        cases = (
            ({"window_class": "Dialog"}, "class 'Notepad' != 'Dialog'"),
            ({"title_contains": "Missing"}, "does not contain 'Missing'"),
        )
        for filters, message in cases:
            with self.subTest(filters=filters):
                with (
                    patch.object(window_module, "user32", user32),
                    patch.object(window_module, "_get_pid", return_value=10),
                    patch.object(window_module, "_class_name", return_value="Notepad"),
                    patch.object(
                        window_module,
                        "_title",
                        return_value="Document - Notepad",
                    ),
                    self.assertRaisesRegex(LookupError, message),
                ):
                    window_module.find_window_in_pids(
                        [10], expected_hwnd=100, **filters
                    )


class HwndReuseRejectionTests(unittest.TestCase):
    """Window identity validation catches stale or foreign HWND values."""

    def test_window_snapshot_rejects_dead_hwnd(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = False
        with (
            patch.object(window_module, "user32", user32),
            self.assertRaisesRegex(LookupError, "window no longer exists"),
        ):
            window_module._window_snapshot(7)

    def test_is_descendant_rejects_non_descendant(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.GetParent.return_value = 0
        with patch.object(window_module, "user32", user32):
            self.assertFalse(window_module._is_descendant(100, 200))

    def test_is_descendant_accepts_direct_child(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.GetParent.side_effect = lambda h: 100 if h == 200 else 0
        with patch.object(window_module, "user32", user32):
            self.assertTrue(window_module._is_descendant(100, 200))

    def test_verify_window_in_pids_rejects_hwnd_after_pid_changes(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.IsWindowVisible.return_value = True
        user32.GetParent.return_value = 0
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module, "_get_pid", side_effect=[10, 30]),
        ):
            self.assertTrue(window_module.verify_window_in_pids(100, [10, 20]))
            self.assertFalse(window_module.verify_window_in_pids(100, [10, 20]))

    def test_verify_window_in_pids_rejects_dead_hwnd(self) -> None:
        user32 = Mock()
        user32.IsWindow.return_value = False
        with patch.object(window_module, "user32", user32):
            self.assertFalse(window_module.verify_window_in_pids(100, [10, 20]))


class KeyVkValidationTests(unittest.TestCase):
    """key() vk parameter type and range enforcement."""

    def test_missing_vk_and_name_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "vk or name required"):
            window_module.key(7)

    def test_unknown_key_name_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown key name"):
            window_module.key(7, name="nonexistent")

    def test_valid_vk_code_works(self) -> None:
        with (
            patch.object(window_module, "_activate"),
            patch.object(window_module, "_send") as send,
        ):
            window_module.key(7, vk=0x0D)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0][0][2], 0x0D)

    def test_valid_key_name_works(self) -> None:
        with (
            patch.object(window_module, "_activate"),
            patch.object(window_module, "_send") as send,
        ):
            window_module.key(7, name="return")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0][0][2], 0x0D)


class TypeTextTests(unittest.TestCase):
    def test_type_sends_wm_char_and_return_for_newlines(self) -> None:
        sent: list[tuple[int, int, int]] = []
        user32 = Mock()
        user32.GetFocus.return_value = 22
        with (
            patch.object(window_module, "user32", user32),
            patch.object(window_module, "_input_attached") as attached,
            patch.object(window_module, "_is_descendant", return_value=True),
            patch.object(
                window_module,
                "_send",
                side_effect=lambda hwnd, message, wparam, lparam: sent.append(
                    (hwnd, message, wparam)
                ),
            ),
        ):
            attached.return_value.__enter__ = Mock(return_value=None)
            attached.return_value.__exit__ = Mock(return_value=False)
            result = window_module.type_text(7, "A\nB")

        self.assertEqual(result["hwnd"], 7)
        self.assertEqual(result["target_hwnd"], 22)
        self.assertEqual(result["chars"], 3)
        self.assertEqual(
            sent,
            [
                (22, window_module.WM_CHAR, ord("A")),
                (22, window_module.WM_KEYDOWN, 0x0D),
                (22, window_module.WM_KEYUP, 0x0D),
                (22, window_module.WM_CHAR, ord("B")),
            ],
        )

    def test_type_rejects_empty_and_overlong_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "text required"):
            window_module.type_text(7, "")
        with self.assertRaisesRegex(ValueError, "4000"):
            window_module.type_text(7, "x" * 4001)

    def test_bool_vk_raises_type_error(self) -> None:
        with self.assertRaisesRegex(TypeError, "vk must be int"):
            window_module.key(7, vk=True)

    def test_float_vk_raises_type_error(self) -> None:
        with self.assertRaisesRegex(TypeError, "vk must be int"):
            window_module.key(7, vk=0.5)

    def test_vk_zero_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "vk must be in 1..255"):
            window_module.key(7, vk=0)

    def test_vk_256_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "vk must be in 1..255"):
            window_module.key(7, vk=256)


if __name__ == "__main__":
    unittest.main()
