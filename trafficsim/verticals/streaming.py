"""Video streaming vertical: Segment Video Spec events."""

from __future__ import annotations

import bisect
import random

from ..journeys import Session, State, VerticalRuntime, cumulative, zipf_weights

GENRES = ["Drama", "Comedy", "Sci-Fi", "Thriller", "Documentary", "Kids", "Action", "Romance", "Crime", "Reality", "Sports", "Anime"]
WORDS = [
    "Shadow",
    "River",
    "Empire",
    "Echo",
    "Midnight",
    "Garden",
    "Signal",
    "Crown",
    "Frontier",
    "Harbor",
    "Ember",
    "Atlas",
    "Wild",
    "Silent",
    "Golden",
    "Iron",
    "Paper",
    "Glass",
    "Northern",
    "Lost",
    "Hidden",
    "Last",
    "City",
]
CHANNELS = ["Originals", "Movies", "TV", "Kids", "Sports"]
PUBLISHERS = ["StreamFlix Studios", "Northlight Pictures", "Blue Harbor Media", "Atlas Entertainment"]
SEARCH_TERMS = [
    "thriller",
    "new movies",
    "comedy specials",
    "documentary",
    "anime",
    "kids",
    "true crime",
    "football",
    "sci fi series",
    "romance",
    "cooking",
    "nature",
]
QUALITIES = [("2160p", 4500, 0.2), ("1080p", 2800, 0.45), ("720p", 1500, 0.25), ("480p", 700, 0.1)]


