"""Fingerprint-based tech stack detection (no external service, no browser)."""

from __future__ import annotations

import re

SIGNATURES: list[tuple[str, str, re.Pattern[str]]] = [
    # CMS / site builders
    ("WordPress", "cms", re.compile(r"wp-content|wp-includes|/wp-json|generator[^>]*wordpress", re.I)),
    ("WooCommerce", "ecommerce", re.compile(r"woocommerce", re.I)),
    ("Shopify", "ecommerce", re.compile(r"cdn\.shopify\.com|shopify\.theme", re.I)),
    ("Wix", "builder", re.compile(r"wix\.com|wixstatic", re.I)),
    ("Webflow", "builder", re.compile(r"webflow\.(com|io)|data-wf-page", re.I)),
    ("Tilda", "builder", re.compile(r"tilda\.(ws|cc)|tildacdn", re.I)),
    ("Bitrix", "cms", re.compile(r"bitrix|b24", re.I)),
    ("OpenCart", "ecommerce", re.compile(r"opencart", re.I)),
    ("Joomla", "cms", re.compile(r"/media/jui/|joomla", re.I)),
    ("Drupal", "cms", re.compile(r"drupal-settings-json|sites/default/files", re.I)),
    ("Elementor", "builder", re.compile(r"elementor", re.I)),
    ("HubSpot CMS", "cms", re.compile(r"hs-scripts\.com|hubspot\.net/hubfs", re.I)),
    # commerce / payments
    ("Stripe", "payments", re.compile(r"js\.stripe\.com", re.I)),
    ("LiqPay", "payments", re.compile(r"liqpay", re.I)),
    ("WayForPay", "payments", re.compile(r"wayforpay", re.I)),
    ("Monobank", "payments", re.compile(r"monobank", re.I)),
    ("Prom.ua", "marketplace", re.compile(r"prom\.ua", re.I)),
    # chat / crm
    ("Intercom", "chat", re.compile(r"intercom(cdn|\.io)", re.I)),
    ("Crisp", "chat", re.compile(r"crisp\.chat", re.I)),
    ("Tawk.to", "chat", re.compile(r"tawk\.to", re.I)),
    ("JivoSite", "chat", re.compile(r"jivosite|jivo\.site", re.I)),
    ("LiveChat", "chat", re.compile(r"livechatinc", re.I)),
    ("HubSpot", "crm", re.compile(r"hubspot", re.I)),
    ("Salesforce", "crm", re.compile(r"salesforce|force\.com", re.I)),
    ("Calendly", "booking", re.compile(r"calendly\.com", re.I)),
    ("Booksy", "booking", re.compile(r"booksy\.com", re.I)),
    ("Dikidi", "booking", re.compile(r"dikidi", re.I)),
    # analytics
    ("Google Analytics 4", "analytics", re.compile(r"gtag/js\?id=G-|googletagmanager\.com/gtag", re.I)),
    ("Google Tag Manager", "analytics", re.compile(r"googletagmanager\.com/gtm", re.I)),
    ("Meta Pixel", "analytics", re.compile(r"connect\.facebook\.net/.*fbevents", re.I)),
    ("Hotjar", "analytics", re.compile(r"hotjar", re.I)),
    ("Clarity", "analytics", re.compile(r"clarity\.ms", re.I)),
    # infra
    ("Cloudflare", "infra", re.compile(r"cloudflare", re.I)),
    ("Next.js", "framework", re.compile(r"/_next/static", re.I)),
    ("Open Graph", "meta", re.compile(r'property=["\']og:', re.I)),
]

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "linkedin": re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[^\s\"'<>]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[^\s\"'<>]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>]+", re.I),
    "telegram": re.compile(r"https?://t\.me/[^\s\"'<>]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/[^\s\"'<>]+", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[^\s\"'<>]+", re.I),
}

BOOKING_HINTS = re.compile(
    r"онлайн[- ]?запис|записатися|забронювати|booking|book now|запись онлайн|calendly|booksy|dikidi",
    re.I,
)


def detect_tech_stack(html: str) -> list[str]:
    return sorted({name for name, _category, pattern in SIGNATURES if pattern.search(html)})


def detect_socials(html: str) -> dict[str, str]:
    socials: dict[str, str] = {}
    for name, pattern in SOCIAL_PATTERNS.items():
        match = pattern.search(html)
        if match:
            socials[name] = match.group(0).rstrip("/\"'<>")
    return socials


def detect_booking(html: str) -> bool:
    if any(name in detect_tech_stack(html) for name in ("Calendly", "Booksy", "Dikidi")):
        return True
    return bool(BOOKING_HINTS.search(html))


def detect_chat(html: str) -> bool:
    stack = set(detect_tech_stack(html))
    return bool(stack & {"Intercom", "Crisp", "Tawk.to", "JivoSite", "LiveChat"})
