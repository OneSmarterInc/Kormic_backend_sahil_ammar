# P2 onboarding and local web authentication fixes

- P2-01: removed the inactive mock liveness screen, service, state and actions. No biometric data is collected and no liveness claim is made.
- P2-02: replaced timed agent-construction stages with a user-controlled Profile ready confirmation. Source uploads retain their real request/loading behavior. No backend construction job or identity verification is implied.
- P2-03: removed incomplete roadmap generation/history routes and API-discovery advertising. Existing roadmap models and stored data are retained. No active student roadmap screen was present. Reintroduction requires a tested planner and an explicit product/API contract.
- P2-04: phone input preserves international prefixes and formatting. Country-aware libraries validate on both clients and server; successful profile writes store E.164. Explicit +country codes override residence, allowing foreign numbers. National-format API submissions must include country; unknown countries require an international prefix. Existing stored phone values are not guessed or bulk-rewritten. Parsing does not verify phone ownership.
- P2-05: one jest-expo declaration remains, in devDependencies, with a matching lockfile. Complete lint, TypeScript, Jest and Expo exports remain blocking CI gates; no continue-on-error is present.

## Screenshot: CSRF cookie not set

The browser page used localhost:8081 while the API used 127.0.0.1:8000. These are different sites for cookies. The development student API resolver now matches loopback API hostname to the browser page; native, LAN and remote origins are unchanged. The shared environment matrix uses localhost consistently, and generated local CORS/CSRF origins allow both loopback aliases.

Restart Expo after installing dependencies and changing environment values. Use http://localhost:8081 and EXPO_PUBLIC_API_BASE_URL=http://localhost:8000/api. Refresh the page before signing in. Production must serve app/API over HTTPS on the same site or use a same-site API proxy. CSRF checks, HttpOnly cookies, SameSite=Lax, and production Secure flags remain enabled. The app reports actionable cookie guidance if the browser still refuses cookies.

## Validation and rollout

Install dependencies using npm ci for the student and pip install -r requirements.txt for the backend. Use the companion frontend/backend PRs together for international phone normalization. No additional database migration is introduced by P2; the preceding chat changes still require their migration/worker rollout.

Run npm run lint, npm run typecheck, npm test and npx expo export --platform all. Backend targeted tests: python manage.py test django_api.test_phone_and_roadmap accounts.test_basic_info_persistence accounts.test_web_auth. Environment tests: python scripts/test_configure_environment.py. Full backend normal/shuffled CI remains required.
