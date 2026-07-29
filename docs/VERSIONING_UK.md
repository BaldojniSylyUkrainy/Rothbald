# Версіонування Rothbald

`VERSION` — єдине джерело версії застосунку. Під час складання вона вбудовується
в macOS bundle, Windows executable та `/api/app`; footer читає її з API й не має
захардкодженого номера.

Формат: `MAJOR.MINOR.PATCH.HOTFIX`.

- термінове точкове виправлення: `0.3.1.0` → `0.3.1.1`;
- виправлення: `0.3.1.0` → `0.3.2.0`;
- нова функція: `0.3.1.0` → `0.4.0.0`.

Перед релізним commit виконайте одну з команд:

```bash
python scripts/versioning.py hotfix
python scripts/versioning.py fix
python scripts/versioning.py feature
```

Скрипт одночасно змінює `VERSION`, перший рядок `RELEASE_NOTES.md` і стандартне
значення поля tag у `Manual signed release`. Notes навмисно отримують заборонену
TODO-заглушку: її треба замінити реальним описом, інакше CI не пропустить реліз.

Перевірка синхронності без змін:

```bash
python scripts/versioning.py check
python scripts/validate_release_notes.py
```

Після зеленого `main` створіть tag `v` + точне значення `VERSION`. Поле
`Existing green-main release tag` уже міститиме цю версію за замовчуванням.
