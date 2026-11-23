---
apply: always
---

# JetBrains AI Assistant — Spec-Driven Development

Дотримуйся [`AGENTS.md`](../../AGENTS.md) у корені репозиторію як єдиного джерела правил.

- **Специфікації в `specs/` — джерело правди.** Жодної зміни поведінки без оновлення спеки.
- Перед зміною файлу прочитай `specs/constitution.md` і відповідну спеку в `specs/capabilities/` (рядок «Код»).
- Для нової фічі або зміни API/моделі/БД/CLI/комплаєнсу/безпеки/PII виконуй повний цикл: `specs/changes/NNN-slug/{spec,plan,tasks}.md` → тести першими → реалізація → синхронізація `specs/capabilities/*.md` → архівування.
- Кожна вимога `REQ-…` повинна мати посилання на офлайн-тест `` `tests/<file>.py::<test>` ``.
- Комплаєнс (`robots.txt`, реєстр джерел, logged-out, SSRF) і маскування `pii="personal"` не послаблюються; LLM — лише guarded fallback.
- Перевірки перед завершенням: `make specs-check test lint build`.
