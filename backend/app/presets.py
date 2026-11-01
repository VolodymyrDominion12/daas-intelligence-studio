"""Offline (LLM-free) task templates so the studio works before any model is configured."""

from __future__ import annotations

import re

from .models import EnrichmentSpec, FieldSpec, Limits, TaskSpec


def col(
    key: str,
    title: str,
    dtype: str = "string",
    *,
    aliases: tuple[str, ...] = (),
    method: str = "css",
    pii: str = "none",
    prompt: str | None = None,
    description: str = "",
) -> FieldSpec:
    return FieldSpec(
        key=key,
        title=title,
        dtype=dtype,  # type: ignore[arg-type]
        aliases=list(aliases),
        method=method,  # type: ignore[arg-type]
        pii=pii,  # type: ignore[arg-type]
        prompt=prompt,
        description=description or title,
    )


CONTACT_ALIASES = ("телефон", "тел", "phone", "контакт")
EMAIL_ALIASES = ("email", "пошта", "e-mail", "mail", "контакти")
WEBSITE_ALIASES = ("сайт", "website", "url", "link", "перейти")
NAME_ALIASES = ("назва", "name", "найменування", "title", "компанія", "компания")
CITY_ALIASES = ("місто", "city", "населений пункт", "локація", "город")
ADDRESS_ALIASES = ("адреса", "address", "вулиця", "location")
PRICE_ALIASES = ("ціна", "price", "вартість", "цена", "стоимость", "від")


def _company_columns() -> list[FieldSpec]:
    return [
        col("name", "Назва компанії", aliases=NAME_ALIASES),
        col("website", "Сайт", "url", aliases=WEBSITE_ALIASES),
        col("city", "Місто", aliases=CITY_ALIASES),
        col("address", "Адреса", aliases=ADDRESS_ALIASES),
        col("phone", "Телефон", "phone", aliases=CONTACT_ALIASES, pii="business_contact"),
        col("email", "Email", "email", aliases=EMAIL_ALIASES, pii="business_contact"),
        col("category", "Сфера діяльності", aliases=("категорія", "сфера", "industry", "напрям")),
        col("description", "Опис", aliases=("опис", "description", "про компанію")),
    ]


def _local_business_columns() -> list[FieldSpec]:
    return [
        col("name", "Назва", aliases=NAME_ALIASES),
        col("city", "Місто", aliases=CITY_ALIASES),
        col("address", "Адреса", aliases=ADDRESS_ALIASES),
        col("phone", "Телефон", "phone", aliases=CONTACT_ALIASES, pii="business_contact"),
        col("website", "Сайт", "url", aliases=WEBSITE_ALIASES),
        col("rating", "Рейтинг", "number", aliases=("рейтинг", "rating", "оцінка", "зірки")),
        col(
            "reviews_count",
            "Кількість відгуків",
            "integer",
            aliases=("відгуки", "reviews", "review", "оцінок"),
        ),
        col("email", "Email", "email", aliases=EMAIL_ALIASES, pii="business_contact"),
        col(
            "has_online_booking",
            "Онлайн-запис",
            "boolean",
            method="enrichment",
            aliases=("онлайн запис", "booking", "записатися"),
        ),
        col("tech_stack", "Технології сайту", "array", method="enrichment"),
    ]


def _real_estate_columns() -> list[FieldSpec]:
    return [
        col("name", "ЖК / об'єкт", aliases=("назва", "жк", "name", "комплекс")),
        col("developer", "Забудовник", aliases=("забудовник", "developer", "компанія")),
        col("city", "Місто", aliases=CITY_ALIASES),
        col("address", "Адреса", aliases=ADDRESS_ALIASES),
        col("price", "Ціна", "number", aliases=PRICE_ALIASES),
        col(
            "price_per_sqm",
            "Ціна за м²",
            "number",
            aliases=("ціна за м2", "ціна за м²", "грн/м2", "price per sqm"),
        ),
        col("currency", "Валюта", aliases=("валюта", "currency", "грн", "usd")),
        col("rooms", "Кімнатність", aliases=("кімнат", "rooms", "планування")),
        col("area", "Площа, м²", "number", aliases=("площа", "area", "м2")),
        col("sales_status", "Статус продажу", aliases=("статус", "продаж", "здача", "готовність")),
        col("completion_date", "Термін здачі", "datetime", aliases=("здача", "термін", "completion")),
        col("website", "Сайт", "url", aliases=WEBSITE_ALIASES),
        col("phone", "Телефон", "phone", aliases=CONTACT_ALIASES, pii="business_contact"),
        col("description", "Опис", aliases=("опис", "description")),
    ]


