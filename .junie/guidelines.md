# JetBrains Junie — Spec-Driven Development

Усі правила проєкту — у [`AGENTS.md`](../AGENTS.md) в корені репозиторію. Прочитай його повністю перед будь-якими змінами коду.

## Обов'язковий процес (SDD)

1. **Специфікація — джерело правди.** Жодної зміни поведінки без зміни спеки в `specs/`.
2. Перед редагуванням файлу прочитай `specs/README.md`, `specs/constitution.md` і спеку-власника в `specs/capabilities/` (рядок «Код»).
3. **Повний цикл** для нової фічі, зміни API/моделі/схеми БД/CLI, комплаєнсу, безпеки чи PII:
   `specs/changes/NNN-slug/spec.md` → `plan.md` → `tasks.md` (за шаблонами `specs/_templates/`) → код → синхронізація `specs/capabilities/*.md` → архівування в `specs/changes/archive/`.
4. **Баг, де код суперечить спеці:** спершу офлайн-тест, що відтворює баг → мінімальний фікс → посилання на тест `` `tests/<file>.py::<test>` `` у відповідній `REQ-…`.
5. **Непорушні інваріанти:** тести офлайн (`FixtureFetcher`, `tests/fixtures/`), LLM лише як guarded fallback, комплаєнс (`robots.txt`, реєстр джерел, logged-out, SSRF) і маскування PII не послаблюються.
6. **Мова:** ідентифікатори й docstring-и — англійською; тексти для користувача, події, спеки й документація — українською.
7. **Перевірки перед завершенням:** `make specs-check`, `make test`, `make lint`, `make build`.
