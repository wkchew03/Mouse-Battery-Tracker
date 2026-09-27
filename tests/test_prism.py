"""The drifting field, and the chart series that feeds the panel.

Nothing here opens a device or a window: the field is a pure image function
and the pan schedule is arithmetic over elapsed seconds.
"""

from mbt import prism
from mbt.cards import fade_mask, render_discharge_chart, render_frost, render_icon
from mbt.history import discharge_series


# --------------------------------------------------------------------------
# The field
# --------------------------------------------------------------------------


def test_field_is_rendered_larger_than_the_window():
    """It is panned, not redrawn, so it has to overhang on every side or the
    drift would expose an edge."""
    image = prism.render_field(400, 300)
    assert image.width > 400 and image.height > 300
    assert image.width == int(400 * prism.OVERSCAN)


def test_pan_never_exposes_an_edge():
    """The offset is the field's top-left corner: it must never be positive
    (a gap on the left) nor leave the right edge inside the window."""
    width, height = 900, 600
    field_w = int(width * prism.OVERSCAN)
    field_h = int(height * prism.OVERSCAN)
    for step in range(0, 400):
        x, y = prism.offset_at(step * 0.37, width, height)
        assert x <= 0 and y <= 0
        assert x + field_w >= width
        assert y + field_h >= height


def test_pan_actually_moves():
    """A field that does not move is just a background."""
    seen = {prism.offset_at(t, 900, 600) for t in range(0, 48, 2)}
    assert len(seen) > 8


def test_pan_loops_seamlessly():
    """One period later the field is exactly where it started, so the loop has
    no visible jump."""
    a = prism.offset_at(3.0, 900, 600)
    b = prism.offset_at(3.0 + prism.PERIOD_SECONDS, 900, 600)
    assert a == b


def test_field_is_not_a_flat_wash():
    """The first build blended the blobs instead of compositing them, which
    averaged the colour out to near-grey. Guard the fix: the field has to
    carry real colour variation."""
    image = prism.render_field(400, 300).convert("RGB")
    width, height = image.size
    sampled = [
        image.getpixel((x * width // 8, y * height // 8))
        for x in range(1, 8)
        for y in range(1, 8)
    ]
    spread = max(max(p) - min(p) for p in sampled)
    assert spread > 25, "the field lost its colour separation"


# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------


def test_frost_keeps_its_alpha():
    """The frosting is real: a Tk photo image blends against the canvas item
    beneath it, so the sprite must not arrive pre-flattened."""
    card = render_frost(120, 90, 14, (18, 19, 26, 143), (255, 255, 255, 20))
    assert card.mode == "RGBA"
    assert card.getpixel((60, 45))[3] == 143
    # ...and the rounded corner is cut out entirely.
    assert card.getpixel((0, 0))[3] < 40


def test_fade_runs_from_clear_to_solid():
    """Solid past the ramp: a fade that stays translucent shows the cards
    through it instead of hiding them."""
    fade = fade_mask(40, 30, ramp=20)
    assert fade.getpixel((20, 0)) == 0
    assert 0 < fade.getpixel((20, 10)) < 255
    assert fade.getpixel((20, 20)) == fade.getpixel((20, 29)) == 255

    upward = fade_mask(40, 30, ramp=20, reverse=True)
    assert upward.getpixel((20, 29)) == 0
    assert upward.getpixel((20, 0)) == 255


def test_icons_are_drawn_not_glyphs():
    """A font without the character renders an empty box and says nothing, so
    these are drawn. Both must actually put ink down."""
    for name in ("gear", "folder"):
        icon = render_icon(name, 16, "#777984")
        assert icon.size == (16, 16)
        inked = any(
            icon.getpixel((x, y))[3] > 0 for x in range(16) for y in range(16)
        )
        assert inked, f"{name} drew nothing"


def test_chart_survives_having_nothing_to_plot():
    """A mouse seen once has no series at all; the panel still has to paint."""
    empty = render_discharge_chart(200, 80, [], "#4cbb6a", threshold=15)
    assert empty.size == (200, 80)
    single = render_discharge_chart(200, 80, [(0.0, 80)], "#4cbb6a", threshold=15)
    assert single.size == (200, 80)


def test_chart_marks_the_hovered_point_without_moving_the_line():
    plain = render_discharge_chart(200, 80, [(0.0, 80), (3600.0, 75)], "#4cbb6a")
    hovered = render_discharge_chart(
        200, 80, [(0.0, 80), (3600.0, 75)], "#4cbb6a", hover=0
    )
    assert plain.tobytes() != hovered.tobytes()


# --------------------------------------------------------------------------
# The series behind the chart
# --------------------------------------------------------------------------


def test_series_is_relative_to_the_window_start():
    history = [[1000.0, 80], [4600.0, 78], [8200.0, 75]]
    assert discharge_series(history) == [(0.0, 80), (3600.0, 78), (7200.0, 75)]


def test_series_needs_two_samples():
    assert discharge_series([]) == []
    assert discharge_series([[1000.0, 80]]) == []


def test_series_starts_at_the_last_recharge():
    """Anything before the last rise belongs to a previous cycle, and plotting
    across it would draw a mouse gaining battery."""
    history = [[0.0, 40], [100.0, 30], [200.0, 90], [300.0, 88], [400.0, 86]]
    assert discharge_series(history) == [(0.0, 90), (100.0, 88), (200.0, 86)]


def test_series_keeps_real_time_spacing():
    """Samples are recorded only on
    change, so they are irregular in time. Even spacing would report a mouse
    that sat idle all afternoon with the same slope as one in use."""
    history = [[0.0, 90], [60.0, 89], [36000.0, 88]]
    series = discharge_series(history)
    gaps = [series[i + 1][0] - series[i][0] for i in range(len(series) - 1)]
    assert gaps == [60.0, 35940.0]


def test_series_is_capped_at_the_newest_samples():
    history = [[float(i), 200 - i] for i in range(120)]
    series = discharge_series(history, limit=10)
    assert len(series) == 10
    assert series[-1][1] == 200 - 119
