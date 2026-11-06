"""Catalogs, journey state machines and the per-session runner.

A session is driven by a Python generator: it emits events at the session's
current simulated time and `yield`s the number of simulated seconds to wait
before it should be resumed. The worker's scheduler resumes it at that time.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from .config import CATALOG_DIR, VerticalConfig
from .contexts import web_page

EXIT = "exit"


@dataclass
class State:
    name: str
    view: dict | None = None
    track: list[str] = field(default_factory=list)
    dwell: tuple[float, float] = (5.0, 30.0)
    handler: str | None = None
    next: dict[str, float] = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.targets = list(self.next)
        self.weights = [float(self.next[t]) for t in self.targets]


@dataclass
class Journey:
    start: str
    states: dict[str, State]

    def validate(self, handlers: dict, track_keys: set[str]) -> None:
        if self.start not in self.states:
            raise ValueError(f"journey.start {self.start!r} is not a state")
        for st in self.states.values():
            for t in st.targets:
                if t != EXIT and t not in self.states:
                    raise ValueError(f"state {st.name}: transition to unknown state {t!r}")
            fb = st.params.get("fallback")
            if fb and fb != EXIT and fb not in self.states:
                raise ValueError(f"state {st.name}: fallback to unknown state {fb!r}")
            if st.handler and st.handler not in handlers:
                raise ValueError(f"state {st.name}: unknown handler {st.handler!r} (have {sorted(handlers)})")
            for k in st.track:
                if k not in track_keys:
                    raise ValueError(f"state {st.name}: unknown event key {k!r}")
            if not st.targets and not st.handler:
                raise ValueError(f"state {st.name}: needs transitions in 'next'")


@dataclass
class Catalog:
    vertical: str
    app: dict
    events: dict[str, str]
    funnels: dict[str, list[str]]
    params: dict[str, Any]
    journey: Journey
    raw: dict

    @classmethod
    def load(cls, vertical: str, vc: VerticalConfig | None = None) -> "Catalog":
        path = Path(vc.catalog) if vc and vc.catalog else CATALOG_DIR / f"{vertical}.yaml"
        if not path.is_absolute() and not path.exists():
            path = CATALOG_DIR.parent / path
        raw = yaml.safe_load(path.read_text()) or {}
        if raw.get("vertical", vertical) != vertical:
            raise ValueError(f"catalog {path} is for vertical {raw.get('vertical')!r}, not {vertical!r}")
        events = dict(raw.get("events") or {})
        if vc:
            unknown = set(vc.event_names) - set(events)
            if unknown:
                raise ValueError(f"{vertical}.event_names: unknown keys {sorted(unknown)}")
            events.update(vc.event_names)
        params = dict(raw.get("params") or {})
        if vc:
            params.update(vc.params)
        j = raw.get("journey") or {}
        states = {}
        for name, s in (j.get("states") or {}).items():
            s = s or {}
            dwell = s.get("dwell", [5, 30])
            if isinstance(dwell, (int, float)):
                dwell = [dwell, dwell]
            view = s.get("view")
            if isinstance(view, str):
                view = {"name": view}
            states[name] = State(name=name, view=view, track=list(s.get("track") or []), dwell=(float(dwell[0]), float(dwell[1])),
                                 handler=s.get("handler"), next=dict(s.get("next") or {}), params=dict(s.get("params") or {}))
        return cls(vertical=vertical, app=dict(raw.get("app") or {}), events=events, funnels=dict(raw.get("funnels") or {}),
                   params=params, journey=Journey(start=j.get("start", ""), states=states), raw=raw)


class Session:
    __slots__ = ("sid", "vertical", "platform", "region", "cohort", "user", "ctx", "user_id", "anon_id", "start",
                 "budget_end", "now", "gen", "data", "vx", "ending", "warm", "seq", "first_session")

    def __init__(self, sid: int, cohort: tuple[str, str, str], user, ctx: dict, start: float, budget: float, vx: "VerticalRuntime",
                 warm: bool = False):
        self.sid = sid
        self.cohort = cohort
        self.vertical, self.platform, self.region = cohort
        self.user = user
        self.ctx = ctx
        self.anon_id = user.anonymous_id
        self.user_id: str | None = None
        self.start = start
        self.budget_end = start + budget
        self.now = start
        self.vx = vx
        self.data: dict = {}
        self.ending = False
        self.warm = warm
        self.seq = 0
        self.first_session = False
        self.gen: Iterator[float] = vx.run(self)

    def expired(self) -> bool:
        return self.ending or self.now >= self.budget_end

    @property
    def is_web(self) -> bool:
        return self.platform == "web"


Handler = Callable[["Session", State], Iterator[float]]


class VerticalRuntime:
    """Base class for a vertical: owns the catalog, handlers and property builders."""

    vertical = ""

    def __init__(self, catalog: Catalog, emitter, rng: random.Random, seed: int = 0):
        self.catalog = catalog
        self.names = catalog.events
        self.params = catalog.params
        self.app = catalog.app
        self.emitter = emitter
        self.rng = rng
        self.seed = seed
        self.effects: dict[str, dict] = {}  # platform -> {"bias":..., "featured": [...]}
        self.handlers: dict[str, Handler] = {"auth": self.h_auth}
        self.props: dict[str, Callable[[Session], dict]] = {}
        self.setup()
        catalog.journey.validate(self.handlers, set(self.names))

    # ---- hooks for subclasses
    def setup(self) -> None:
        pass

    def on_start(self, s: Session) -> Iterator[float]:
        return iter(())

    def on_end(self, s: Session) -> None:
        pass

    def requirement(self, s: Session, name: str) -> bool:
        if name == "identified":
            return s.user_id is not None
        if name == "anonymous":
            return s.user_id is None
        return True

    def initial_login(self, s: Session) -> bool:
        """Whether a registered user is already logged in at session start."""
        return s.user.registered and (not s.is_web or self.rng.random() < 0.6)

    # ---- helpers
    def bias(self, s: Session) -> dict:
        eff = self.effects.get(s.platform)
        return eff["bias"] if eff else {}

    def featured(self, s: Session, kind: str | None = None) -> dict | None:
        eff = self.effects.get(s.platform)
        if not eff:
            return None
        for f in eff["featured"]:
            if kind is None or f["_kind"] == kind or kind in f:
                return f
        return None

    def dwell(self, st: State) -> float:
        lo, hi = st.dwell
        return lo if hi <= lo else self.rng.uniform(lo, hi)

    def choose(self, s: Session, st: State) -> str:
        if not st.targets:
            return EXIT
        b = self.bias(s)
        weights = [w * b.get(t, 1.0) for t, w in zip(st.targets, st.weights)] if b else st.weights
        return self.rng.choices(st.targets, weights)[0]

    def view(self, s: Session, view: dict, props: dict | None = None) -> None:
        name = view.get("name", "")
        props = dict(props or {})
        if s.is_web:
            path = view.get("path") or "/" + name.lower().replace(" ", "-")
            if "{" in path:
                path = path.format_map(_Missing(s.data))
            prev = s.data.get("page")
            referrer = prev["url"] if prev else self.rng.choice(self.params.get("referrers", ["https://www.google.com/", ""]))
            page = web_page(self.app.get("web_url", "https://example.com"), path, f"{name} | {self.app.get('name', '')}",
                            referrer, view.get("search", ""))
            s.data["page"] = page
            props.update(page)
            self.emitter.page(s, name, props, view.get("category"))
        else:
            self.emitter.screen(s, name, props, view.get("category"))

    def track(self, s: Session, key: str, props: dict | None = None) -> None:
        if props is None:
            fn = self.props.get(key)
            props = fn(s) if fn else {}
        self.emitter.track(s, key, self.names[key], props)

    def identify(self, s: Session) -> None:
        s.user_id = s.user.user_id
        self.emitter.identify(s, s.user.traits)

    # ---- built-in handler: sign in / sign up
    def h_auth(self, s: Session, st: State):
        if s.user_id:
            return None
        if st.view:
            self.view(s, st.view)
        yield self.rng.uniform(8, 40)
        if s.user.registered:
            self.identify(s)
            if "signed_in" in self.names:
                self.track(s, "signed_in", {"method": self.rng.choice(["email", "google", "apple"])})
        else:
            s.user.registered = True
            s.user.extra["signed_up"] = True
            s.user_id = s.user.user_id
            self.emitter.alias(s, s.anon_id)
            self.emitter.identify(s, s.user.traits)
            if "signed_up" in self.names:
                self.track(s, "signed_up", {"method": self.rng.choice(["email", "google", "apple"])})
        return None

    # ---- the session generator
    def run(self, s: Session) -> Iterator[float]:
        if self.initial_login(s):
            s.user_id = s.user.user_id
        yield from self.on_start(s)
        states = self.catalog.journey.states
        state = self.catalog.journey.start
        hops = 0
        while not s.expired():
            st = states[state]
            req = st.params.get("requires")
            if req and not self.requirement(s, req):
                state = st.params.get("fallback", EXIT)
                hops += 1
                if state == EXIT or hops > 8:
                    break
                continue
            hops = 0
            nxt = None
            if st.handler:
                nxt = yield from self.handlers[st.handler](s, st)
            else:
                # build props first: builders may select the product/content the view refers to
                props = [(key, self.props[key](s) if key in self.props else {}) for key in st.track]
                if st.view:
                    self.view(s, st.view)
                for key, p in props:
                    self.track(s, key, p)
                yield self.dwell(st)
            if s.expired():
                break
            state = nxt or self.choose(s, st)
            if state == EXIT:
                break
        self.on_end(s)


class _Missing(dict):
    def __missing__(self, key):
        return key


def zipf_weights(n: int, s: float = 1.07) -> list[float]:
    return [1.0 / ((i + 1) ** s) for i in range(n)]


def cumulative(weights: list[float]) -> list[float]:
    out, acc = [], 0.0
    for w in weights:
        acc += w
        out.append(acc)
    return out
