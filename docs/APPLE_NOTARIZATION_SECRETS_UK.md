# Apple Developer credentials для Rothbald

Використовуються ті самі Developer ID Application certificate та App Store
Connect API credentials, що вже перевірені у `yt-dlp-BD`. Не копіюйте secret у
commit, Artifact, Issue або чат.

## Certificate

Потрібний `.p12` із certificate та private key для identity:

`Developer ID Application: Volodymyr Bortiuk (3KFYRV3QRP)`

Перед додаванням у GitHub перевірте оригінал і скопіюйте base64 у clipboard:

```bash
P12_FILE="/absolute/path/to/certificate.p12"
openssl pkcs12 -legacy -in "$P12_FILE" -noout
base64 -i "$P12_FILE" | pbcopy
```

Clipboard вставляється в `APPLE_CERTIFICATE`, пароль — у
`APPLE_CERTIFICATE_PASSWORD`. Workflow декодує файл системним macOS `base64` і
імпортує через `security import`, що сумісно зі старими Keychain PKCS#12.

Для прямого запису в уже створений GitHub environment `release`, без виведення
Base64 у Terminal:

```bash
P12_FILE="/absolute/path/to/certificate.p12"
/usr/bin/base64 -i "$P12_FILE" \
  | gh secret set APPLE_CERTIFICATE --env release \
      --repo BaldojniSylyUkrainy/Rothbald

read -s "ROTHBALD_P12_PASSWORD?Пароль .p12: "
printf '%s' "$ROTHBALD_P12_PASSWORD" \
  | gh secret set APPLE_CERTIFICATE_PASSWORD --env release \
      --repo BaldojniSylyUkrainy/Rothbald
unset ROTHBALD_P12_PASSWORD
```

## Notarization API key

Потрібні:

- `APPLE_API_ISSUER` — Issuer ID;
- `APPLE_API_KEY` — Key ID;
- `APPLE_API_KEY_CONTENT` — повний вміст `AuthKey_<KEY_ID>.p8`.

Безпечний CLI-варіант:

```bash
P8_FILE="/absolute/path/to/AuthKey_KEYID.p8"

read "ROTHBALD_APPLE_ISSUER?Issuer ID: "
printf '%s' "$ROTHBALD_APPLE_ISSUER" \
  | gh secret set APPLE_API_ISSUER --env release \
      --repo BaldojniSylyUkrainy/Rothbald
unset ROTHBALD_APPLE_ISSUER

read "ROTHBALD_APPLE_KEY_ID?Key ID: "
printf '%s' "$ROTHBALD_APPLE_KEY_ID" \
  | gh secret set APPLE_API_KEY --env release \
      --repo BaldojniSylyUkrainy/Rothbald
unset ROTHBALD_APPLE_KEY_ID

gh secret set APPLE_API_KEY_CONTENT --env release \
  --repo BaldojniSylyUkrainy/Rothbald < "$P8_FILE"
```

GitHub не дозволяє прочитати secret назад. Перевірити можна лише наявність назв:

```bash
gh secret list --env release --repo BaldojniSylyUkrainy/Rothbald
```

Workflow записує `.p12` і `.p8` лише у тимчасову папку runner-а, використовує
окремий тимчасовий Keychain, зберігає й відновлює початковий Keychain search
list і видаляє все через cleanup step навіть після збою.

## Що перевіряє CI

- Developer ID Application authority;
- hardened runtime;
- secure timestamp;
- відсутність debug entitlement `get-task-allow`;
- strict `codesign` verification;
- notarization status `Accepted`;
- stapling та Gatekeeper assessment готового DMG.

## Binary updater

Rothbald `0.2.0.0` має власний Ed25519 updater для PyInstaller-збірок. Він не
використовує `TAURI_SIGNING_PRIVATE_KEY`. Public key уже вбудований у застосунок,
а відповідний private key створено локально в ignored-файлі:

`./.secrets/rothbald-updater-private.key`

Збережіть цей файл у зашифрованому password manager/backup і додайте його як
environment secret без виведення значення:

```bash
gh secret set ROTHBALD_UPDATER_PRIVATE_KEY --env release \
  --repo BaldojniSylyUkrainy/Rothbald \
  < .secrets/rothbald-updater-private.key
```

Workflow підписує `latest.json`, а застосунок перевіряє підпис, точний asset URL,
ім’я, розмір і SHA-256 до відкриття installer-а. Private key ніколи не
потрапляє у застосунок, repository, release asset або Actions artifact.

Не видаляйте єдину резервну копію й не запускайте генератор повторно для заміни
ключа. Втрата private key після першого updater-enabled релізу позбавить уже
встановлені копії можливості перевіряти майбутні оновлення.
