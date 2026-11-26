import io

import pytest

from tokenwell import splash


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def test_every_frame_has_the_same_row_count():
    assert {len(frame) for frame in splash.FRAMES} == {11}


@pytest.mark.parametrize(
    "env_name, env_value",
    [("CI", "1"), ("TOKENWELL_NO_ANIMATION", "1")],
)
def test_animation_is_gated_by_environment(monkeypatch, env_name, env_value):
    monkeypatch.setenv(env_name, env_value)
    stream = TTYBuffer()
    monkeypatch.setattr(splash.time, "sleep", pytest.fail)

    assert splash.show(stream=stream) is False
    assert stream.getvalue() == ""


def test_animation_is_gated_when_stdout_is_not_a_tty(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(splash.time, "sleep", pytest.fail)

    assert splash.show(stream=stream) is False
    assert stream.getvalue() == ""


def test_animation_is_gated_by_quiet(monkeypatch):
    stream = TTYBuffer()
    monkeypatch.setattr(splash.time, "sleep", pytest.fail)

    assert splash.show(stream=stream, quiet=True) is False
    assert stream.getvalue() == ""


def test_no_color_shows_static_logo_without_any_escape_codes(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    stream = TTYBuffer()
    monkeypatch.setattr(splash.time, "sleep", pytest.fail)

    assert splash.show(port=4401, stream=stream) is True
    output = stream.getvalue()
    assert "tokenwell" in output
    assert "http://127.0.0.1:4401/" in output
    assert "\x1b" not in output


def test_animation_total_frame_delay_is_under_one_second(monkeypatch):
    stream = TTYBuffer()
    delays = []
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TOKENWELL_NO_ANIMATION", raising=False)
    monkeypatch.setattr(splash.time, "sleep", delays.append)
    monkeypatch.setenv("COLORTERM", "truecolor")

    assert splash.show(stream=stream) is True
    assert len(delays) == len(splash.FRAMES)
    assert sum(delays) < 1.0


def test_breaks_the_rim_only_colors_coin_characters(monkeypatch):
    monkeypatch.setenv("COLORTERM", "truecolor")
    rendered = splash.render_frame(splash.FRAMES[3], 3)
    gold = splash.GOLD_24
    water = splash.WATER_24

    rim_top = rendered.splitlines()[6]
    assert "____" in rim_top
    assert gold + "'-'" + splash.RESET in rim_top
    assert water + "____" in rim_top
    assert "____" not in rim_top.split(gold)[-1].split(splash.RESET)[0]
