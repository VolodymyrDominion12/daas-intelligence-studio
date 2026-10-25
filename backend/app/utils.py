"""Small, dependency-light helpers shared by every layer."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import unicodedata
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    # Microseconds matter: several runs can start within the same second.
    return (dt or utcnow()).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# text / ids
# ---------------------------------------------------------------------------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ye",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "yu", "я": "ya", "ы": "y", "э": "e", "ё": "e", "ъ": "", "Ä": "a",
}

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_len: int = 60) -> str:
    """ASCII-ish slug that keeps Ukrainian names readable (``ТОВ «Альфа»`` -> ``tov-alfa``)."""
    if not value:
        return ""
    lowered = value.casefold()
    transliterated = "".join(_TRANSLIT.get(ch, ch) for ch in lowered)
    normalized = unicodedata.normalize("NFKD", transliterated)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _NON_WORD.sub("-", ascii_only).strip("-")
    return slug[:max_len].strip("-")


def short_hash(value: str, *, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_company_name(value: str) -> str:
    text = normalize_space(value).casefold()
    text = re.sub(r"[\"'«»„“”()\[\]{}.,;:!?/\\\-–—–_]", " ", text)
    text = re.sub(
        r"\b(тов|фоп|пп|ат|прат|тзов|llc|ltd|inc|corp|co|company|компанія|компания)\b",
        " ",
        text,
    )
    return normalize_space(text)


def normalize_domain(value: str) -> str:
    if not value:
        return ""
    candidate = value.strip().lower()
    if "//" not in candidate:
        candidate = "http://" + candidate
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    return host.strip(".")


def normalize_authority(value: str) -> str:
    """Host + non-default port; used for identity of real company domains."""
    if not value:
        return ""
    candidate = value.strip().lower()
    if "//" not in candidate:
        candidate = "http://" + candidate
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""
    host = (parts.hostname or "").removeprefix("www.")
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


def is_private_host(host: str) -> bool:
    bare = host.strip("[]").split(":")[0].lower()
    if bare in {"localhost", "127.0.0.1", "::1"} or bare.endswith(".localhost"):
        return True
    try:
        return is_private_ip(bare)
    except ValueError:
        return False


def record_key_for(payload: dict[str, Any], *, fallback: str = "") -> str:
    """Stable natural key: official domain first, then normalized name + city.

    Loopback/private hosts are not real company domains (demos, intranets), so identity
    for them falls back to the company name — otherwise several demo sites on one host
    would collapse into a single record.
    """
    website = payload.get("website") or payload.get("domain") or ""
    authority = normalize_authority(str(website)) if website else ""
    if authority and not is_private_host(authority):
        return f"d:{authority}"
    name = normalize_company_name(str(payload.get("name") or payload.get("company") or ""))
    city = normalize_company_name(str(payload.get("city") or payload.get("location") or ""))
    digest_basis = f"{name}|{city}" if name else str(fallback)
    if not digest_basis:
        return f"h:{short_hash(json.dumps(payload, sort_keys=True, default=str), length=16)}"
    slug = slugify(f"{name} {city}".strip(), max_len=48) or "record"
    return f"n:{slug}-{short_hash(digest_basis, length=6)}"


# ---------------------------------------------------------------------------
# value coercion (trusted only after Pydantic validation)
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"-?\d[\d\s\u00a0\u202f.,']*")
_CURRENCY_RE = re.compile(r"\b(UAH|USD|EUR|₴|\$|€|грн|uah)\b", re.IGNORECASE)


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = normalize_space(str(value)).replace("\u00a0", "").replace("\u202f", "")
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").replace("'", "")
    # "1.234,56" (uk/de) vs "1,234.56" (en)
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(",") == 1 and len(raw.split(",")[-1]) != 3:
        raw = raw.replace(",", ".")
    elif raw.count(",") > 1:
        raw = raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except (ValueError, InvalidOperation):
        return None


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = normalize_space(str(value)).casefold()
    if text in {"true", "yes", "y", "1", "так", "є", "available", "in stock"}:
        return True
    if text in {"false", "no", "n", "0", "ні", "немає", "out of stock", "sold out"}:
        return False
    return None


def parse_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_space(str(value))
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt).isoformat()
        except ValueError:
            continue
    return text


def coerce_value(value: Any, dtype: str) -> Any:
    """Best-effort conversion; returns ``None`` when the value is unusable."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if dtype == "number":
        return parse_number(value)
    if dtype == "integer":
        number = parse_number(value)
        return int(number) if number is not None else None
    if dtype == "boolean":
        return parse_bool(value)
    if dtype == "email":
        match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", str(value))
        return match.group(0).lower() if match else None
    if dtype == "url":
        text = normalize_space(str(value))
        return text if text.startswith(("http://", "https://")) else None
    if dtype == "phone":
        digits = normalize_phone(str(value))
        return digits or None
    if dtype == "datetime":
        return parse_datetime(value)
    if dtype == "array":
        if isinstance(value, list):
            return [normalize_space(str(v)) for v in value if normalize_space(str(v))]
        parts = re.split(r"[,;|]", str(value))
        return [normalize_space(p) for p in parts if normalize_space(p)]
    if dtype == "json":
        return value
    text = normalize_space(str(value))
    return text or None


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", value or "")
    if digits.startswith("+380") and len(digits) == 13:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if 10 <= len(digits.lstrip("+")) <= 15:
        return digits if digits.startswith("+") else "+" + digits
    return ""


