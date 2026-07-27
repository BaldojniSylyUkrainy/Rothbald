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

## Notarization API key

Потрібні:

- `APPLE_API_ISSUER` — Issuer ID;
- `APPLE_API_KEY` — Key ID;
- `APPLE_API_KEY_CONTENT` — повний вміст `AuthKey_<KEY_ID>.p8`.

Workflow записує `.p12` і `.p8` лише у тимчасову папку runner-а, використовує
окремий тимчасовий Keychain і видаляє все через cleanup step навіть після збою.

## Що перевіряє CI

- Developer ID Application authority;
- hardened runtime;
- secure timestamp;
- відсутність debug entitlement `get-task-allow`;
- strict `codesign` verification;
- notarization status `Accepted`;
- stapling та Gatekeeper assessment готового DMG.
