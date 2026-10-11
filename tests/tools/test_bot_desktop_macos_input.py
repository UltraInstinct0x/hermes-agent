"""Mac Bot Screen input mapping: RFB pointer and key events become cua-driver desktop-scope gestures at native
pixel coordinates; hover moves coalesce; typing is batched; a failing injection never kills the driver."""

from __future__ import annotations

from tools.bot_desktop.macos.cua import CuaError
from tools.bot_desktop.macos.input import InputDriver, coalesce, keysym_char
from tests.tools._macos_bot_screen_fakes import FakeCua

LEFT, MIDDLE, RIGHT = 1, 2, 4
WHEEL_UP, WHEEL_DOWN = 8, 16


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _driver(scale=(2.0, 2.0)):
    cua, clock = FakeCua(), _Clock()
    return InputDriver(cua, lambda: scale, clock=clock), cua, clock


def test_a_click_is_one_native_pixel_click_at_the_press_point():
    driver, cua, _ = _driver()
    driver.process([("pointer", LEFT, 100, 50), ("pointer", 0, 101, 51)])
    assert cua.acted() == [("click", 200, 100, "left", 1)]


def test_press_move_release_is_one_drag_not_a_click():
    driver, cua, _ = _driver()
    driver.process([("pointer", LEFT, 10, 10), ("pointer", LEFT, 40, 30), ("pointer", LEFT, 80, 60), ("pointer", 0, 80, 60)])
    assert cua.acted() == [("drag", 20, 20, 160, 120, "left")]


def test_a_second_click_inside_400ms_is_a_double_click():
    driver, cua, clock = _driver()
    driver.process([("pointer", LEFT, 5, 5), ("pointer", 0, 5, 5)])
    clock.now += 0.2
    driver.process([("pointer", LEFT, 6, 5), ("pointer", 0, 6, 5)])
    clock.now += 2
    driver.process([("pointer", LEFT, 6, 5), ("pointer", 0, 6, 5)])
    assert [c[4] for c in cua.acted()] == [1, 2, 1]


def test_right_and_middle_buttons_click_with_their_own_button():
    driver, cua, _ = _driver((1.0, 1.0))
    driver.process([("pointer", RIGHT, 7, 8), ("pointer", 0, 7, 8), ("pointer", MIDDLE, 9, 9), ("pointer", 0, 9, 9)])
    assert [(c[3]) for c in cua.acted()] == ["right", "middle"]


def test_wheel_notches_scroll_at_the_pointer_once_per_press_edge():
    driver, cua, _ = _driver((1.0, 1.0))
    driver.process([("pointer", WHEEL_DOWN, 30, 40), ("pointer", 0, 30, 40), ("pointer", WHEEL_UP, 30, 40), ("pointer", 0, 30, 40)])
    scrolls = [c for c in cua.acted() if c[0] == "scroll"]
    assert scrolls == [("scroll", 30, 40, "down", 2), ("scroll", 30, 40, "up", 2)]


def test_hover_moves_coalesce_to_the_latest_position():
    driver, cua, _ = _driver((1.0, 1.0))
    driver.process([("pointer", 0, i, i) for i in range(1, 30)])
    assert cua.acted() == [("move", 29, 29)]


def test_scale_maps_framebuffer_pixels_to_native_pixels():
    driver, cua, _ = _driver((2.296875, 2.298077))
    driver.process([("pointer", LEFT, 640, 416), ("pointer", 0, 640, 416)])
    assert cua.acted() == [("click", 1470, 956, "left", 1)]


def test_printable_keys_are_typed_as_one_string_and_key_ups_are_ignored():
    driver, cua, _ = _driver()
    events = []
    for ch in "hi!":
        events += [("key", True, ord(ch)), ("key", False, ord(ch))]
    driver.process(events)
    assert cua.acted() == [("type_text", "hi!")]


def test_shift_does_not_turn_typing_into_a_shortcut_but_ctrl_and_cmd_do():
    driver, cua, _ = _driver()
    driver.process([("key", True, 0xFFE1), ("key", True, ord("A")), ("key", False, ord("A")), ("key", False, 0xFFE1)])
    driver.process([("key", True, 0xFFE3), ("key", True, ord("c")), ("key", False, ord("c")), ("key", False, 0xFFE3)])
    driver.process([("key", True, 0xFFEB), ("key", True, ord("V")), ("key", False, ord("V")), ("key", False, 0xFFEB)])
    assert cua.acted() == [("type_text", "A"), ("press_key", "c", ["ctrl"]), ("press_key", "v", ["cmd"])]


def test_special_keys_use_press_key_with_held_modifiers():
    driver, cua, _ = _driver()
    driver.process([("key", True, 0xFF0D), ("key", True, 0xFFE9), ("key", True, 0xFF51), ("key", False, 0xFF51), ("key", False, 0xFFE9),
                    ("key", True, 0xFF08), ("key", True, 0xFFFF)])
    assert cua.acted() == [("press_key", "return", []), ("press_key", "left", ["option"]),
                           ("press_key", "delete", []), ("press_key", "delete", ["fn"])]


def test_unmapped_keysyms_are_ignored():
    driver, cua, _ = _driver()
    driver.process([("key", True, 0xFF67), ("key", True, 0x1008FF11)])
    assert cua.acted() == []


def test_unicode_keysyms_type_the_character():
    assert keysym_char(0x01000000 + ord("ğ")) == "ğ" and keysym_char(0xE9) == "é" and keysym_char(0xFF0D) is None


def test_coalesce_keeps_every_button_transition_and_drops_only_hover():
    events = [("pointer", 0, 1, 1), ("pointer", 0, 2, 2), ("pointer", LEFT, 2, 2), ("pointer", LEFT, 3, 3), ("pointer", 0, 3, 3)]
    assert coalesce(events) == [("pointer", 0, 2, 2), ("pointer", LEFT, 2, 2), ("pointer", LEFT, 3, 3), ("pointer", 0, 3, 3)]


def test_a_failed_injection_is_counted_and_the_next_event_still_runs():
    driver, cua, _ = _driver((1.0, 1.0))
    original = cua.click

    def flaky(*a, **k):
        cua.click = original
        raise CuaError("daemon busy")
    cua.click = flaky
    driver.process([("pointer", LEFT, 1, 1), ("pointer", 0, 1, 1), ("pointer", LEFT, 9, 9), ("pointer", 0, 9, 9)])
    assert driver.errors == 1 and cua.acted() == [("click", 9, 9, "left", 1)]


def test_activity_callback_reports_what_was_injected():
    seen = []
    cua = FakeCua()
    driver = InputDriver(cua, lambda: (1.0, 1.0), on_activity=seen.append)
    driver.process([("pointer", LEFT, 1, 1), ("pointer", 0, 1, 1), ("key", True, ord("a"))])
    assert seen == ["click", "keys"]
