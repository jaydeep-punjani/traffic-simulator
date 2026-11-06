"""E-commerce vertical: Segment E-commerce Spec events."""

from __future__ import annotations

import bisect
import random

from ..journeys import Session, VerticalRuntime, cumulative, zipf_weights

CATEGORIES = {
    "Electronics": (["Headphones", "Smartwatch", "Speaker", "Charger", "Tablet", "Earbuds", "Monitor"], 120, 0.9),
    "Fashion": (["Sneakers", "Jacket", "T-Shirt", "Jeans", "Dress", "Backpack", "Sunglasses"], 45, 0.7),
    "Home": (["Lamp", "Cushion", "Coffee Maker", "Blender", "Rug", "Cookware Set", "Vacuum"], 60, 0.8),
    "Beauty": (["Serum", "Moisturizer", "Perfume", "Lipstick", "Shampoo", "Face Mask"], 25, 0.6),
    "Sports": (["Yoga Mat", "Dumbbells", "Running Shorts", "Water Bottle", "Bike Helmet"], 40, 0.7),
    "Toys": (["Building Set", "Puzzle", "Board Game", "Plush Toy", "RC Car"], 30, 0.6),
}
BRANDS = ["Nova", "Arcadia", "Kestrel", "Lumen", "Orbit", "Willow", "Summit", "Pixel&Co", "Terra", "Vanta"]
ADJ = ["Pro", "Lite", "Max", "Classic", "Air", "Ultra", "Eco", "Mini", "Plus", "Studio"]
PAYMENT = [("credit_card", 0.55), ("paypal", 0.2), ("apple_pay", 0.12), ("google_pay", 0.08), ("klarna", 0.05)]
SHIPPING = ["standard", "express", "next_day", "pickup"]


