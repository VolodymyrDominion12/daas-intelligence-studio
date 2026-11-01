"""Field-name synonyms (uk/en) used for selector learning and JSON-LD mapping."""

from __future__ import annotations

import re

from ..models import FieldSpec

SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("name", "назва", "название", "title", "найменування", "товар", "компанія"),
    "title": ("title", "назва", "заголовок", "посада", "вакансія", "найменування"),
    "website": ("website", "site", "сайт", "url", "домен", "посилання", "link"),
    "domain": ("domain", "домен", "сайт", "website"),
    "phone": ("phone", "телефон", "тел", "контакт", "мобільний", "номер"),
    "email": ("email", "e-mail", "пошта", "почта", "mail", "контакти"),
    "address": ("address", "адреса", "адрес", "location", "розташування", "вулиця"),
    "city": ("city", "місто", "город", "населений", "локація"),
    "region": ("region", "область", "район", "region"),
    "price": (
        "price", "ціна", "цена", "вартість", "стоимость", "cost", "amount", "тариф", "від",
    ),
    "currency": ("currency", "валюта", "грн", "uah", "usd"),
    "description": ("description", "опис", "описание", "about", "про", "деталі", "характеристик"),
    "rating": ("rating", "рейтинг", "оцінка", "оценка", "stars", "зорі", "зірки"),
    "reviews": ("reviews", "reviews_count", "відгук", "отзыв", "review"),
    "category": ("category", "категорія", "категория", "рубрика", "тип"),
    "image": ("image", "photo", "фото", "зображення", "картинка", "image"),
    "sku": ("sku", "артикул", "код", "id", "код_товару"),
    "availability": ("availability", "наявність", "наличие", "в наявності", "stock", "статус"),
    "company": ("company", "компанія", "компания", "роботодавець", "бренд", "brand", "seller"),
    "job_title": ("job_title", "position", "посада", "вакансія", "вакансия", "profession"),
    "salary": ("salary", "зарплата", "зарплатня", "оплата", "винагорода", "salary"),
    "posted_at": ("posted_at", "date", "дата", "опубліковано", "published", "created"),
    "tags": ("tags", "теги", "мітки", "features", "особливості"),
}

STOPWORDS = {"uk", "ua", "com", "the", "and", "or", "for", "with", "by", "of", "to", "in"}


def normalize_token(value: str) -> str:
    return re.sub(r"[^a-zа-яіїєґ0-9]+", "_", value.casefold()).strip("_")


def field_tokens(field: FieldSpec) -> set[str]:
    tokens: set[str] = set()
    for raw in (field.key, field.title, *field.aliases):
        normalized = normalize_token(raw)
        if not normalized:
            continue
        tokens.add(normalized)
        for part in normalized.split("_"):
            if len(part) >= 3 and part not in STOPWORDS:
                tokens.add(part)
    for synonym_key, values in SYNONYMS.items():
        if synonym_key in tokens or any(normalize_token(v) in tokens for v in values):
            tokens.update(normalize_token(v) for v in values)
    return {token for token in tokens if token}


def known_synonyms(name: str) -> set[str]:
    key = normalize_token(name)
    return {normalize_token(v) for v in SYNONYMS.get(key, ())}


def text_matches_field(text: str, field: FieldSpec, *, threshold: float = 0.5) -> float:
    """Crude lexical overlap between a label/descriptor and a field definition."""
    tokens = field_tokens(field)
    normalized = normalize_token(text)
    if not normalized:
        return 0.0
    parts = set(normalized.split("_"))
    if normalized in tokens:
        return 1.0
    hits = tokens & parts
    if not hits:
        return 0.0
    return min(1.0, len(hits) / max(1, len(parts))) + (0.3 if len(hits) > 1 else 0.0)
