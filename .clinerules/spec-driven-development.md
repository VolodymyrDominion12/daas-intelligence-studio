# Cline / Roo Code — Spec-Driven Development

Усі правила проєкту — у [`AGENTS.md`](../AGENTS.md). Коротко:

1. **Специфікації в `specs/` — джерело правди.** Жодної зміни поведінки без зміни спеки.
2. Перед зміною коду прочитай `specs/README.md`, `specs/constitution.md` і спеку-власника в `specs/capabilities/` (рядок «Код»).
3. Нова фіча або зміна API/моделі/БД/CLI/комплаєнсу/безпеки/PII → повний цикл `specs/changes/NNN-slug/{spec,plan,tasks}.md` → тести першими → код → синхронізація `specs/capabilities/*.md` → архівування.
4. Баг, де код не відповідає спеці → офлайн-тест, що відтворює баг → фікс → посилання `` `tests/<file>.py::<test>` `` у вимозі `REQ-…`.
5. Тести офлайн; LLM лише як guarded fallback; комплаєнс і маскування PII не послаблюються.
6. Тексти для користувача й спеки — українською; ідентифікатори й docstring-и — англійською.
7. Перед завершенням запусти: `make specs-check test lint build`.