class EcomRuntime(VerticalRuntime):
    vertical = "ecom"

    def setup(self):
        rng = random.Random(f"{self.seed}:ecom-catalog")
        n = int(self.params.get("products", 2000))
        self.currency = self.params.get("currency", "USD")
        cats = list(CATEGORIES)
        self.products = []
        self.by_cat: dict[str, list[int]] = {c: [] for c in cats}
        for i in range(n):
            cat = rng.choice(cats)
            nouns, base, sigma = CATEGORIES[cat]
            brand = rng.choice(BRANDS)
            name = f"{brand} {rng.choice(nouns)} {rng.choice(ADJ)}"
            price = round(max(4.99, rng.lognormvariate(0, sigma) * base), 2)
            pid = f"prd-{i:06d}"
            self.products.append({"product_id": pid, "sku": f"SKU-{rng.randint(100000, 999999)}", "category": cat,
                                  "name": name, "brand": brand, "variant": rng.choice(["Black", "White", "Blue", "Red", "Green", "Default"]),
                                  "price": price, "url": f"/p/{pid}", "image_url": f"https://cdn.shopnow.example/img/{pid}.jpg"})
            self.by_cat[cat].append(i)
        self.cum = cumulative(zipf_weights(n, 1.02))
        self.props.update({
            "products_searched": self.p_search,
            "product_list_viewed": self.p_list,
            "product_viewed": self.p_product,
            "product_added": self.p_add,
            "product_removed": self.p_remove,
            "product_added_to_wishlist": self.p_wishlist,
            "cart_viewed": self.p_cart,
            "checkout_started": self.p_checkout,
            "coupon_applied": self.p_coupon,
            "checkout_step_completed": self.p_step,
            "payment_info_entered": self.p_payment,
            "order_completed": self.p_order,
        })

    # ---- requirements for guarded states
    def requirement(self, s: Session, name: str) -> bool:
        if name == "cart":
            return bool(s.data.get("cart"))
        if name == "checkout":
            return bool(s.data.get("cart")) and "order_id" in s.data
        if name == "product":
            return "product" in s.data
        return super().requirement(s, name)

    # ---- product selection
    def _sale(self, s: Session) -> dict | None:
        return self.featured(s, "featured_skus")

    def _pick(self, s: Session, category: str | None = None) -> dict:
        sale = self._sale(s)
        if sale and self.rng.random() < float(sale.get("share", 0.4)) * sale["_intensity"]:
            k = int(sale.get("featured_skus", 20))
            p = self.products[self.rng.randrange(min(k, len(self.products)))]
            return {**p, "price": round(p["price"] * (1 - float(sale.get("discount", 0.2))), 2), "sale": sale.get("sale_name", sale["_name"])}
        if category:
            idxs = self.by_cat[category]
            return self.products[idxs[min(len(idxs) - 1, int(self.rng.random() ** 2.2 * len(idxs)))]]
        i = bisect.bisect_left(self.cum, self.rng.random() * self.cum[-1])
        return self.products[min(i, len(self.products) - 1)]

    def _line(self, p: dict, qty: int = 1, position: int | None = None) -> dict:
        d = {k: p[k] for k in ("product_id", "sku", "category", "name", "brand", "variant", "price")}
        d["quantity"] = qty
        if position is not None:
            d["position"] = position
        return d

    def _cart_id(self, s: Session) -> str:
        cid = s.data.get("cart_id")
        if not cid:
            cid = s.data["cart_id"] = f"cart-{s.anon_id[:13]}"
        return cid

    # ---- property builders (may mutate session state)
    def p_search(self, s):
        cat = self.rng.choice(list(CATEGORIES))
        q = self.rng.choice(CATEGORIES[cat][0]).lower()
        s.data["category"] = cat
        s.data["query"] = q
        return {"query": q}

    def p_list(self, s):
        cat = s.data.pop("category", None) or self.rng.choice(list(CATEGORIES))
        items = [self._pick(s, cat) for _ in range(8)]
        s.data["list"] = items
        s.data["list_category"] = cat
        return {"list_id": f"{cat.lower()}-{'search' if 'query' in s.data else 'browse'}", "category": cat,
                "products": [self._line(p, 1, i + 1) for i, p in enumerate(items)]}

    def p_product(self, s):
        lst = s.data.get("list")
        p = self.rng.choice(lst) if lst and self.rng.random() < 0.8 else self._pick(s)
        s.data["product"] = p
        s.data["product_slug"] = p["product_id"]
        props = {**self._line(p), "currency": self.currency, "value": p["price"], "url": "https://www.shopnow.example" + p["url"],
                 "image_url": p["image_url"]}
        if "sale" in p:
            props["sale"] = p["sale"]
        return props

    def p_add(self, s):
        p = s.data.get("product") or self._pick(s)
        qty = 1 if self.rng.random() < 0.85 else self.rng.randint(2, 3)
        cart = s.data.setdefault("cart", [])
        cart.append(self._line(p, qty))
        return {"cart_id": self._cart_id(s), **self._line(p, qty), "currency": self.currency, "value": round(p["price"] * qty, 2)}

    def p_remove(self, s):
        cart = s.data.get("cart") or []
        if not cart:
            return {"cart_id": self._cart_id(s)}
        line = cart.pop(self.rng.randrange(len(cart)))
        return {"cart_id": self._cart_id(s), **line}

    def p_wishlist(self, s):
        p = s.data.get("product") or self._pick(s)
        return {"wishlist_id": f"wl-{s.user.index}", "wishlist_name": "My Wishlist", **self._line(p)}

    def p_cart(self, s):
        cart = s.data.get("cart") or []
        return {"cart_id": self._cart_id(s), "products": [dict(line) for line in cart],
                "value": round(sum(x["price"] * x["quantity"] for x in cart), 2), "currency": self.currency}

    def _totals(self, s):
        cart = s.data.get("cart") or []
        subtotal = round(sum(x["price"] * x["quantity"] for x in cart), 2)
        discount = s.data.get("discount", 0.0)
        shipping = 0.0 if subtotal > 50 else 5.99
        tax = round((subtotal - discount) * 0.08, 2)
        revenue = round(subtotal - discount, 2)
        return {"order_id": s.data["order_id"], "affiliation": "ShopNow " + s.platform, "value": revenue, "revenue": revenue,
                "shipping": shipping, "tax": tax, "discount": discount, "coupon": s.data.get("coupon"),
                "currency": self.currency, "products": [dict(line) for line in cart]}

    def p_checkout(self, s):
        s.data["order_id"] = f"ord-{s.anon_id[:8]}-{s.seq}"
        s.data["checkout_id"] = f"chk-{s.anon_id[:8]}-{s.seq}"
        return self._totals(s)

    def p_coupon(self, s):
        sale = self._sale(s)
        code = (sale or {}).get("coupon") or self.rng.choice(["WELCOME10", "SAVE15", "FREESHIP"])
        cart = s.data.get("cart") or []
        subtotal = sum(x["price"] * x["quantity"] for x in cart)
        disc = round(subtotal * (0.15 if not sale else float(sale.get("discount", 0.2)) / 2), 2)
        s.data["coupon"] = code
        s.data["discount"] = disc
        return {"order_id": s.data.get("order_id"), "cart_id": self._cart_id(s), "coupon_id": code, "coupon_name": code, "discount": disc}

    def p_step(self, s):
        step = s.data.get("step", 0) + 1
        s.data["step"] = step
        return {"checkout_id": s.data.get("checkout_id"), "step": step, "shipping_method": self.rng.choice(SHIPPING)}

    def p_payment(self, s):
        method = self._weighted(PAYMENT)
        s.data["payment"] = method
        return {"checkout_id": s.data.get("checkout_id"), "order_id": s.data.get("order_id"), "step": s.data.get("step", 1) + 1,
                "payment_method": method}

    def p_order(self, s):
        props = self._totals(s)
        props["checkout_id"] = s.data.get("checkout_id")
        props["total"] = round(props["revenue"] + props["shipping"] + props["tax"], 2)
        props["payment_method"] = s.data.get("payment", "credit_card")
        for k in ("cart", "order_id", "checkout_id", "coupon", "discount", "step", "payment"):
            s.data.pop(k, None)
        return props

    def _weighted(self, items):
        x = self.rng.random() * sum(w for _, w in items)
        for v, w in items:
            x -= w
            if x <= 0:
                return v
        return items[-1][0]

    # ---- session hooks
    def on_start(self, s: Session):
        push = self.featured(s, "campaign")
        if push and s.platform in ("ios", "android") and self.rng.random() < float(push.get("push_open_share", 0.6)) * push["_intensity"]:
            s.ctx = {**s.ctx, "campaign": push["campaign"]}
            self.track(s, "push_opened", {"campaign": push["campaign"]["name"], "action": "open"})
        return
        yield
