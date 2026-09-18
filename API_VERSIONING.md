# API version policy

`/api/v1/` is the canonical prefix for mobile, external clients and all three web portals. Every existing API family is mounted there with the same handlers, authentication, TOTP, authorization, throttles and error envelopes. `/api/` stays available for existing releases; there is no removal or sunset date in this change. Requests are handled directly, without redirects that could change POST bodies or credential behavior.

Examples: `/api/v1/auth/me/`, `/api/v1/chat/agent/`, `/api/v1/verification/status/`, `/api/v1/university-admin/knowledge-sources/`, `/api/v1/auth/privacy/export/`, `/api/v1/superuser/metrics/models/`. Health and the student-profile OpenAPI contract are at `/api/v1/health/` and `/api/v1/schema/`. The schema remains explicitly scoped to the profile contract, not a claim of complete API documentation.

Versioned responses include `X-API-Version: v1`. Compatibility responses include `X-API-Version: legacy` and a relative `Link` with `rel="successor-version"` pointing to the corresponding v1 URL. Unknown versions return 404. The student transport never silently changes an explicitly versioned request to another version. Attachment links returned from v1 requests remain in v1; existing legacy links continue to work.

## Rollout

Deploy the backend aliases before publishing updated clients. Web portal environment values remain API origins (without a path); their transports append `/api/v1`. Student environment generation now emits the full `/api/v1` base, and its configuration resolver accepts old origin or `/api` values while upgrading new builds to v1. Older published builds continue to use their unchanged URLs.

Browser refresh and CSRF cookies already use `Path=/`; sessions work across both prefixes without moving cookies or relaxing CSRF checks. Native bearer-token contracts are unchanged. Tests exercise password/TOTP, refresh/logout, portal isolation, CSRF/Origin checks, and legacy-cookie refresh through v1.

The environment generator now emits `/api/v1/auth/github/callback/`. Register that redirect URI with the GitHub OAuth application before adopting the generated value. Existing explicitly configured `/api/auth/github/callback/` redirects remain supported, allowing a coordinated provider configuration rollout.

## Compatibility rules

Within v1, preserve field meanings, authentication and ownership semantics, error codes/statuses, and required inputs. Additive optional response fields are allowed; clients should ignore unknown fields. Removing/renaming fields, changing types, or changing required input/behavior needs a new version and an explicit migration plan. Future v2 routes should use separate contract handlers rather than silently changing v1. Legacy retirement requires measured client adoption, release/support coordination, and a published deprecation/sunset period; none is invented here.

Keep the profile OpenAPI fixture and generated student types aligned. Route-parity and v1 auth tests are part of full backend CI; all portal browser fixtures require the v1 prefix. Changes to a public contract must include behavior tests before release.