# ---------------------------------------------------------------------------
# extraction from text
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?<![\d+])(?:"
    r"\+?38[\s(-]*0?[\s(-]*\d{2,3}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
    r"|0\d{2,3}[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
    r"|\(0\d{2,3}\)[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
    r")\b"
)

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
MONEY_RE = re.compile(
    r"(\d[\d\s\u00a0.,]*)\s*(₴|грн|uah|usd|\$|€|eur)", re.IGNORECASE
)
RATING_RE = re.compile(
    r"\b([0-5](?:[.,]\d)?)\s*(?:/|з|із|out of)?\s*5?\s*(?:зірк|stars?|★)?", re.IGNORECASE
)


def find_emails(text: str, *, limit: int = 20) -> list[str]:
    found: list[str] = []
    for match in EMAIL_RE.finditer(text or ""):
        email = match.group(0).lower().rstrip(".")
        if any(email.endswith(suffix) for suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if email not in found:
            found.append(email)
        if len(found) >= limit:
            break
    return found


def find_phones(text: str, *, limit: int = 15) -> list[str]:
    found: list[str] = []
    for match in PHONE_RE.finditer(text or ""):
        phone = normalize_phone(match.group(0))
        if phone and phone not in found:
            found.append(phone)
        if len(found) >= limit:
            break
    return found


def find_money(text: str, *, limit: int = 10) -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    for match in MONEY_RE.finditer(text or ""):
        number = parse_number(match.group(1))
        if number is not None and number > 0:
            found.append((number, match.group(2).lower()))
        if len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------
# PII helpers (masking for samples / redaction before cloud LLM)
# ---------------------------------------------------------------------------


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return "***"
    return f"+{digits[:3]}***{digits[-2:]}"


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    return text


def mask_value(value: Any, dtype: str) -> Any:
    if value is None:
        return None
    if dtype == "email":
        return mask_email(str(value))
    if dtype == "phone":
        return mask_phone(str(value))
    if isinstance(value, str):
        return redact_pii(value)
    return value


# ---------------------------------------------------------------------------
# URLs & safety
# ---------------------------------------------------------------------------

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}


class UnsafeUrlError(ValueError):
    pass


def is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def assert_public_url(url: str, *, allow_private: bool = False) -> str:
    """SSRF guard for user-supplied URLs (redirect targets must be checked too)."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError(f"unsupported scheme: {parts.scheme or 'none'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("missing host")
    if allow_private:
        return url
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise UnsafeUrlError(f"private host blocked: {host}")
    if is_private_ip(host):
        raise UnsafeUrlError(f"private ip blocked: {host}")
    # DNS name that resolves into a private range is also unsafe.
    if not _is_ip_literal(host):
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            infos = []
        for info in infos:
            address = info[4][0]
            if is_private_ip(address):
                raise UnsafeUrlError(f"host resolves to private ip: {host} -> {address}")
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def canonical_url(url: str, *, drop_query: bool = True) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = "" if drop_query else parts.query
    return urlunsplit((scheme, netloc, path, query, ""))


def same_site(url_a: str, url_b: str) -> bool:
    return normalize_domain(url_a) == normalize_domain(url_b)


def is_plausible_content_url(url: str) -> bool:
    """Filter navigation noise while expanding a crawl frontier."""
    lowered = url.lower()
    if any(lowered.endswith(ext) for ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg",
                                             ".webp", ".mp4", ".zip", ".doc", ".docx", ".xml")):
        return False
    noisy = (
        "/login", "/signin", "/signup", "/cart", "/basket", "/checkout", "/privacy", "/terms",
        "/cookie", "/gdpr", "/unsubscribe", "/search", "javascript:", "mailto:", "tel:",
        "/wp-admin", "/feed", "/rss", "/sitemap",
    )
    return not any(token in lowered for token in noisy)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def unique(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item, None)
    return list(seen)


def safe_filename(name: str, *, default: str = "dataset") -> str:
    base = slugify(name, max_len=48) or default
    return base


def human_bytes(size: int | float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
