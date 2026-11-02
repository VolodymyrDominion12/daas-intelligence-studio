# Copilot instructions

Повні правила — у [`AGENTS.md`](../AGENTS.md). Коротко:

- Проєкт працює за **Spec-Driven Development**: специфікації в `specs/` — джерело правди. Жодної
  зміни поведінки без зміни спеки (`specs/changes/NNN-slug/spec.md → plan.md → tasks.md`, потім
  синхронізація `specs/capabilities/*.md`).
- Перед зміною файлу знайди спеку-власника в `specs/capabilities/` (рядок «Код») і дотримуйся її
  вимог `REQ-…`.
- Тест пишеться до реалізації; кожна вимога має посилання `` `tests/<file>.py::<test>` ``.
- Тести офлайн (`FixtureFetcher`, `tests/fixtures/`), LLM лише як guarded fallback, комплаєнс
  (robots, реєстр джерел, logged-out, SSRF) і маскування PII не послаблюються.
- Тексти для користувача — українською; ідентифікатори — англійською.
- Перевірки: `make specs-check test lint build`.
