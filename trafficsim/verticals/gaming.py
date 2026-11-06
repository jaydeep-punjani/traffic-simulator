"""Gaming vertical (PC / PS5): custom game telemetry event set."""

from __future__ import annotations

import zlib
from typing import Any

from ..journeys import Session, State, VerticalRuntime

DEFAULT_GAMES: list[dict[str, Any]] = [
    {
        "id": "nebula_arena",
        "name": "Nebula Arena",
        "genre": "shooter",
        "weight": 0.5,
        "modes": {"team_deathmatch": [8, 14], "battle_royale": [15, 28], "ranked": [12, 22]},
        "maps": ["Orbital Station", "Dust Ring", "Cryo Labs", "Skyline"],
    },
    {
        "id": "kingdom_forge",
        "name": "Kingdom Forge",
        "genre": "rpg",
        "weight": 0.3,
        "modes": {"dungeon": [15, 35], "raid": [30, 60], "pvp_arena": [6, 12]},
        "maps": ["Ashen Keep", "Verdant Vale", "Frost Hollow"],
    },
    {
        "id": "velocity_rush",
        "name": "Velocity Rush",
        "genre": "racing",
        "weight": 0.2,
        "modes": {"quick_race": [3, 6], "grand_prix": [12, 20], "time_trial": [2, 5]},
        "maps": ["Neon Bay", "Alpine Pass", "Desert Loop", "Harbor Circuit"],
    },
]
ITEMS = [
    ("skin", "Crimson Nova Skin", 1200),
    ("emote", "Victory Dance", 500),
    ("battle_pass", "Season Pass", 950),
    ("bundle", "Starter Bundle", 2000),
    ("booster", "XP Booster", 300),
    ("currency_pack", "1000 Credits", 0),
]
ACHIEVEMENTS = ["First Blood", "Untouchable", "Marathon", "Sharpshooter", "Team Player", "Collector", "Speed Demon"]


