import pytest
from pydantic import ValidationError

from trafficsim.config import (Destinations, apply_overrides, expand_env, list_scenarios, load_destinations,
                               load_scenario, parse_set_args)
from trafficsim.events import PRESETS, Overlay, effects_for
from trafficsim.planner import TrafficPlanner


def test_targets_split_by_weights(make_scenario):
    sc = make_scenario(traffic={"base_concurrency": 1000, "regions": {"us": 0.5, "eu": 0.5}},
                       verticals={"streaming": {"weight": 3, "platforms": {"web": 1, "ios": 1}}, "gaming": {"weight": 1}})
    pl = TrafficPlanner(sc, start_sim=0)
    tg = pl.targets(0)
    assert sum(tg.values()) == pytest.approx(1000)
    assert sum(v for (vert, _, _), v in tg.items() if vert == "streaming") == pytest.approx(750)
    assert tg[("streaming", "web", "us")] == pytest.approx(187.5)
    assert ("streaming", "android", "us") not in tg


def test_overlay_multiplies_scope_only(make_scenario):
    sc = make_scenario(traffic={"base_concurrency": 900, "regions": {"us": 1}},
                       events=[{"type": "campaign_push", "at": "+0s", "rise": "1m", "hold": "1h", "magnitude": 3}])
    pl = TrafficPlanner(sc, start_sim=0)
    t = 1800
    tg = pl.targets(t)
    assert tg[("ecom", "ios", "us")] == pytest.approx(3 * pl.base[("ecom", "ios", "us")])
    assert tg[("ecom", "web", "us")] == pytest.approx(pl.base[("ecom", "web", "us")])
    assert tg[("gaming", "pc", "us")] == pytest.approx(pl.base[("gaming", "pc", "us")])
    peak, _ = pl.peak_estimate()
    assert peak > 900


def test_overlay_effects_and_presets():
    for kind, preset in PRESETS.items():
        if kind == "custom":
            with pytest.raises(ValueError):
                Overlay({"type": kind}, 0)
            continue
        ov = Overlay({"type": kind, "at": "+1h"}, 0)
        assert ov.vertical == preset["vertical"] and ov.end > ov.at
    ov = Overlay({"type": "flash_sale", "at": 0, "rise": 0, "hold": 100, "decay": 0, "magnitude": 2}, 0)
    eff = effects_for([ov], "ecom", "web", 50)
    assert eff["bias"]["add_to_cart"] > 1.5
    assert effects_for([ov], "ecom", "web", 500)["bias"] == {}
    with pytest.raises(ValueError):
        Overlay({"type": "unknown"}, 0)


def test_all_bundled_scenarios_validate():
    found = list_scenarios()
    assert len(found) >= 10
    for s in found:
        sc = load_scenario(s["id"])
        pl = TrafficPlanner(sc)
        assert pl.cohorts, s["id"]


def test_scenario_validation_errors(make_scenario):
    with pytest.raises(ValidationError):
        make_scenario(verticals={"gaming": {"platforms": {"ios": 1}}})
    with pytest.raises(ValidationError):
        make_scenario(verticals={"nope": {}})
    with pytest.raises(ValidationError):
        make_scenario(bogus=1)
    with pytest.raises(ValidationError):
        make_scenario(traffic={"regions": {"mars": 1}})


def test_overrides_and_set_args():
    raw = {"traffic": {"base_concurrency": 10}}
    out = apply_overrides(raw, {"traffic.base_concurrency": "500", "verticals.ecom.platforms.web": 1})
    assert out["traffic"]["base_concurrency"] == 500 and out["verticals"]["ecom"]["platforms"]["web"] == 1
    assert raw["traffic"]["base_concurrency"] == 10
    assert parse_set_args(["speed=60", "start_at=-24h"]) == {"speed": 60, "start_at": "-24h"}


def test_destinations(monkeypatch, tmp_path):
    monkeypatch.setenv("TS_HOST", "http://collector:9000")
    assert expand_env({"a": "${TS_HOST}/x", "b": "${MISSING:-fallback}"}) == {"a": "http://collector:9000/x", "b": "fallback"}
    f = tmp_path / "d.yaml"
    f.write_text("host: ${TS_HOST}\ndefault_write_key: dk\nsources:\n  ecom: ecom-key\n  ecom_ios: ${NOPE:-}\n  gaming_ps5: ps5\n")
    d = load_destinations(str(f))
    assert d.host == "http://collector:9000"
    assert d.write_key("ecom", "ios") == "ecom-key"
    assert d.write_key("gaming", "ps5") == "ps5" and d.write_key("gaming", "pc") == "dk"
    assert "ps5" not in str(Destinations(default_write_key="secret-key-123").redacted())
    assert load_destinations(str(f), host_override="http://h").host == "http://h"