def _ecommerce_columns() -> list[FieldSpec]:
    return [
        col("name", "Назва товару", aliases=("товар", "назва", "name", "title")),
        col("sku", "Артикул", aliases=("артикул", "sku", "код товару")),
        col("price", "Ціна", "number", aliases=PRICE_ALIASES),
        col("currency", "Валюта", aliases=("валюта", "currency", "грн")),
        col("availability", "Наявність", aliases=("наявність", "наявний", "в наявності", "stock")),
        col("category", "Категорія", aliases=("категорія", "category", "розділ")),
        col("brand", "Бренд", aliases=("бренд", "brand", "виробник")),
        col("rating", "Рейтинг", "number", aliases=("рейтинг", "rating", "оцінка")),
        col("reviews_count", "Відгуків", "integer", aliases=("відгуки", "reviews")),
        col("image", "Фото", "url", aliases=("фото", "image", "зображення")),
        col("website", "Сайт", "url", aliases=WEBSITE_ALIASES),
        col("description", "Опис", aliases=("опис", "description")),
    ]


def _jobs_columns() -> list[FieldSpec]:
    return [
        col("company", "Компанія", aliases=("компанія", "company", "роботодавець")),
        col("job_title", "Посада", aliases=("посада", "вакансія", "position", "job title")),
        col("salary", "Зарплата", "number", aliases=("зарплата", "salary", "оплата", "винагорода")),
        col("currency", "Валюта", aliases=("валюта", "currency", "грн", "usd")),
        col("city", "Місто", aliases=CITY_ALIASES),
        col("employment_type", "Тип зайнятості", aliases=("зайнятість", "remote", "формат")),
        col("posted_at", "Дата публікації", "datetime", aliases=("дата", "опубліковано", "posted")),
        col("description", "Опис вакансії", aliases=("опис", "description", "обов'язки")),
        col("url", "Посилання", "url", aliases=("url", "link", "посилання")),
        col("website", "Сайт", "url", aliases=WEBSITE_ALIASES),
    ]


def _tenders_columns() -> list[FieldSpec]:
    return [
        col("name", "Замовник", aliases=("замовник", "name", "організація")),
        col("title", "Предмет закупівлі", aliases=("предмет", "назва", "title")),
        col("tender_id", "ID тендера", aliases=("номер", "id", "tender")),
        col("amount", "Сума", "number", aliases=("сума", "amount", "вартість", "очікувана")),
        col("currency", "Валюта", aliases=("валюта", "currency", "грн")),
        col("status", "Статус", aliases=("статус", "status", "етап")),
        col("deadline", "Дедлайн", "datetime", aliases=("дедлайн", "кінцевий строк", "deadline")),
        col("city", "Місто", aliases=CITY_ALIASES),
        col("url", "Посилання", "url", aliases=("url", "link")),
        col("description", "Опис", aliases=("опис", "description")),
    ]


class Preset:
    def __init__(self, entity: str, title: str, keywords: tuple[str, ...], columns_factory):
        self.entity = entity
        self.title = title
        self.keywords = keywords
        self._factory = columns_factory

    def columns(self) -> list[FieldSpec]:
        return self._factory()


PRESETS: list[Preset] = [
    Preset(
        "tender",
        "Тендери та закупівлі",
        ("тендер", "prozorro", "закупівл", "prozzoro", "публічні закупівлі"),
        _tenders_columns,
    ),
    Preset(
        "job",
        "Вакансії",
        ("вакансі", "робота", "працевлашт", "посад", "job", "vacancy", "рекрут"),
        _jobs_columns,
    ),
    Preset(
        "property",
        "Нерухомість",
        ("нерухом", "квартир", "забудовник", "жк", "житло", "ріелт", "м²", "м2", "котедж"),
        _real_estate_columns,
    ),
    Preset(
        "local_business",
        "Локальний бізнес",
        (
            "клінік", "стоматолог", "ресторан", "кафе", "салон", "перукар", "спа", "фітнес",
            "автосервіс", "аптек", "магазин", "google maps", "google карт", "локальн",
        ),
        _local_business_columns,
    ),
    Preset(
        "product",
        "Товари / e-commerce",
        ("товар", "магазин", "ціна", "каталог", "ecommerce", "маркетплейс", "склад", "продаж"),
        _ecommerce_columns,
    ),
    Preset(
        "company",
        "Компанії (B2B)",
        ("компані", "тов", "бізнес", "b2b", "фірм", "підприємств", "лід", "постачальник"),
        _company_columns,
    ),
]


