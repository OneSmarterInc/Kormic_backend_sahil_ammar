# CI/CD and release gates

All repositories run blocking checks on every pull request and every push to main. Student CI also runs on `release/**` pushes. No test, scan, or build uses `continue-on-error`. Manual dispatch is available.

| Repository | Required check names | Coverage |
| --- | --- | --- |
| Backend | `backend-tests`, `Dependency security scan`, `Docker build and pilot smoke` | Django system check, migration drift, complete and shuffled PostgreSQL/Redis tests; strict pip-audit including transitive dependencies; real image/Compose startup, migrations, API and worker health |
| Student | `student-app-tests`, `Native Android build`, `profile-contract` | format, lint, typecheck, complete Jest tests, CSRF/browser journeys and exports; native ARM64 release-variant compilation; generated API contract drift |
| University / Institute / Superuser | `web-baseline` | npm ci, lint, tests, explicit production build and Playwright smoke/regressions |

Configure these checks as required in GitHub rulesets/branch protection for main and release branches, with PR review and up-to-date branch requirements. Workflow failure alone is not a repository merge restriction. This GitHub connection can edit workflow files but cannot administer repository protection; an administrator must apply/verify the required-check settings. Keep existing runtime/schema contract checks required too.

The dependency scan fails on vulnerabilities and audit errors; it has no blanket ignore list. Update affected pins and rerun the complete suite. Reports remain attached to the workflow. The Docker job does not publish images or deploy to a server.

Android validation runs on all PRs, main and release branches, so release-branch creation cannot bypass native compilation. It generates the native project using locked Expo dependencies and compiles an ARM64 release variant with CI/debug signing. If Firebase configuration is absent, the job supplies an explicitly non-production fixture solely for compilation. The retained APK is not store-ready and cannot prove real push delivery. Production releases must supply real Firebase configuration, signing credentials and an EAS/store release process. No Expo account token is required for this validation gate.

Student `npm run format` checks authored code and documentation. Build outputs, dependencies and generated native directories are excluded; the profile code generator formats its output with the same Prettier configuration, so its drift check remains compatible.

Follow DEPLOYMENT.md for the deterministic single-host pilot. AWS service migration is intentionally deferred. See TESTING_MATRIX.md for staging and physical-device acceptance checks still required before release.
