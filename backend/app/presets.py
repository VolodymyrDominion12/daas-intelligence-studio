"""Offline (LLM-free) task templates so the studio works before any model is configured."""

from __future__ import annotations

import re

from .models import (
    ApiSourceSpec,
    AutoEnrichTrigger,
    EnrichmentSpec,
    FieldSpec,
    Limits,
    TaskSpec,
)
from .utils import parse_number


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


def _openprocurement_columns() -> list[FieldSpec]:
    return [
        col("tender_id", "ID закупівлі / лота", method="api_json"),
        col("title", "Предмет / назва", method="api_json"),
        col("name", "Замовник / організатор", method="api_json"),
        col("edrpou", "ЄДРПОУ", method="api_json"),
        col("cpv", "Код CPV", method="api_json"),
        col("region", "Регіон", method="api_json"),
        col("amount", "Очікувана вартість / старт", "number", method="api_json"),
        col("currency", "Валюта", method="api_json"),
        col("status", "Статус", method="api_json"),
        col("modified_at", "Оновлено", "datetime", method="api_json"),
        col("deadline", "Дедлайн", "datetime", method="api_json"),
        col("url", "Посилання", "url", method="api_json"),
    ]


def _is_openprocurement_api(prompt: str, seeds: list[str]) -> bool:
    lowered = (prompt or "").casefold()
    joined_seeds = " ".join(seeds or []).casefold()
    if any(host in joined_seeds for host in ("openprocurement.org", "openprocurement.local")):
        return True
    if any(token in lowered for token in ("openprocurement", "prozorro api", "prozorro.продажі api")):
        return True
    has_prozorro = any(token in lowered for token in ("prozorro", "prozzoro", "прозорро"))
    has_api = any(token in lowered for token in ("api", "апі", "json"))
    return has_prozorro and has_api


def _is_openprocurement_winners_api(prompt: str, seeds: list[str]) -> bool:
    if not _is_openprocurement_api(prompt, seeds):
        return False
    lowered = (prompt or "").casefold()
    return any(
        token in lowered
        for token in (
            "перемож",
            "winner",
            "award",
            "авард",
            "постачальник",
            "субпідряд",
            "b2b",
            "аудит їх сайт",
            "аудит сайт",
            "site_audit",
        )
    )


def _parse_amount_threshold(prompt: str, *, default: float = 1_000_000.0) -> float:
    lowered = (prompt or "").casefold()
    if any(token in lowered for token in ("без порог", "усіх сум", "всіх сум")):
        return 0.0
    match = re.search(
        r"(?:від|понад|більше|>=?)\s*([\d\s.,]+)\s*(млн|м\b|тис|к\b|k\b)?",
        lowered,
    )
    if not match:
        return default
    raw_num, unit = match.groups()
    num = parse_number(raw_num)
    if num is None or num < 0:
        return default
    if unit and unit.startswith(("млн", "м")):
        return float(num * 1_000_000)
    if unit and unit.startswith(("тис", "к", "k")):
        return float(num * 1_000)
    return float(num)


def _openprocurement_winners_columns() -> list[FieldSpec]:
    return [
        col("tender_id", "ID закупівлі / лота", method="api_json"),
        col("title", "Предмет закупівлі", method="api_json"),
        col("winner_name", "Переможець / постачальник", method="api_json"),
        col("winner_edrpou", "ЄДРПОУ переможця", method="api_json"),
        col("award_amount", "Сума присудження", "number", method="api_json"),
        col("currency", "Валюта", method="api_json"),
        col("award_status", "Статус присудження", method="api_json"),
        col("award_date", "Дата рішення", "datetime", method="api_json"),
        col("name", "Замовник", method="api_json"),
        col("cpv", "Код CPV", method="api_json"),
        col("region", "Регіон", method="api_json"),
        col("status", "Статус тендера", method="api_json"),
        col("modified_at", "Оновлено", "datetime", method="api_json"),
        col("url", "Посилання", "url", method="api_json"),
        col("website", "Сайт переможця", "url", method="enrichment"),
        col("email", "Email переможця", "email", method="enrichment", pii="business_contact"),
        col("phone", "Телефон переможця", "phone", method="enrichment", pii="business_contact"),
        col("socials", "Соцмережі", "json", method="enrichment", pii="business_contact"),
        col("tech_stack", "Технології сайту", "array", method="enrichment"),
    ]


def _default_openprocurement_source(seeds: list[str]) -> ApiSourceSpec:
    seed = (seeds[0] if seeds else "").strip().rstrip("/")
    base = seed.split("?", 1)[0] if seed else "https://public.api.openprocurement.org/api/2.5/tenders"
    if not base.startswith(("http://", "https://")):
        base = "https://" + base.lstrip("/")
    is_sale = "prozorro.sale" in base or "auctions" in base
    record_tpl = (
        "https://prozorro.sale/auction/{id}" if is_sale else "https://prozorro.gov.ua/tender/{id}"
    )
    return ApiSourceSpec(
        items_path="data",
        id_path="id",
        next_page_path="next_page.uri",
        cursor_path="next_page.offset",
        cursor_param="offset",
        detail_url_template=f"{base}/{{id}}",
        record_url_template=record_tpl,
        incremental=True,
        field_map={
            "tender_id": "tenderID|auctionID|id",
            "title": "title",
            "name": "procuringEntity.name",
            "edrpou": "procuringEntity.identifier.id",
            "cpv": "items.0.classification.id",
            "region": "procuringEntity.address.region|items.0.deliveryAddress.region",
            "amount": "value.amount|minimalStep.amount",
            "currency": "value.currency",
            "status": "status",
            "modified_at": "dateModified",
            "deadline": "tenderPeriod.endDate|auctionPeriod.startDate",
        },
    )


