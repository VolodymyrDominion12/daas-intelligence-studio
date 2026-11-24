# 014 — Задачі: Профіль «Prozorro Переможці» та B2B-тригери авто-збагачення

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-PLAN-015` (профіль Prozorro Переможці vs базовий OpenProcurement) → `backend/tests/test_prozorro_winners.py::test_prozorro_winners_preset_and_trigger_config`
- [x] T011 [P] Тест `REQ-EXT-011` (`[field=value]` фільтр масиву у `_walk_path`) → `backend/tests/test_prozorro_winners.py::test_json_path_filtered_award_selection`
- [x] T012 [P] Тест `REQ-ENR-012` (щасливий шлях: збір → авто-тригер `min_amount` → `domain_resolver` → `site_audit` + `tech_detect` за 1 прохід → подія `auto_enrichment_triggered` → снапшот) → `backend/tests/test_prozorro_winners.py::test_collect_winner_trigger_resolves_domain_and_audits_site`
- [x] T013 [P] Тест `REQ-ENR-012` (відмови: сума < порогу, відсутній переможець, повторна ідемпотентність, заборонений домен не зриває збір) → `backend/tests/test_prozorro_winners.py::test_winner_trigger_respects_threshold_idempotency_and_compliance`
- [x] T014 [P] Тест `REQ-DEL-007` (доставка переможця за `award_amount`, бізнес-контакти без маскування і захист `personal` PII) → `backend/tests/test_prozorro_winners.py::test_winner_delivery_formats_contacts_and_protects_personal_pii`

## Фаза 2 — Реалізація
- [x] T020 Моделі (`AutoEnrichTrigger`, `TaskSpec.auto_enrich`) → `backend/app/models.py`
- [x] T021 Фільтр масиву `[key=val]` у JSON-екстракторі → `backend/app/extraction/api_json.py`
- [x] T022 Профіль «Prozorro Переможці (B2B)» і прив'язка збагачувачів → `backend/app/presets.py`, `backend/app/runtime.py`
- [x] T023 Підтримка `name_field` / `winner_name` / `region` у `DomainResolverEnricher` → `backend/app/enrichment/domain_resolver.py`
- [x] T024 Послідовний waterfall + `_run_auto_enrichment` + подія `auto_enrichment_triggered` → `backend/app/pipeline/runner.py`
- [x] T025 Підтримка `award_amount` і B2B-полів переможця у доставці → `backend/app/pipeline/delivery.py`
- [x] T026 UI-типи, приклад у `NewTask` і керування тригером у `PlanPanel` → `frontend/src/types.ts`, `frontend/src/components/NewTask.tsx`, `frontend/src/components/PlanPanel.tsx`

## Фаза 3 — Перевірка
- [x] T030 `make specs-check test lint build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00,01,03,07,08,13}*.md` (з посиланнями на тести)
- [x] T041 Оновити `docs/06_DEVELOPMENT_AND_MONETIZATION_PLAN.md` (§5 SDD `014`)
- [x] T042 Перенести `specs/changes/014-prozorro-winners-b2b-triggers/` → `specs/changes/archive/`
