# Rothbald: handoff підписаного GitHub Release

Rothbald повторює безпечний релізний принцип `yt-dlp BD`: звичайний CI окремо,
а публічний реліз запускається лише вручну через `.github/workflows/release.yml`.
Workflow:

- перевіряє і збирає звичайний Windows x64 `setup.exe` через Inno Setup;
- перевіряє, підписує Developer ID, нотаризує й stapling-перевіряє macOS Apple Silicon;
- формує `latest.json` і `SHA256SUMS.txt`;
- створює **draft** GitHub Release та ніколи не публікує його автоматично.

Версія оголошена у `VERSION`. Перед складанням `scripts/prepare_build.py` вбудовує
її разом із commit SHA та часом складання у застосунок. Footer читає ці метадані
через локальний API; номер не дублюється в HTML або JavaScript.

## 1. Repository visibility і main ruleset

Workflow працює і для private repository. Для private repository доступність і
ліміти GitHub-hosted macOS/Windows runners залежать від GitHub plan власника.

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
- Workflow permissions: Read and write;
- `can_approve_pull_request_reviews`: false.

Release workflow використовує лише офіційні `actions/checkout`,
`actions/setup-python`, `actions/upload-artifact` і `actions/download-artifact`.

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
| Variable | `APPLE_SIGNING_IDENTITY` | `Developer ID Application: Volodymyr Bortiuk (3KFYRV3QRP)` |

Apple secrets можна зберегти на repository або environment рівні. Не дублюйте
однакову назву на обох рівнях. Variable рекомендовано зберігати в environment.

Rothbald не використовує `TAURI_SIGNING_PRIVATE_KEY`: Tauri `.sig` підписує
тільки Tauri updater payload, а Rothbald є PyInstaller/PySide6 застосунком.
Механічне використання цього ключа не дало б жодної перевірки під час запуску.

## 5. Запуск релізу

1. Змініть `VERSION` у reviewed commit і дочекайтесь зеленого `test-and-build` на `main`.
2. Створіть annotated tag `v` + значення `VERSION`, наприклад `v0.1.1.0`, саме на зеленому `main`, і запуште його.
3. Переконайтесь, що tag-triggered `test-and-build` також зелений.
4. Відкрийте **Actions → Manual signed release → Run workflow**.
5. Branch: лише `main`.
6. `tag`: уже наявний тег із попереднього кроку.
7. `notes`: короткий текст змін.
8. Якщо для environment налаштовані required reviewers, approve macOS і Windows jobs.

Workflow fail-closed зупиниться, якщо запуск зроблено не з поточного `main`, тег
не існує, не вказує на цей commit або не збігається з `VERSION`, бракує credential,
Developer ID signature/hardened runtime/timestamp, Apple notarization не має
статусу `Accepted`, stapling невалідний або бракує будь-якого release asset.

Після зеленого workflow відкрийте draft у **Releases** і перевірте:

- `Rothbald_<version>_aarch64.dmg`;
- `Rothbald_<version>_aarch64.zip`;
- `Rothbald_<version>_windows-x86_64-setup.exe`;
- `latest.json`;
- `SHA256SUMS.txt`.

За можливості встановіть `.dmg` і Windows installer на чистих машинах, після чого
натисніть **Publish release**. До цього draft не змінює `/releases/latest`.

## Windows SmartScreen

Як і в `yt-dlp-BD`, Tauri updater signature не є Windows Authenticode. У
Rothbald Tauri updater відсутній, тому Windows installer має SHA-256 integrity у
release manifest, але без окремого Authenticode certificate Windows може
показувати `Unknown publisher`. Прибрати це можна лише окремим Windows
code-signing certificate.
