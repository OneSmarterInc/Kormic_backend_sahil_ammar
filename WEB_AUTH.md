# Browser authentication

The Institute, University, Superuser and Student web clients keep access tokens
in memory only. They remove old localStorage credentials on startup. Users with
old browser sessions must log in again after this release. Android/iOS continue
using Expo SecureStore and the existing JSON-token endpoints.

Browser login uses `/api/auth/web/login/` and `/api/auth/web/verify-totp/`.
After MFA, the backend sends the refresh token only in a host-only HttpOnly,
Secure, SameSite=Lax cookie named `__Host-kormic-refresh-<portal>`. Its lifetime
is seven days; refreshing does not extend that deadline. Each portal has a
separate cookie. Five-minute access tokens are returned in JSON and used with
the Bearer header. No API authenticates ordinary requests using a cookie.

On a page reload, clients POST to `/api/auth/web/refresh/` with the portal name,
then load the user using the returned access token. Refresh checks the token
blacklist, current active status, portal role and TOTP enrollment. Browser
refresh tokens cannot be exchanged on the legacy JSON refresh endpoint.

## Deployment

1. Deploy the backend before the clients. No database migration or new key is
   needed for this change. Existing native refresh tokens remain valid.
2. Use HTTPS for the API and all portals, on the same site (for example
   `backend.kormic.ai` and portal subdomains of `kormic.ai`). Host cookies on the
   API only: do not set a broad cookie Domain. Unrelated hosting domains need
   a same-site API proxy/custom domain; Lax cookies intentionally do not support
   cross-site embedded authentication.
3. Set `DJANGO_DEBUG=false` and set `DJANGO_CORS_ALLOWED_ORIGINS` in backend
   `.env` to the exact HTTPS origins of all deployed portals, comma-separated,
   without paths or trailing slashes. This list also configures Django's CSRF
   trusted origins. Wildcard CORS is disabled, including in development.
4. Forward HTTPS correctly through the trusted reverse proxy. Keep auth
   responses uncached (`Cache-Control: no-store`) and preserve Set-Cookie.

Browser requests use `credentials: include` / Axios `withCredentials`.
Before every cookie-authentication POST, clients GET `/api/auth/web/csrf/`
and send the returned masked token in `X-CSRFToken`. Django verifies the CSRF
cookie/token pair and Origin/Referer even for unauthenticated login and logout.
The CSRF cookie is also HttpOnly; clients obtain the masked value through JSON,
not document.cookie. Only allow-listed origins may read credentialed responses.

Local HTTP development (`DJANGO_DEBUG=true`) uses unprefixed, non-Secure
refresh cookies. With the origin setting blank, only localhost/127.0.0.1 ports
5173, 5174, 5175 and 8081 are allowed. Use the same hostname for frontend and API
(do not mix localhost and 127.0.0.1). Never use DEBUG on a public deployment.

## Logout and limitations

POST `/api/auth/web/logout/` blacklists the cookie refresh token and deletes
that portal's cookie, even when the access token expired. It is idempotent and
CSRF-protected. Clients clear in-memory access and prevent a pending refresh
from restoring it. Other portal sessions remain independent. A network failure
cannot revoke a server session: retry logout when online. Previously issued
access tokens expire within five minutes, as with other JWT logout flows.

HttpOnly blocks direct JavaScript extraction of the refresh credential. It does
not make XSS harmless: injected code can still act within a live browser
session. Continue dependency maintenance and XSS prevention.

Verification covers browser cookie flags, no refresh token in JSON/storage,
CSRF/origin rejection, portal isolation, reload/refresh, expired or revoked
sessions, logout revocation and native token compatibility. See
`accounts/test_web_auth.py` and frontend authentication tests.

References: [Django CSRF protection](https://docs.djangoproject.com/en/5.2/ref/csrf/)
and [Simple JWT blacklist](https://django-rest-framework-simplejwt.readthedocs.io/en/stable/blacklist_app.html).
