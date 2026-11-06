import pytest

from trafficsim.clock import SimClock
from trafficsim.patterns import build_pattern, envelope
from trafficsim.timeutil import fmt_duration, iso_ms, parse_duration, parse_start_at


@pytest.mark.parametrize("text,seconds", [
    ("90s", 90), ("15m", 900), ("2h", 7200), ("1d12h", 129600), ("+2h", 7200), ("-24h", -86400),
    ("1h30m", 5400), (45, 45), ("3600", 3600),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "5x", "1h-2m"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_parse_start_at():
    now = 1_700_000_000.0
    assert parse_start_at("now", now) == now
    assert parse_start_at("-24h", now) == now - 86400
    assert parse_start_at("2024-01-01T00:00:00Z") == 1704067200.0
    assert parse_start_at(123.0) == 123.0


def test_iso_and_fmt():
    assert iso_ms(1704067200.5) == "2024-01-01T00:00:00.500+00:00"
    assert fmt_duration(3661).startswith("1h01m")


class FakeWall:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_clock_speed_pause_resume_and_state():
    wall = FakeWall()
    c = SimClock(0.0, speed=60, wall_fn=wall)
    wall.t += 10
    assert c.now() == pytest.approx(600)
    c.pause()
    wall.t += 100
    assert c.now() == pytest.approx(600)
    c.resume()
    c.set_speed(2)
    wall.t += 5
    assert c.now() == pytest.approx(610)
    c2 = SimClock.from_state(c.state(), wall_fn=wall)
    assert c2.now() == pytest.approx(c.now())
    assert c.wall_until(620) == pytest.approx(5)  # wall seconds until sim time 620
    with pytest.raises(ValueError):
        SimClock(0, speed=0)


def test_envelope_shape():
    assert envelope(-1, 0, 10, 10, 10) == 0
    assert envelope(5, 0, 10, 10, 10) == pytest.approx(0.5)
    assert envelope(15, 0, 10, 10, 10) == 1
    assert envelope(25, 0, 10, 10, 10) == pytest.approx(0.5)
    assert envelope(31, 0, 10, 10, 10) == 0


def test_patterns():
    start = 0.0
    assert build_pattern({"type": "constant", "value": 2}, start).value(0) == 2
    ramp = build_pattern({"type": "ramp", "from": 0.5, "to": 1.0, "duration": "1h"}, start)
    assert ramp.value(0) == pytest.approx(0.5) and ramp.value(1800) == pytest.approx(0.75) and ramp.value(9999) == 1.0
    step = build_pattern({"type": "step", "initial": 1, "steps": [{"at": "+1h", "value": 3}]}, start)
    assert step.value(10) == 1 and step.value(3601) == 3
    spike = build_pattern({"type": "spike", "at": "+1h", "rise": "10m", "hold": "10m", "decay": "10m", "magnitude": 4}, start)
    assert spike.value(0) == 1 and spike.value(3600 + 900) == pytest.approx(4)
    sine = build_pattern({"type": "sine", "period": "1h", "amplitude": 0.2}, start)
    assert 0.8 - 1e-9 <= min(sine.value(t * 60) for t in range(60)) and max(sine.value(t * 60) for t in range(60)) <= 1.2 + 1e-9
    custom = build_pattern({"type": "custom", "points": [["0s", 1], ["1h", 3]]}, start)
    assert custom.value(1800) == pytest.approx(2)
    di = build_pattern({"type": "diurnal", "profile": "streaming", "floor": 0.1}, start)
    vals = [di.value(h * 3600, tz_hours=0) for h in range(24)]
    assert max(vals) <= 1.0 + 1e-9 and min(vals) >= 0.1 - 1e-9 and max(vals) > 2 * min(vals)
    # timezone shifts the curve
    assert di.value(0, tz_hours=0) != di.value(0, tz_hours=8)
    weekly = build_pattern({"type": "weekly", "factors": [1, 1, 1, 1, 1, 2, 2]}, start)
    assert {weekly.value(d * 86400 + 3600) for d in range(7)} == {1, 2}
    with pytest.raises(ValueError):
        build_pattern({"type": "nope"}, start)


def test_pattern_scoping():
    p = build_pattern({"type": "constant", "value": 2, "verticals": ["gaming"], "platforms": ["ps5"]}, 0)
    assert p.applies("gaming", "ps5", "us") and not p.applies("gaming", "pc", "us") and not p.applies("ecom", "web", "us")
