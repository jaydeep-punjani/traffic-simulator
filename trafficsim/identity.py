"""Deterministic synthetic user pool.

A user is a pure function of (seed, vertical, platform, region, index), so any
worker can materialise the same user without shared state. Workers own the
indices where index % n_workers == worker_id.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import REGIONS

FIRST = [
    "Aarav",
    "Maya",
    "Liam",
    "Sofia",
    "Noah",
    "Emma",
    "Kenji",
    "Yuki",
    "Lucas",
    "Ana",
    "Oliver",
    "Mia",
    "Ethan",
    "Zara",
    "Leo",
    "Chloe",
    "Ravi",
    "Priya",
    "Mateo",
    "Isabella",
    "Jonas",
    "Lea",
    "Min-jun",
    "Seo-yeon",
    "Hugo",
    "Camille",
    "Diego",
    "Valentina",
    "Arjun",
    "Ines",
    "Felix",
    "Hannah",
    "Kai",
    "Nora",
    "Omar",
    "Layla",
]
LAST = [
    "Sharma",
    "Smith",
    "Garcia",
    "Müller",
    "Martin",
    "Tanaka",
    "Kim",
    "Silva",
    "Rossi",
    "Brown",
    "Patel",
    "Johnson",
    "Lopez",
    "Schmidt",
    "Dubois",
    "Sato",
    "Park",
    "Santos",
    "Wilson",
    "Nguyen",
    "Khan",
    "Lee",
]
PLANS = {
    "streaming": [("basic", 0.3), ("standard", 0.45), ("premium", 0.25)],
    "ecom": [("free", 0.8), ("plus", 0.2)],
    "gaming": [("free", 0.6), ("plus", 0.3), ("ultimate", 0.1)],
}
REGISTERED_SHARE = {"streaming": 0.92, "ecom": 0.55, "gaming": 0.97}


def weighted(rng: random.Random, items):
    total = sum(w for *_, w in items)
    x = rng.random() * total
    for it in items:
        x -= it[-1]
        if x <= 0:
            return it
    return items[-1]


@dataclass(slots=True)
class User:
    index: int
    user_id: str
    anonymous_id: str
    registered: bool
    traits: dict
    country: str
    locale: str
    timezone: str
    ip: str
    seed: int
    extra: dict = field(default_factory=dict)  # state that persists across this user's sessions in a run


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_user(seed: int, vertical: str, platform: str, region: str, index: int) -> User:
    rng = random.Random(f"{seed}:{vertical}:{platform}:{region}:{index}")
    country, locale, tz, _ = weighted(rng, REGIONS[region]["countries"])
    first, last = rng.choice(FIRST), rng.choice(LAST)
    prefix = {"streaming": "st", "ecom": "ec", "gaming": "gm"}[vertical]
    uid = f"{prefix}_{uuid.UUID(int=rng.getrandbits(128), version=4).hex[:20]}"
    anon = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    created = 1_577_836_800 + rng.randint(0, 6 * 365 * 86400)  # 2020-2026
    plan = weighted(rng, PLANS[vertical])[0]
    email_local = f"{first}.{last}{rng.randint(1, 999)}".lower().replace(" ", "")
    # Segment reserved trait names (camelCase; createdAt as ISO-8601).
    traits = {
        "name": f"{first} {last}",
        "firstName": first,
        "lastName": last,
        "email": f"{email_local}@example.com",
        "plan": plan,
        "address": {"country": country},
        "createdAt": iso_utc(created),
    }
    if vertical == "gaming":
        traits["username"] = f"{first.lower()}{rng.randint(10, 9999)}"
        traits["level"] = rng.randint(1, 120)
    ip = f"{rng.randint(11, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    return User(
        index=index,
        user_id=uid,
        anonymous_id=anon,
        registered=rng.random() < REGISTERED_SHARE[vertical],
        traits=traits,
        country=country,
        locale=locale,
        timezone=tz,
        ip=ip,
        seed=rng.getrandbits(32),
    )


class UserPool:
    """Per-worker view of a cohort's user pool with active-user de-duplication.

    A user is never in two sessions at once. Per-user state that should survive
    between sessions (sign-ups, installed games) lives in `persist`; it is bounded
    by the number of distinct users touched.
    """

    def __init__(self, seed: int, cohort: tuple[str, str, str], size: int, worker_id: int, n_workers: int):
        self.seed = seed
        self.cohort = cohort
        self.size = max(n_workers * 4, int(size))
        self.worker_id = worker_id
        self.n = n_workers
        self.slots = max(1, (self.size - self.worker_id + self.n - 1) // self.n)
        self.active: set[int] = set()
        self.persist: dict[int, dict] = {}
        self._overflow_next = self.slots  # next never-used slot beyond the pool
        self._overflow_free: list[int] = []  # released overflow slots, reused before minting more

    def _idx(self, slot: int) -> int:
        return self.worker_id + self.n * slot

    def acquire(self, rng: random.Random) -> User:
        idx = -1
        for _ in range(6):
            cand = self._idx(rng.randrange(self.slots))
            if cand not in self.active:
                idx = cand
                break
        if idx < 0 and len(self.active) < self.slots:
            start = rng.randrange(self.slots)
            for k in range(self.slots):
                cand = self._idx((start + k) % self.slots)
                if cand not in self.active:
                    idx = cand
                    break
        if idx < 0:
            # every pool slot is busy: borrow an overflow user (deterministic per worker)
            if self._overflow_free:
                idx = self._overflow_free.pop()
            else:
                idx = self._idx(self._overflow_next)
                self._overflow_next += 1
        self.active.add(idx)
        v, p, r = self.cohort
        user = make_user(self.seed, v, p, r, idx)
        state = self.persist.get(idx)
        if state:
            user.extra = state
            if "signed_up_at" in state:
                user.registered = True
                user.traits["createdAt"] = iso_utc(state["signed_up_at"])
        return user

    def release(self, user: User) -> None:
        self.active.discard(user.index)
        if user.extra:
            self.persist[user.index] = user.extra
        if user.index >= self._idx(self.slots):
            self._overflow_free.append(user.index)
