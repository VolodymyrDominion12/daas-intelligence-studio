# Зміни в роботі

Одна папка на зміну: `specs/changes/NNN-короткий-slug/` з файлами `spec.md`, `plan.md`, `tasks.md`
(шаблони — у `specs/_templates/`).

- `NNN` — наступний вільний номер (дивіться і тут, і в `archive/`), три цифри: `001`, `002`, …
- Slug — латиницею, kebab-case, бажано з ID беклогу: `001-p1-01-gold-set-eval`.
- Одна зміна = один логічний PR. Великі фічі розбивайте на кілька змін.

Після злиття: дельту перенесено в `specs/capabilities/`, папку переміщено в `archive/`.

## Активні зміни

| # | Зміна | Беклог | Статус |
|:---|:---|:---|:---|
| — | _(активних змін немає)_ | — | — |

## Архівовані зміни

| # | Зміна | Беклог | Статус |
|:---|:---|:---|:---|
| 001 | [Конектор відкритого JSON API](archive/001-d02-open-api-source/spec.md) | `D-02` | done |
| 002 | [Планувальник запусків за розкладом](archive/002-p1-05-scheduler/spec.md) | `P1-05` | done |
| 003 | [Канали доставки дельти та алертів](archive/003-d01-delta-delivery/spec.md) | `D-01` | done |
| 004 | [Автоматичні перевірки якості та офлайн gold-set eval](archive/004-p1-01-quality-guards/spec.md) | `P1-01` | done |
| 005 | [Веб-інтерфейс для розкладу, доставки дельти та JSON API джерел](archive/005-ui-automation-and-api/spec.md) | `docs/05` Тиждень 3 | done |
| 006 | [Деплой на VPS і автоматизований щоденний бекап](archive/006-vps-deploy-and-ops/spec.md) | `docs/05` Тиждень 3 | done |
| 007 | [Карантин підозрілих записів у пайплайні та UI](archive/007-p1-08-quarantine/spec.md) | `P1-08` | done |
| 008 | [Атрибуція plan-витрат LLM та Prometheus `/api/metrics`](archive/008-p1-09-metrics-and-spend/spec.md) | `P1-09` | done |
| 009 | [Entity Resolution v2: merge/unmerge дублікатів + UI-черга](archive/009-p1-02-entity-merge/spec.md) | `P1-02` | done |
| 010 | [Proxy provider і domain circuit breaker](archive/010-p1-06-proxy-circuit-breaker/spec.md) | `P1-06` | done |
