# Rothbald: handoff підписаного GitHub Release

Rothbald повторює безпечний релізний принцип `yt-dlp BD`: звичайний CI окремо,
а публічний реліз запускається лише вручну через `.github/workflows/release.yml`.
Workflow:

- перевіряє, збирає та Authenticode-підписує Windows x64 застосунок і `setup.exe`;
- перевіряє, підписує Developer ID, нотаризує й stapling-перевіряє macOS Apple Silicon;
- формує підписаний `latest.json` і `SHA256SUMS.txt`;
- бере один текст із обов’язкового `RELEASE_NOTES.md` для manifest та GitHub Release;
- створює **draft** GitHub Release та ніколи не публікує його автоматично.

Версія оголошена у `VERSION`. Перед складанням `scripts/prepare_build.py` вбудовує
її разом із commit SHA та часом складання у застосунок. Footer читає ці метадані
через локальний API; номер не дублюється в HTML або JavaScript.
Правила підвищення версії та команда синхронізації описані у
`docs/VERSIONING_UK.md`.

## 1. Repository visibility і main ruleset

Перед зміною visibility перевірте всю історію Git на credentials, signing files,
локальні бази, transcripts і media. Після переходу в public код, повна історія
commit-ів, наявні Actions logs та artifacts стають публічними. GitHub також
вимикає push rulesets під час зміни visibility, тому ruleset нижче потрібно
перевірити або створити заново вже після переходу.

У repository зараз немає `LICENSE`. Public visibility робить код доступним для
читання і fork, але саме по собі не надає open-source ліцензію. Додавати ліцензію
потрібно лише після окремого рішення власника щодо MIT/Apache-2.0/GPL або іншої
моделі.

Для public repository стандартні GitHub-hosted runners безкоштовні. Rothbald
використовує лише стандартні `macos-15`, `windows-latest` та `ubuntu-latest`,
не larger runners. Artifact storage обліковується окремо: PR перевіряє повну
збірку без upload, а main/manual CI artifacts зберігаються лише один день.

Створіть active branch ruleset `Protect main` для default branch:

- Restrict deletions;
- Block force pushes;
- Require a pull request before merging;
- 1 approval;
- Dismiss stale approvals when new commits are pushed;
- Require conversation resolution before merging;
- без bypass actors.

## 2. Actions permissions

У **Settings → Actions → General**:

- Allowed actions: тільки GitHub-owned actions;
- Workflow permissions: Read repository contents;
- `can_approve_pull_request_reviews`: false.

Release workflow використовує лише офіційні `actions/checkout`,
`actions/setup-python`, `actions/upload-artifact` і `actions/download-artifact`.
Глобально workflow має `contents: read`; тільки job `publish-draft` явно отримує
`contents: write`.

## 3. Protected environment `release`

Створіть environment рівно `release`. Якщо GitHub plan репозиторію підтримує
environment deployment policies, обмежте його:

- deployment branch policy: тільки `main`.

Якщо repository має ще одного trusted owner, додайте його required reviewer і
увімкніть `prevent_self_review`. Не призначайте єдиного release operator-а
`2FED` required reviewer-ом із self-review prevention: він не зможе approve
власний запуск. Навіть без environment protection workflow має окремий
`workflow_dispatch` і fail-closed tag/main/SHA preflight.

## 4. Secrets і variable

Скопіюйте ті самі Apple credentials, що використовує `yt-dlp-BD`, у Rothbald.
GitHub не дозволяє прочитати назад значення secret, тому їх потрібно додати з
оригінального захищеного джерела:

| Type | Назва | Значення |
|---|---|---|
| Secret | `APPLE_CERTIFICATE` | `.p12` Developer ID Application у base64 |
| Secret | `APPLE_CERTIFICATE_PASSWORD` | пароль `.p12` |
| Secret | `APPLE_API_ISSUER` | App Store Connect Issuer ID |
| Secret | `APPLE_API_KEY` | App Store Connect Key ID |
| Secret | `APPLE_API_KEY_CONTENT` | повний `AuthKey_….p8` |
| Secret | `ROTHBALD_UPDATER_PRIVATE_KEY` | base64 Ed25519 private key із `.secrets/rothbald-updater-private.key` |
| Secret | `WINDOWS_CERTIFICATE` | Windows code-signing `.pfx` у base64 |
| Secret | `WINDOWS_CERTIFICATE_PASSWORD` | пароль Windows `.pfx` |
| Variable | `APPLE_SIGNING_IDENTITY` | `Developer ID Application: Volodymyr Bortiuk (3KFYRV3QRP)` |

