# GEMINI.md

Усі правила проєкту — в [`AGENTS.md`](AGENTS.md). Прочитай його повністю перед роботою.

Головне: **Spec-Driven Development**. Специфікації в `specs/` — джерело правди; жодної зміни
поведінки без зміни спеки; тест до реалізації; після злиття `specs/capabilities/` знову описує
фактичну поведінку. Перевірки: `make specs-check test lint build`.
