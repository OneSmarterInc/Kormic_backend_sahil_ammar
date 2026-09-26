# Production student invitation links

Canonical invitation URL: `https://app.kormic.ai/claim?token=...`.
`CLAIM_PAGE_URL` is a backend/Celery setting containing the **student frontend /
deep-link URL**, not the Django API URL (such as `https://backend.kormic.ai/api`).
It belongs in the backend environment, not in portal frontend environment files.
Invitation sending intentionally fails when it is unset. Set it to the public
claim page without a query string or fragment; the backend appends the token.

Django serves both association documents and a browser redirect. The unified
frontend must be deployed to serve the student claim form. Set
`STUDENT_WEB_CLAIM_URL=https://<your-student-web-host>/claim` when the app-link
host routes `/claim` to Django. This URL must point to the frontend, not back to
the same Django route. The Student Expo repository owns native
link registration and the in-app claim flow. An email scanner opening the fallback
does not send OTPs or claim an account.

## Deploy the domain before sending invitations

1. Point `app.kormic.ai` DNS at the backend reverse proxy. Provision its HTTPS
   certificate (for example with Certbot's Nginx plugin). Install
   `deploy/app-links.nginx.conf` once the certificate exists; run `nginx -t` and
   reload Nginx. Add `app.kormic.ai` to `DJANGO_ALLOWED_HOSTS`, preserving existing
   API hosts. Keep the association document routes on Django; redirect only the
   claim page to the web frontend using `STUDENT_WEB_CLAIM_URL`. Configure
   any CDN/WAF to allow unauthenticated association requests without challenges.
2. Set `APP_LINK_ANDROID_SHA256_FINGERPRINTS` to the colon-separated SHA-256
   **app signing key certificate** from Google Play Console > App integrity /
   App signing. This is normally different from the upload key. Multiple valid
   signing certificates may be comma-separated for rotation or direct APK
   distribution. Never publish a debug certificate as a production association.
3. Set `APP_LINK_APPLE_APP_ID_PREFIX` to the prefix of the signed iOS build's
   `application-identifier` entitlement, usually your 10-character Apple Team ID.
   Its suffix must be `com.kormic.student`. Enable Associated Domains on that
   Apple App ID and refresh the signing profile. These values identify your
   released application; they are unrelated to user passwords or TOTP codes.
4. Set `APP_LINK_ANDROID_STORE_URL` and `APP_LINK_IOS_STORE_URL` to the real HTTPS
   store listings. Leave them empty for an unpublished pilot: the page then tells
   students to ask their institute for installation instructions. No fabricated
   store IDs are built into the page.
5. Restart the backend. Verify both documents from outside your network:

   ```sh
   curl -i https://app.kormic.ai/.well-known/assetlinks.json
   curl -i https://app.kormic.ai/.well-known/apple-app-site-association
   curl -i 'https://app.kormic.ai/claim?token=deployment-smoke-test'
   ```

   The documents must return **200**, `application/json`, and the real signing
   identifiers, with **no redirects**. Missing/invalid signing values deliberately
   return 503 and `no-store`, rather than falsely claiming readiness. Successful
   documents cache for five minutes; OS/Apple CDN caches can take longer.
6. Build and install new signed Android/iOS apps with the Student repository's
   updated configuration. An OTA JavaScript update cannot change native intent
   filters or iOS entitlements. Complete the device checks in that repository's
   `APP_LINKS.md` before pilot sign-off.
7. Set `CLAIM_PAGE_URL=https://app.kormic.ai/claim` in the production backend and
   **Celery worker** environment, then restart both. New invite emails use that
   URL. Do not include a query string or fragment in this setting. Keep invitation
   sending disabled/unconfigured until the domain and fallback work.

## Fallback and existing links

If the app is installed and OS association succeeds, HTTPS opens its claim screen.
Otherwise Django redirects to `STUDENT_WEB_CLAIM_URL`, preserving the invitation
code so the browser displays the same claim form as the app. Without this setting,
the legacy installation-help page remains available with an explicit
`kormicstudent://claim?token=...` button. After installing, students must reopen
the email link; this is not deferred deep linking. Manual token entry remains
available. The app still performs email OTP verification; links do not authenticate
the student. Custom schemes are a convenience fallback, not a verified identity.

Old `https://backend.kormic.ai/claim?...` emails now have the same browser fallback
on the backend host; the app parser accepts them if delivered directly. Only
`app.kormic.ai` is registered as the verified production link host in new builds.
Existing `/claim/start/`, `/claim/verify/` and `/claim/confirm/` API compatibility
routes remain available.

The fallback has no analytics, third-party assets, JavaScript redirects or token
forwarding to store listings. It sends `no-store`, `no-referrer`, and noindex
headers. Redact query strings from upstream request/error logs and APM as well;
the supplied proxy disables access logging on this dedicated host.

## References

- [Android website associations](https://developer.android.com/training/app-links/configure-assetlinks)
- [Android verification](https://developer.android.com/training/app-links/verify-applinks)
- [Expo iOS Universal Links](https://docs.expo.dev/linking/ios-universal-links/)

## Email delivery and local development

Invitations are multipart HTML/plain text, sent as Kormic using the configured
`DEFAULT_FROM_EMAIL` mailbox. The institute name appears in the body and footer;
the same invitation code is in the button URL and a separate code box. The later
six-digit email verification code is separate. Opening a link never sends an OTP.

Use IONOS SMTP with `EMAIL_MODE=prod`, `EMAIL_HOST=smtp.ionos.com`, `EMAIL_PORT=587`,
`EMAIL_USE_TLS=true`, and `EMAIL_USE_SSL=false`. Set the mailbox credentials and
an authorized `DEFAULT_FROM_EMAIL` address. Restart the API and delivery workers
after environment changes. Authentication alone does not confirm inbox delivery.

- **Production:** `INVITE_DELIVERY_MODE=celery`, `CELERY_TASK_ALWAYS_EAGER=false`,
  a running broker and a Celery worker consuming the default queue. Existing
  Celery retry behavior applies to transient SMTP failures.
- **Local:** `INVITE_DELIVERY_MODE=database` and `python manage.py invitation_worker`.
  The root `Run-Kormic.bat` starts this worker automatically. It polls the persisted
  invitation outbox, claims each queued row atomically and recovers abandoned
  leases after five minutes. SMTP errors remain failed for manual retry; they
  are never reported as successful sends. Like SMTP generally, delivery across
  a crash between SMTP acceptance and saving status is at-least-once.
- Apply migrations before starting workers. The roster polls pending deliveries;
  Send invites also retries failed rows. Resend does not duplicate pending jobs.
- A status of sent means SMTP accepted the message, not that it reached the inbox.
  For spam/bounce issues verify the mailbox's SPF/DKIM/DMARC and provider reports.

`http://127.0.0.1:5173/claim` is suitable only for testing on this computer.
Live (`EMAIL_MODE=prod`) email delivery rejects localhost claim URLs with a
configuration error before queueing messages. Set the actual
public HTTPS frontend `/claim` URL before use. `CLAIM_PAGE_URL` may instead use
`https://app.kormic.ai/claim` when that host is deployed with the app associations
and a configured browser redirect. Native automatic opening additionally requires
the actual Android signing fingerprint / Apple app prefix and a signed release;
a browser-only test cannot verify OS association.