def _openprocurement_winners_source(seeds: list[str]) -> ApiSourceSpec:
    base_spec = _default_openprocurement_source(seeds)
    return base_spec.model_copy(
        update={
            "field_map": {
                "tender_id": "tenderID|auctionID|id",
                "title": "title",
                "winner_name": (
                    "awards[status=active].suppliers.0.name"
                    "|awards[status=pending].suppliers.0.name"
                    "|awards.0.suppliers.0.name"
                ),
                "winner_edrpou": (
                    "awards[status=active].suppliers.0.identifier.id"
                    "|awards[status=pending].suppliers.0.identifier.id"
                    "|awards.0.suppliers.0.identifier.id"
                ),
                "award_amount": (
                    "awards[status=active].value.amount"
                    "|awards[status=pending].value.amount"
                    "|awards.0.value.amount"
                    "|value.amount"
                ),
                "currency": (
                    "awards[status=active].value.currency"
                    "|awards.0.value.currency"
                    "|value.currency"
                ),
                "award_status": (
                    "awards[status=active].status"
                    "|awards[status=pending].status"
                    "|awards.0.status"
                ),
                "award_date": (
                    "awards[status=active].date"
                    "|awards[status=pending].date"
                    "|awards.0.date"
                ),
                "name": "procuringEntity.name",
                "cpv": "items.0.classification.id",
                "region": (
                    "awards[status=active].suppliers.0.address.region"
                    "|awards.0.suppliers.0.address.region"
                    "|procuringEntity.address.region"
                    "|items.0.deliveryAddress.region"
                ),
                "status": "status",
                "modified_at": "dateModified",
                "website": (
                    "awards[status=active].suppliers.0.contactPoint.url"
                    "|awards.0.suppliers.0.contactPoint.url"
                ),
            }
        }
    )


def build_spec(
    prompt: str,
    seeds: list[str],
    *,
    max_pages: int,
    sample_size: int,
    budget_usd: float,
    max_depth: int,
    api_source: ApiSourceSpec | None = None,
) -> TaskSpec:
    if api_source is None and _is_openprocurement_winners_api(prompt, seeds):
        api_source = _openprocurement_winners_source(seeds)
        columns = _openprocurement_winners_columns()
        title = _derive_title(prompt) or "Prozorro: переможці (B2B)"
        threshold = _parse_amount_threshold(prompt, default=1_000_000.0)
        return TaskSpec(
            title=title[:60],
            prompt=prompt,
            entity="tender",
            columns=columns,
            seeds=seeds,
            extraction="api_json",
            api_source=api_source,
            enrichment=[
                EnrichmentSpec(
                    enricher="domain_resolver",
                    fields=["website"],
                    config={"name_field": "winner_name", "city_fields": ["region", "city"]},
                ),
                EnrichmentSpec(
                    enricher="site_audit",
                    fields=["email", "phone", "socials", "tech_stack"],
                ),
            ],
            auto_enrich=AutoEnrichTrigger(
                enabled=True,
                min_amount=threshold,
                amount_fields=["award_amount", "amount"],
                require_fields=["winner_name"],
                status_field="award_status",
                allowed_statuses=["active"],
                max_records_per_run=20,
            ),
            limits=Limits(
                max_pages=max_pages,
                max_depth=0,
                budget_usd=budget_usd,
                sample_size=sample_size,
            ),
            rationale=(
                "Офлайн B2B-профіль Prozorro Переможці: витягування постачальників-переможців "
                "та авто-аудит їхніх сайтів і контактів при перевищенні порогу суми."
            ),
            assumptions=[
                "Використано декларативний JSON-мапінг OpenProcurement (awards) без LLM.",
                "Контакти переможця мають клас business_contact; персональних PII немає.",
            ],
        )

    if api_source is None and _is_openprocurement_api(prompt, seeds):
        api_source = _default_openprocurement_source(seeds)
        columns = _openprocurement_columns()
        title = _derive_title(prompt) or "Prozorro / OpenProcurement API"
        return TaskSpec(
            title=title[:60],
            prompt=prompt,
            entity="tender",
            columns=columns,
            seeds=seeds,
            extraction="api_json",
            api_source=api_source,
            limits=Limits(
                max_pages=max_pages,
                max_depth=0,
                budget_usd=budget_usd,
                sample_size=sample_size,
            ),
            rationale="Офлайн-профіль OpenProcurement (Prozorro / Prozorro.Продажі) для відкритого JSON API.",
            assumptions=["Використано декларативний JSON-мапінг без LLM і без персональних PII."],
        )

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
    extraction_mode = "api_json" if api_source is not None else "auto"
    if api_source is not None:
        for column in columns:
            if column.key in api_source.field_map:
                column.method = "api_json"
    return TaskSpec(
        title=title[:60],
        prompt=prompt,
        entity=preset.entity,
        columns=columns,
        seeds=seeds,
        extraction=extraction_mode,
        api_source=api_source,
        enrichment=enrichment,
        limits=Limits(
            max_pages=max_pages,
            max_depth=max_depth,
            budget_usd=budget_usd,
            sample_size=sample_size,
        ),
        rationale=f"Офлайн-шаблон «{preset.title}» на основі ключових слів запиту.",
        assumptions=["Схема згенерована локальним шаблоном; її можна відредагувати перед запуском."],
        warning=None if api_source is not None else "LLM не налаштований — використано локальний шаблон схеми.",
    )


def _derive_title(prompt: str) -> str:
    text = re.sub(r"\s+", " ", prompt or "").strip()
    text = re.sub(r"^(знайди|збери|зібери|спарси|витягни|потрібні|мені потрібні|find|collect)\s+", "", text, flags=re.I)
    if len(text) > 58:
        text = text[:55].rstrip() + "…"
    return text.strip(" .")