def detect_preset(prompt: str) -> Preset:
    lowered = (prompt or "").casefold()
    best: Preset | None = None
    best_hits = 0
    for preset in PRESETS:
        hits = sum(1 for keyword in preset.keywords if keyword in lowered)
        if hits > best_hits:
            best, best_hits = preset, hits
    return best or PRESETS[-1]


def build_spec(
    prompt: str,
    seeds: list[str],
    *,
    max_pages: int,
    sample_size: int,
    budget_usd: float,
    max_depth: int,
) -> TaskSpec:
    preset = detect_preset(prompt)
    columns = preset.columns()
    lowered = prompt.casefold()

    # Optional AI columns requested in natural language.
    if any(token in lowered for token in ("біль", "pain", "пріоритет", "score", "скоринг")):
        columns.append(
            col(
                "pain_score",
                "Оцінка болю (0-100)",
                "integer",
                method="llm",
                prompt=(
                    "Оціни 0-100, наскільки ця компанія потребує наших послуг, спираючись лише "
                    "на надані поля. Поясни одним реченням у полі note."
                ),
                description="AI-скоринг на основі запису",
            )
        )
    if any(token in lowered for token in ("керівник", "директор", "decision", "лпр", "контактна особа")):
        columns.append(
            col(
                "decision_maker",
                "Керівник / ЛПР",
                method="llm",
                pii="personal",
                prompt=(
                    "Знайди в наданих полях ім'я та посаду керівника або особи, що приймає рішення. "
                    "Якщо даних немає — null."
                ),
            )
        )
    if any(token in lowered for token in ("відгук", "review", "скарг", "проблем")):
        columns.append(
            col(
                "customer_pain",
                "Головна скарга клієнтів",
                method="llm",
                prompt=(
                    "Сформулюй одним реченням головну скаргу клієнтів, спираючись лише на надані поля."
                ),
            )
        )

    enrichment: list[EnrichmentSpec] = []
    keys = {column.key for column in columns}
    if "website" in keys:
        enrichment.append(EnrichmentSpec(enricher="site_audit", fields=["email", "phone", "tech_stack"]))
        enrichment.append(EnrichmentSpec(enricher="domain_resolver", fields=["website"]))
    for column in columns:
        if column.method == "enrichment":
            fields = [column.key]
            for item in enrichment:
                if item.enricher == "site_audit" and column.key not in item.fields:
                    item.fields.append(column.key)
            if not any(item.enricher == "site_audit" for item in enrichment):
                enrichment.append(EnrichmentSpec(enricher="site_audit", fields=fields))

    title = _derive_title(prompt) or preset.title
    return TaskSpec(
        title=title[:60],
        prompt=prompt,
        entity=preset.entity,
        columns=columns,
        seeds=seeds,
        enrichment=enrichment,
        limits=Limits(
            max_pages=max_pages,
            max_depth=max_depth,
            budget_usd=budget_usd,
            sample_size=sample_size,
        ),
        rationale=f"Офлайн-шаблон «{preset.title}» на основі ключових слів запиту.",
        assumptions=["Схема згенерована локальним шаблоном; її можна відредагувати перед запуском."],
        warning="LLM не налаштований — використано локальний шаблон схеми.",
    )


def _derive_title(prompt: str) -> str:
    text = re.sub(r"\s+", " ", prompt or "").strip()
    text = re.sub(r"^(знайди|збери|зібери|спарси|витягни|потрібні|мені потрібні|find|collect)\s+", "", text, flags=re.I)
    if len(text) > 58:
        text = text[:55].rstrip() + "…"
    return text.strip(" .")