class StreamingRuntime(VerticalRuntime):
    vertical = "streaming"

    def setup(self):
        rng = random.Random(f"{self.seed}:streaming-catalog")
        n = int(self.params.get("content_titles", 400))
        self.content = []
        for i in range(n):
            series = rng.random() < 0.7
            program = f"The {rng.choice(WORDS)} {rng.choice(WORDS)}" if rng.random() < 0.6 else f"{rng.choice(WORDS)} {rng.choice(WORDS)}"
            genre = rng.choice(GENRES)
            length = rng.randint(22, 62) * 60 if series else rng.randint(85, 150) * 60
            self.content.append(
                {
                    "asset_id": f"cnt-{i:05d}",
                    "title": program if not series else f"{program} S{rng.randint(1, 5)}E{rng.randint(1, 10)}",
                    "program": program,
                    "season": str(rng.randint(1, 5)) if series else None,
                    "episode": str(rng.randint(1, 10)) if series else None,
                    "genre": genre,
                    "channel": rng.choice(CHANNELS),
                    "publisher": rng.choice(PUBLISHERS),
                    "full_episode": True,
                    "livestream": False,
                    "total_length": length,
                    "airdate": f"20{rng.randint(15, 26)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "keywords": [genre.lower(), rng.choice(WORDS).lower()],
                }
            )
        self.cum = cumulative(zipf_weights(n))
        self.handlers["watch"] = self.h_watch
        self.props.update(
            {
                "content_browsed": lambda s: {
                    "category": self.rng.choice(GENRES),
                    "row": self.rng.choice(["Trending Now", "Continue Watching", "New Releases", "Top 10", "Because you watched"]),
                    "position": self.rng.randint(1, 20),
                },
                "searched": lambda s: {"query": self.rng.choice(SEARCH_TERMS), "results": self.rng.randint(0, 120)},
            }
        )

    def pick_content(self, s: Session) -> dict:
        f = self.featured(s, "asset_id")
        if f and self.rng.random() < float(f.get("share", 0.5)) * f["_intensity"]:
            return {
                "asset_id": f["asset_id"],
                "title": f.get("title"),
                "program": f.get("program", f.get("title")),
                "season": None,
                "episode": None,
                "genre": f.get("genre", "Drama"),
                "channel": "Originals",
                "publisher": PUBLISHERS[0],
                "full_episode": True,
                "livestream": bool(f.get("livestream", False)),
                "total_length": int(float(f.get("length_minutes", 60)) * 60),
                "airdate": None,
                "keywords": ["featured"],
            }
        i = bisect.bisect_left(self.cum, self.rng.random() * self.cum[-1])
        return self.content[min(i, len(self.content) - 1)]

    @staticmethod
    def _content_props(c: dict, session_id: str, pod: str, position: float) -> dict:
        p = {
            "session_id": session_id,
            "asset_id": c["asset_id"],
            "content_pod_id": pod,
            "title": c["title"],
            "program": c["program"],
            "genre": c["genre"],
            "channel": c["channel"],
            "publisher": c["publisher"],
            "full_episode": c["full_episode"],
            "livestream": c["livestream"],
            "total_length": c["total_length"],
            "position": int(position),
            "keywords": c["keywords"],
        }
        if c.get("season"):
            p["season"], p["episode"] = c["season"], c["episode"]
        if c.get("airdate"):
            p["airdate"] = c["airdate"]
        return p

    def h_watch(self, s: Session, st: State):
        rng = self.rng
        hb = float(self.params.get("heartbeat_seconds", 60))
        c = self.pick_content(s)
        live = c["livestream"]
        total = c["total_length"]
        pos = 0.0 if live or rng.random() < 0.65 else rng.uniform(0.1, 0.8) * total
        quality, bitrate, _ = QUALITIES[min(3, int(rng.random() ** 1.3 * 4))]
        if s.platform == "web":
            player = "html5"
        elif s.platform == "ios":
            player = "AVPlayer"
        else:
            player = "ExoPlayer"
        session_id = f"{s.anon_id[:8]}-{s.seq}"
        ads = s.user.traits.get("plan") == "basic" or live
        base = {
            "session_id": session_id,
            "content_asset_id": c["asset_id"],
            "total_length": total,
            "bitrate": bitrate,
            "framerate": 60 if live else 24,
            "video_player": player,
            "sound": rng.randint(40, 100),
            "full_screen": s.platform != "web" or rng.random() < 0.6,
            "ad_enabled": ads,
            "quality": quality,
            "livestream": live,
        }
        s.data["playback"] = base
        s.data["pos"] = pos
        self.track(s, "playback_started", {**base, "position": int(pos)})
        yield rng.uniform(1, 4)
        if ads and rng.random() < 0.8:
            yield from self._ad(s, session_id, "pre", c)
            if s.expired():
                return None
        pod = f"pod-{c['asset_id']}-{int(pos // 600)}"
        self.track(s, "content_started", self._content_props(c, session_id, pod, pos))
        mean_watch = float(self.params.get("mean_watch_minutes", 32)) * 60
        abandon_p = min(0.5, hb / mean_watch)
        next_ad = pos + 900
        while not s.expired():
            yield hb
            if s.expired():
                break
            pos += hb
            s.data["pos"] = pos
            if not live and pos >= total:
                self.track(s, "content_completed", self._content_props(c, session_id, pod, total))
                self.track(s, "playback_completed", {**base, "position": total})
                s.data.pop("playback", None)
                return None
            r = rng.random()
            if r < 0.04:
                self.track(s, "buffer_started", {**base, "position": int(pos)})
                yield rng.uniform(0.5, 8)
                if s.expired():  # viewer gave up while buffering; on_end emits Playback Exited
                    break
                self.track(s, "buffer_completed", {**base, "position": int(pos)})
            elif r < 0.07:
                self.track(s, "playback_paused", {**base, "position": int(pos)})
                yield rng.uniform(5, 240)
                if s.expired():
                    break
                self.track(s, "playback_resumed", {**base, "position": int(pos)})
            elif r < 0.09 and not live:
                target = min(total - 1, max(0, pos + rng.uniform(-120, 300)))
                self.track(s, "seek_started", {**base, "position": int(pos), "seek_position": int(target)})
                pos = target
                s.data["pos"] = pos
                self.track(s, "seek_completed", {**base, "position": int(pos), "seek_position": int(target)})
            elif ads and pos >= next_ad:
                next_ad = pos + 900
                yield from self._ad(s, session_id, "mid", c)
                if s.expired():
                    break
            if rng.random() < abandon_p:
                self.track(s, "playback_exited", {**base, "position": int(pos)})
                s.data.pop("playback", None)
                return None
            self.track(s, "content_playing", self._content_props(c, session_id, pod, pos))
        return None

    def _ad(self, s: Session, session_id: str, pod_type: str, c: dict):
        n = self.rng.randint(1, 2)
        for i in range(n):
            ad = {
                "session_id": session_id,
                "asset_id": f"ad-{self.rng.randint(1, 300):04d}",
                "pod_id": f"{pod_type}-{c['asset_id']}",
                "type": "pre-roll" if pod_type == "pre" else "mid-roll",
                "title": self.rng.choice(["Spring Collection", "New Phone Launch", "Holiday Travel", "Energy Drink"]),
                "publisher": self.rng.choice(["Acme Ads", "Brightline"]),
                "position": i + 1,
                "total_length": 15 * self.rng.randint(1, 2),
                "content_asset_id": c["asset_id"],
            }
            self.track(s, "ad_started", ad)
            yield float(ad["total_length"])
            self.track(s, "ad_completed", ad)

    def on_end(self, s: Session) -> None:
        pb = s.data.pop("playback", None)
        if pb is not None:
            self.track(s, "playback_exited", {**pb, "position": int(s.data.get("pos", 0))})
