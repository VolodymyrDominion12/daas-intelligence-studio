---
description: SDD крок 1 — створити специфікацію зміни в specs/changes/
argument-hint: <опис фічі або зміни>
---

Створи специфікацію зміни для: **$ARGUMENTS**

1. Прочитай `specs/README.md`, `specs/constitution.md`, `specs/_templates/spec.md`.
2. Знайди в `specs/capabilities/` спеки, яких стосується зміна (за таблицею «Код» і змістом).
   Прочитай їх повністю, а також відповідний розділ `docs/03_IMPLEMENTATION_PLAN.md` (беклог).
3. Визнач наступний вільний номер NNN серед `specs/changes/*/` і `specs/changes/archive/*/`.
   Slug — kebab-case латиницею, з ID беклогу, якщо є (`004-p1-05-scheduler`).
4. Створи `specs/changes/NNN-slug/spec.md` за шаблоном:
   - **що і навіщо**, без назв функцій/таблиць/бібліотек;
   - нові вимоги з ID, що продовжують нумерацію області (`grep -rho "REQ-RUN-[0-9]*" specs`);
   - сценарії GIVEN/WHEN/THEN для успіху і для відмови;
   - явна дельта ADDED / MODIFIED (було → стане) / REMOVED;
   - нефункціональні вимоги: офлайн-тести, PII, комплаєнс, вартість LLM, сумісність сховища;
   - поза скоупом; відкриті питання з `[NEEDS CLARIFICATION]`.
5. Додай рядок у таблицю «Активні зміни» в `specs/changes/README.md`.
6. Не пиши код. Наприкінці покажи список відкритих питань і запропонуй наступний крок
   `/sdd-plan NNN`, якщо питань немає.