class GamingRuntime(VerticalRuntime):
    vertical = "gaming"

    def setup(self):
        self.games = self.params.get("games") or DEFAULT_GAMES
        # share of sessions on an established title that are a player's very first session
        self.new_player_rate = float(self.params.get("new_player_rate", 0.03))
        self.game_weights = [float(g.get("weight", 1)) for g in self.games]
        self.handlers.update({"login": self.h_login, "match": self.h_match})
        self.props.update(
            {
                "lobby_joined": lambda s: {
                    "game_id": s.data["game"]["id"],
                    "lobby_id": f"lby-{self.rng.getrandbits(32):08x}",
                    "party_size": s.data["party"],
                },
                "store_viewed": lambda s: {
                    "game_id": s.data["game"]["id"],
                    "store_section": self.rng.choice(["featured", "skins", "bundles", "battle_pass", "currency"]),
                },
                "item_purchased": self.p_purchase,
                "tutorial_started": lambda s: {"game_id": s.data["game"]["id"]},
                "tutorial_completed": lambda s: {"game_id": s.data["game"]["id"], "duration_s": self.rng.randint(180, 900)},
            }
        )

    def requirement(self, s: Session, name: str) -> bool:
        if name == "new_player":
            return s.first_session and not s.data.get("tutorial_done")
        return super().requirement(s, name)

    def initial_login(self, s: Session) -> bool:
        return False  # gaming sessions always go through the login step

    def _game(self, s: Session) -> dict:
        installed = s.user.extra.setdefault("installed", [])
        f = self.featured(s, "game_id")
        if f and self.rng.random() < float(f.get("share", 0.6)) * f["_intensity"]:
            g = {
                "id": f["game_id"],
                "name": f.get("game", f["game_id"]),
                "genre": f.get("genre", "action"),
                "modes": {"campaign": [20, 40], "co_op": [15, 30], "ranked": [12, 22]},
                "maps": ["Prologue", "Starfall Rim", "Echo Basin"],
                "build": f.get("build", "1.0.0"),
            }
            # a newly launched title: first play is an install unless the user pre-loaded it
            new_share = float(f.get("new_player_share", 0.5))
        else:
            g = self.rng.choices(self.games, self.game_weights)[0]
            new_share = self.new_player_rate
        if g["id"] not in installed:
            s.first_session = self.rng.random() < new_share
            installed.append(g["id"])  # later sessions of this user are returning sessions
        return g

    def on_start(self, s: Session):
        g = self._game(s)
        s.data.update({"game": g, "matches": 0, "party": 1 if self.rng.random() < 0.6 else self.rng.randint(2, 4)})
        patch = self.featured(s, "patch")
        version = g.get("build") or self.app.get("version", "1.0.0")
        if patch and self.rng.random() < patch["_intensity"]:
            version = str(patch.get("build") or self.app.get("patch_version", "7.0.0"))
        if version != s.ctx.get("app", {}).get("version"):
            s.ctx = {**s.ctx, "app": {**s.ctx.get("app", {}), "version": version}}
        if s.first_session:
            self.track(
                s,
                "game_installed",
                {
                    "game_id": g["id"],
                    "game_name": g["name"],
                    "platform": s.platform,
                    "store": "steam" if s.platform == "pc" else "playstation_store",
                    "size_gb": round(self.rng.uniform(40, 120), 1),
                },
            )
            yield self.rng.uniform(30, 300)
        props = {
            "game_id": g["id"],
            "game_name": g["name"],
            "genre": g.get("genre"),
            "build": version,
            "first_launch": s.first_session,
            "platform": s.platform,
        }
        if patch:
            props["patch"] = patch.get("patch")
        self.track(s, "game_launched", props)
        yield self.rng.uniform(5, 25)

    def h_login(self, s: Session, st: State):
        yield self.rng.uniform(2, 10)
        s.user_id = s.user.user_id
        self.emitter.identify(s, s.user.traits)
        method = "psn" if s.platform == "ps5" else ("steam" if s.ctx.get("app", {}).get("launcher") == "Steam" else "epic")
        self.track(s, "login_completed", {"method": method, "game_id": s.data["game"]["id"], "first_login": s.first_session})

    def h_match(self, s: Session, st: State):
        rng = self.rng
        g = s.data["game"]
        t = self.featured(s, "tournament")
        modes = g["modes"]
        if t and rng.random() < t["_intensity"] * 0.8:
            mode, dur = t.get("mode", "ranked_tournament"), [15, 30]
        else:
            mode = rng.choice(list(modes))
            dur = modes[mode]
        region = {"us": "na-east", "eu": "eu-west", "apac": "ap-northeast", "latam": "sa-east", "india": "ap-south"}[s.region]
        match_id = f"m-{rng.getrandbits(48):012x}"
        self.track(
            s,
            "matchmaking_started",
            {
                "game_id": g["id"],
                "mode": mode,
                "region": region,
                "party_size": s.data["party"],
                "ranked": "rank" in mode or "tournament" in mode,
            },
        )
        surge = 1.0 + (2.0 * t["_intensity"] if t else 0.0)
        wait = min(600.0, rng.lognormvariate(3.2, 0.6) * surge)
        yield wait
        if s.expired():
            return None
        if rng.random() < 0.05:
            return "lobby"
        self.track(s, "match_found", {"game_id": g["id"], "match_id": match_id, "mode": mode, "wait_time_s": round(wait, 1)})
        yield rng.uniform(10, 40)
        if s.ending:
            return None
        players = {"battle_royale": 60}.get(mode, rng.choice([2, 8, 10, 12]))
        self.track(
            s, "match_started", {"game_id": g["id"], "match_id": match_id, "mode": mode, "map": rng.choice(g["maps"]), "players": players}
        )
        length = rng.uniform(dur[0], dur[1]) * 60
        chunks = 3
        for _ in range(chunks):
            yield length / chunks
            if s.ending:  # session trimmed by the planner: player quits mid-match
                return None
            if rng.random() < 0.06:
                self.track(s, "achievement_unlocked", {"game_id": g["id"], "achievement": rng.choice(ACHIEVEMENTS), "match_id": match_id})
        xp_mult = float((self.featured(s, "xp_multiplier") or {}).get("xp_multiplier", 1.0))
        won = rng.random() < 0.48
        xp = int(rng.randint(400, 2500) * xp_mult * (1.3 if won else 1.0))
        s.data["matches"] += 1
        self.track(
            s,
            "match_completed",
            {
                "game_id": g["id"],
                "match_id": match_id,
                "mode": mode,
                "result": "win" if won else "loss",
                "score": rng.randint(100, 9000),
                "kills": rng.randint(0, 25),
                "deaths": rng.randint(0, 15),
                "duration_s": int(length),
                "xp_earned": xp,
            },
        )
        if rng.random() < 0.22:
            lvl = int(s.data.get("level") or s.user.traits.get("level", 1)) + 1
            s.data["level"] = lvl
            self.track(s, "level_up", {"game_id": g["id"], "level": lvl, "previous_level": lvl - 1})
        return None

    def p_purchase(self, s):
        kind, name, price = self.rng.choice(ITEMS)
        props = {
            "game_id": s.data["game"]["id"],
            "item_id": f"itm-{zlib.crc32(name.encode()) % 10000:04d}",
            "item_name": name,
            "item_type": kind,
            "currency": "credits",
            "price": price,
        }
        if kind == "currency_pack":
            props.update({"currency": "USD", "price": 9.99, "real_money": True})
        return props

    def on_end(self, s: Session) -> None:
        if "game" in s.data:
            self.track(
                s,
                "game_exited",
                {"game_id": s.data["game"]["id"], "session_length_s": int(s.now - s.start), "matches_played": s.data.get("matches", 0)},
            )
