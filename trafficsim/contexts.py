"""Segment `context` builders that mimic each client platform's SDK."""

from __future__ import annotations

import random
import uuid

from .identity import User, weighted

LIBRARIES = {
    "web": {"name": "analytics.js", "version": "next-1.72.0"},
    "ios": {"name": "analytics-ios", "version": "4.1.8"},
    "android": {"name": "analytics-android", "version": "4.11.3"},
    "pc": {"name": "analytics-csharp", "version": "2.4.2"},
    "ps5": {"name": "analytics-csharp", "version": "2.4.2"},
}

BROWSERS = [
    (
        "Chrome",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
        ["130", "131", "132", "133"],
        0.42,
    ),
    (
        "Chrome",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
        ["130", "131", "132", "133"],
        0.16,
    ),
    (
        "Safari",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{v} Safari/605.1.15",
        ["17.6", "18.1", "18.2"],
        0.18,
    ),
    ("Firefox", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0", ["131", "132", "133"], 0.11),
    (
        "Edge",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36 Edg/{v}.0.0.0",
        ["130", "131", "132"],
        0.13,
    ),
]
WEB_SCREENS = [
    (1920, 1080, 1, 0.35),
    (1536, 864, 1.25, 0.15),
    (1440, 900, 2, 0.15),
    (2560, 1440, 1, 0.12),
    (1366, 768, 1, 0.1),
    (1280, 800, 2, 0.13),
]

# (model id, marketing name, width, height, launch OS major, weight). The OS version is
# drawn only from releases the device can run (>= the OS it shipped with).
IOS_DEVICES = [
    ("iPhone16,2", "iPhone 15 Pro Max", 1290, 2796, 17, 0.18),
    ("iPhone16,1", "iPhone 15 Pro", 1179, 2556, 17, 0.14),
    ("iPhone15,4", "iPhone 15", 1179, 2556, 17, 0.16),
    ("iPhone17,1", "iPhone 16 Pro", 1206, 2622, 18, 0.17),
    ("iPhone14,7", "iPhone 14", 1170, 2532, 16, 0.15),
    ("iPhone14,5", "iPhone 13", 1170, 2532, 15, 0.12),
    ("iPad13,18", "iPad (10th gen)", 1640, 2360, 16, 0.08),
]
IOS_VERSIONS = [("18.1", 0.35), ("18.0.1", 0.2), ("17.6.1", 0.25), ("17.5", 0.1), ("16.7.10", 0.1)]
ANDROID_DEVICES = [
    ("samsung", "SM-S928B", "Galaxy S24 Ultra", 1440, 3120, 14, 0.16),
    ("samsung", "SM-A546B", "Galaxy A54", 1080, 2340, 13, 0.18),
    ("Google", "Pixel 8", "Pixel 8", 1080, 2400, 14, 0.12),
    ("Google", "Pixel 9 Pro", "Pixel 9 Pro", 1280, 2856, 14, 0.08),
    ("Xiaomi", "23078RKD5C", "Redmi Note 13", 1080, 2400, 13, 0.16),
    ("OnePlus", "CPH2581", "OnePlus 12", 1440, 3168, 14, 0.08),
    ("motorola", "moto g84 5G", "moto g84", 1080, 2400, 13, 0.1),
    ("OPPO", "CPH2525", "Reno 10", 1080, 2412, 13, 0.12),
]
ANDROID_VERSIONS = [("15", 0.2), ("14", 0.42), ("13", 0.22), ("12", 0.11), ("11", 0.05)]
ANDROID_BUILD_IDS = {"15": "AP3A", "14": "UP1A", "13": "TP1A", "12": "SP1A", "11": "RP1A"}
CARRIERS = {
    "US": ["Verizon", "AT&T", "T-Mobile"],
    "GB": ["EE", "Vodafone UK", "O2"],
    "DE": ["Telekom.de", "Vodafone.de", "o2 - de"],
    "FR": ["Orange F", "SFR", "Free"],
    "ES": ["Movistar", "Vodafone ES"],
    "JP": ["NTT DOCOMO", "au", "SoftBank"],
    "AU": ["Telstra", "Optus"],
    "SG": ["Singtel", "StarHub"],
    "KR": ["SKTelecom", "KT"],
    "BR": ["Vivo", "Claro BR", "TIM"],
    "MX": ["Telcel", "AT&T MX"],
    "IN": ["Jio", "Airtel", "Vi"],
}

PC_OS = [
    ("Windows", "10.0.22631", "Windows 11 23H2", 0.5),
    ("Windows", "10.0.26100", "Windows 11 24H2", 0.25),
    ("Windows", "10.0.19045", "Windows 10 22H2", 0.25),
]
PC_GPUS = [
    ("NVIDIA GeForce RTX 4070", 0.2),
    ("NVIDIA GeForce RTX 3060", 0.22),
    ("NVIDIA GeForce RTX 4090", 0.05),
    ("AMD Radeon RX 7800 XT", 0.12),
    ("NVIDIA GeForce GTX 1660 SUPER", 0.15),
    ("AMD Radeon RX 6600", 0.14),
    ("Intel Arc A770", 0.04),
    ("NVIDIA GeForce RTX 4060 Laptop GPU", 0.08),
]
PC_SCREENS = [(1920, 1080, 0.55), (2560, 1440, 0.3), (3840, 2160, 0.08), (3440, 1440, 0.07)]
LAUNCHERS = [("Steam", 0.72), ("Epic Games", 0.2), ("Standalone", 0.08)]
PS5_MODELS = [("CFI-1216A", "PlayStation 5", 0.35), ("CFI-2016", "PlayStation 5 Slim", 0.45), ("CFI-7019", "PlayStation 5 Pro", 0.2)]
PS5_SYSTEM = [("24.06-10.00.00", 0.55), ("24.05-09.60.00", 0.3), ("24.04-09.40.00", 0.15)]
PS5_OUTPUTS = [(3840, 2160, 0.6), (2560, 1440, 0.1), (1920, 1080, 0.3)]


def _os_for(rng: random.Random, versions: list, min_major: int) -> str:
    ok = [v for v in versions if int(v[0].split(".")[0]) >= min_major]
    return weighted(rng, ok or versions[:1])[0]


def _id(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def build_context(platform: str, user: User, app: dict, rng_seed: int) -> dict:
    """Session-stable context. The emitter adds `page` per event on web."""
    rng = random.Random(rng_seed)
    ctx: dict = {
        "ip": user.ip,
        "locale": user.locale,
        "timezone": user.timezone,
        "library": dict(LIBRARIES[platform]),
    }
    if platform == "web":
        _, ua, versions, _ = weighted(rng, BROWSERS)
        w, h, dens, _ = weighted(rng, WEB_SCREENS)
        ctx["userAgent"] = ua.format(v=rng.choice(versions))
        ctx["screen"] = {"width": w, "height": h, "density": dens}
    elif platform in ("ios", "android"):
        if platform == "ios":
            model, name, w, h, min_os, _ = weighted(rng, IOS_DEVICES)
            manufacturer = "Apple"
            os_ver = _os_for(rng, IOS_VERSIONS, min_os)
            family = "iPad" if model.startswith("iPad") else "iPhone"
            os_name = "iPadOS" if family == "iPad" else "iOS"
            adid = _id(rng).upper()
            ua = f"{app['name']}/{app['version']} ({family}; iOS {os_ver}; Scale/{2 if family == 'iPad' else 3}.00)"
        else:
            manufacturer, model, name, w, h, min_os, _ = weighted(rng, ANDROID_DEVICES)
            os_name = "Android"
            os_ver = _os_for(rng, ANDROID_VERSIONS, min_os)
            adid = _id(rng)
            ua = f"Dalvik/2.1.0 (Linux; U; Android {os_ver}; {model} Build/{ANDROID_BUILD_IDS.get(os_ver, 'UP1A')})"
        wifi = rng.random() < 0.62
        ctx.update(
            {
                "app": {
                    "name": app["name"],
                    "version": app["version"],
                    "build": str(app["build"]),
                    "namespace": app.get("namespace_" + platform, app.get("namespace", "")),
                },
                "device": {
                    "id": _id(rng),
                    "advertisingId": adid,
                    "adTrackingEnabled": rng.random() < 0.35,
                    "manufacturer": manufacturer,
                    "model": model,
                    "name": name,
                    "type": platform,
                },
                "os": {"name": os_name, "version": os_ver},
                "network": {
                    "wifi": wifi,
                    "cellular": not wifi,
                    "bluetooth": rng.random() < 0.3,
                    "carrier": rng.choice(CARRIERS.get(user.country, ["Carrier"])),
                },
                "screen": {"width": w, "height": h, "density": (2 if model.startswith("iPad") else 3) if platform == "ios" else 2.75},
                "userAgent": ua,
            }
        )
    elif platform == "pc":
        os_name, os_ver, os_label, _ = weighted(rng, PC_OS)
        gpu = weighted(rng, PC_GPUS)[0]
        w, h, _ = weighted(rng, PC_SCREENS)
        launcher = weighted(rng, LAUNCHERS)[0]
        ctx.update(
            {
                "app": {"name": app["name"], "version": app["version"], "build": str(app["build"]), "launcher": launcher},
                "device": {
                    "id": _id(rng),
                    "type": "pc",
                    "manufacturer": rng.choice(["Custom", "Dell", "ASUS", "Lenovo", "HP", "MSI"]),
                    "model": "Desktop" if rng.random() < 0.7 else "Laptop",
                    "gpu": gpu,
                    "cpu_cores": rng.choice([6, 8, 12, 16]),
                    "ram_gb": rng.choice([16, 16, 32, 32, 64]),
                },
                "os": {"name": os_name, "version": os_ver, "label": os_label},
                "screen": {"width": w, "height": h},
                "network": {"wifi": rng.random() < 0.25},
            }
        )
    elif platform == "ps5":
        model, name, _ = weighted(rng, PS5_MODELS)
        sysver = weighted(rng, PS5_SYSTEM)[0]
        out_w, out_h, _ = weighted(rng, PS5_OUTPUTS)
        ctx.update(
            {
                "app": {
                    "name": app["name"],
                    "version": app["version"],
                    "build": str(app["build"]),
                    "title_id": app.get("ps5_title_id", "PPSA01234_00"),
                },
                "device": {"id": _id(rng), "type": "console", "manufacturer": "Sony", "model": model, "name": name},
                "os": {"name": "PlayStation OS", "version": sysver},
                "screen": {"width": out_w, "height": out_h},
                "network": {"wifi": rng.random() < 0.4},
            }
        )
    else:
        raise ValueError(f"unknown platform {platform}")
    return ctx


def web_page(base_url: str, path: str, title: str, referrer: str = "", search: str = "") -> dict:
    return {"path": path, "url": base_url + path + search, "title": title, "referrer": referrer, "search": search}
