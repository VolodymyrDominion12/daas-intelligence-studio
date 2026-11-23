---
trigger: always_on
---

# Windsurf — Spec-Driven Development

Дотримуйся [`AGENTS.md`](../../AGENTS.md) у корені репозиторію. Це обов'язкові правила проєкту:

1. **Специфікації в `specs/` — джерело правди.** Перед будь-якою зміною коду прочитай `specs/README.md`, `specs/constitution.md` і спеку-власника в `specs/capabilities/` (рядок «Код»).
2. Нова фіча, зміна API/моделі/схеми БД/CLI, комплаєнс, безпека, PII → повний цикл `specs/changes/NNN-slug/{spec,plan,tasks}.md` за шаблонами `specs/_templates/`.
3. Тест пишеться **до** реалізації; кожна вимога `REQ-…` має посилання `` `tests/<file>.py::<test>` ``.
4. Після реалізації онови `specs/capabilities/*.md` і `docs/03_IMPLEMENTATION_PLAN.md`, перенеси зміну в `specs/changes/archive/`.
5. Тести суворо офлайн (`FixtureFetcher`, `tests/fixtures/`), LLM — лише guarded fallback, комплаєнс і маскування PII не послаблюються.
6. Перевірки: `make specs-check test lint build`.