Apple secrets можна зберегти на repository або environment рівні. Не дублюйте
однакову назву на обох рівнях. Variable рекомендовано зберігати в environment.

Rothbald не використовує `TAURI_SIGNING_PRIVATE_KEY`: це PyInstaller/PySide6
застосунок із власним Ed25519 manifest. Public key вбудований у
`update_manifest.py`; відповідний private key існує лише в ignored локальному
файлі, encrypted backup і secret середовища `release`.

Додайте вже згенерований ключ без виведення його в Terminal:

```bash
gh secret set ROTHBALD_UPDATER_PRIVATE_KEY --env release \
  --repo BaldojniSylyUkrainy/Rothbald \
  < .secrets/rothbald-updater-private.key
```

Не генеруйте новий ключ, якщо цей файл втрачено без підтвердженої резервної
копії: новий private key не відповідатиме public key у вже встановлених збірках.

## 5. Запуск релізу

1. Запустіть `python scripts/versioning.py fix` для виправлення або `python scripts/versioning.py feature` для нової функції. Скрипт синхронізує `VERSION`, заголовок notes і стандартне значення tag у workflow. Замініть TODO у `RELEASE_NOTES.md` реальним описом до commit.
2. Дочекайтесь зеленого `test-and-build` на `main`: звичайний CI також перевіряє notes.
3. Створіть annotated tag `v` + значення `VERSION`, наприклад `v0.2.0.0`, саме на зеленому `main`, і запуште його.
4. Tag push навмисно не запускає повторний `test-and-build`.
5. Відкрийте **Actions → Manual signed release → Run workflow**.
6. Branch: лише `main`.
7. `tag`: уже наявний тег із попереднього кроку. Поле автоматично має актуальне значення з `VERSION`; окремого поля notes немає.
8. Якщо для environment налаштовані required reviewers, approve jobs.

Workflow fail-closed зупиниться, якщо запуск зроблено не з поточного `main`, тег
не існує, не вказує на цей commit або не збігається з `VERSION`, notes порожні,
неактуальні, надто короткі чи містять заглушку, бракує credential,
Developer ID signature/hardened runtime/timestamp, Apple notarization не має
статусу `Accepted`, stapling невалідний або бракує будь-якого release asset.

Після зеленого workflow відкрийте draft у **Releases** і перевірте:

- `Rothbald-<version>-Mac-Apple-Silicon.dmg`;
- `Rothbald-<version>-Mac-Apple-Silicon.zip`;
- `Rothbald-<version>-Windows-Setup.exe`;
- `latest.json`;
- `SHA256SUMS.txt`.

За можливості встановіть `.dmg` і Windows installer на чистих машинах, після чого
натисніть **Publish release**. До цього draft не змінює `/releases/latest`.

## Windows Authenticode і SmartScreen

Ed25519 signature updater manifest не є Windows Authenticode, тому release
додатково вимагає окремий Windows code-signing certificate. Workflow декодує
`WINDOWS_CERTIFICATE` лише у тимчасовий файл runner-а, підписує та перевіряє
`Rothbald.exe`, пакує його в Inno Setup, після чого підписує й перевіряє сам
installer. Тимчасовий `.pfx` видаляється навіть після невдалого job.

Додайте PFX без виведення його в Terminal, наприклад на macOS:

```bash
base64 -i /absolute/path/to/windows-code-signing.pfx |
  gh secret set WINDOWS_CERTIFICATE --env release \
    --repo BaldojniSylyUkrainy/Rothbald
gh secret set WINDOWS_CERTIFICATE_PASSWORD --env release \
  --repo BaldojniSylyUkrainy/Rothbald
```

SmartScreen reputation накопичується окремо від валідності підпису. Новий
сертифікат може спочатку все одно показувати попередження, але publisher уже не
буде `Unknown` за умови довіреного code-signing certificate.
