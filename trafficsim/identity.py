"""Deterministic synthetic user pool.

A user is a pure function of (seed, vertical, platform, region, index), so any
worker can materialise the same user without shared state. Workers own the
indices where index % n_workers == worker_id.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field

from .config import REGIONS

FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Emma", "Kenji", "Yuki", "Lucas", "Ana", "Oliver", "Mia", "Ethan",
         "Zara", "Leo", "Chloe", "Ravi", "Priya", "Mateo", "Isabella", "Jonas", "Lea", "Min-jun", "Seo-yeon", "Hugo",
         "Camille", "Diego", "Valentina", "Arjun", "Ines", "Felix", "Hannah", "Kai", "Nora", "Omar", "Layla"]
LAST = ["Sharma", "Smith", "Garcia", "Müller", "Martin", "Tanaka", "Kim", "Silva", "Rossi", "Brown", "Patel",
        "Johnson", "Lopez", "Schmidt", "Dubois", "Sato", "Park", "Santos", "Wilson", "Nguyen", "Khan", "Lee"]
PLANS = {"streaming": [("basic", 0.3), ("standard", 0.45), ("premium", 0.25)],
         "ecom": [("free", 0.8), ("plus", 0.2)],
         "gaming": [("free", 0.6), ("plus", 0.3), ("ultimate", 0.1)]}
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
    first_seen: bool = False
    extra: dict = field(default_factory=dict)


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
    traits = {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": f"{email_local}@example.com",
        "plan": plan,
        "country": country,
        "created_at": created,
    }
    if vertical == "gaming":
        traits["username"] = f"{first.lower()}{rng.randint(10, 9999)}"
        traits["level"] = rng.randint(1, 120)
    ip = f"{rng.randint(11, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    return User(index=index, user_id=uid, anonymous_id=anon, registered=rng.random() < REGISTERED_SHARE[vertical],
                traits=traits, country=country, locale=locale, timezone=tz, ip=ip, seed=rng.getrandbits(32))


class UserPool:
    """Per-worker view of a cohort's user pool with active-user de-duplication."""

    def __init__(self, seed: int, cohort: tuple[str, str, str], size: int, worker_id: int, n_workers: int):
        self.seed = seed
        self.cohort = cohort
        self.size = max(n_workers * 4, int(size))
        self.worker_id = worker_id
        self.n = n_workers
        self.active: set[int] = set()
        self.signed_up: set[int] = set()  # users who registered during the run stay registered
        self._overflow = self.size

    def acquire(self, rng: random.Random) -> User:
        slots = max(1, (self.size - self.worker_id + self.n - 1) // self.n)
        for _ in range(6):
            idx = self.worker_id + self.n * rng.randrange(slots)
            if idx not in self.active:
                break
        else:
            # pool saturated: mint a fresh user beyond the pool (still deterministic per worker)
            while True:
                idx = self.worker_id + self.n * ((self._overflow // self.n) + 1)
                self._overflow = idx
                if idx not in self.active:
                    break
        self.active.add(idx)
        v, p, r = self.cohort
        user = make_user(self.seed, v, p, r, idx)
        if idx in self.signed_up:
            user.registered = True
        return user

    def release(self, user: User) -> None:
        self.active.discard(user.index)
        if user.extra.get("signed_up"):
            self.signed_up.add(user.index)
