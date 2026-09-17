# TOTP encryption, rotation and recovery

TOTP seeds are stored only in `accounts_totpdevice.secret_encrypted`, using
authenticated Fernet encryption (the same cryptography library as GitHub OAuth
tokens). The model's `secret` property encrypts on assignment and decrypts on
access; Django serialization/dumps contain ciphertext, not the property. Admin
shows metadata only and cannot create seeds. Backup codes remain one-way hashes.

## Required key and first deployment

`TOTP_SECRET_KEYS` is a comma-separated key ring. The first key encrypts new
seeds; all keys may decrypt existing rows. It is independent of database
credentials, `DJANGO_SECRET_KEY`, and `GITHUB_OAUTH_TOKEN_KEY`; there is no derived
or hardcoded default and no plaintext fallback. `manage.py check` rejects missing
or malformed keys. Authentication returns a generic 503 if a seed cannot be
decrypted; it never treats a key failure as successful MFA.

Generate a key on a trusted administrative machine:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store it in the backend's ignored `.env` as `TOTP_SECRET_KEYS`, separate from
PostgreSQL and database backups. Restrict that file to the service account and
trusted administrators (for example `chmod 600 .env` on Linux). A secret manager
such as AWS Secrets Manager is also supported through environment injection.
Supply the same key ring to migrations, web processes, and Celery workers.
Never commit, log, email, or place the key in a frontend environment variable.
Keep a separate, access-controlled recovery copy and test restoring it.

1. Back up the database and securely retain the corresponding keys separately.
2. Stop old web/worker processes for a maintenance window. This migration renames
   the secret column; old and new application versions must not run concurrently.
3. Inject `TOTP_SECRET_KEYS`, deploy this version, run `python manage.py check`,
   then `python manage.py migrate`.
4. Migration `accounts.0002_encrypt_totp_secrets` expands/renames the column and
   encrypts every existing seed in one PostgreSQL transaction. Confirmation dates,
   seed values and authenticator registrations are preserved. Any failure rolls
   back the migration; correct the key/configuration and retry before reopening.
5. Run `python manage.py rotate_totp_secrets --check`, restart all services, and
   verify enrollment, TOTP login and backup-code login.

The migration is intentionally irreversible: rolling back does not silently
write plaintext. Roll forward to fix application problems. Old database snapshots,
WAL archives and replicas may still contain historical plaintext. Protect them
and expire/replace them under your retention policy; encrypting live rows cannot
erase old backups. Do not use a database restore as an unplanned rollback.

## Key rotation without re-enrollment

1. Generate `NEW`. First deploy the ring `OLD,NEW` to **every** web/worker instance
   and migration job. All processes can now read both keys while still writing
   OLD. Retain any older keys that still protect rows/backups.
2. Once all instances know NEW, deploy `NEW,OLD` everywhere and restart/reload
   their environments. Wait until no process still writes with OLD.
3. Run:

   ```sh
   python manage.py rotate_totp_secrets
   python manage.py rotate_totp_secrets --check
   ```

   Rotation locks rows, re-encrypts with the primary key and commits atomically.
   A corrupt row or missing old key aborts the entire rotation. The read-only
   `--check` verifies every row with the primary key **alone**. Output contains
   counts only, never seeds or keys. Run during a low-traffic/maintenance window
   because locks may delay enrollment. Re-running rotation is safe.
4. Confirm check success and absence of old writers, then deploy `NEW` alone.
   Keep OLD in protected recovery storage until all backups encrypted with it
   have expired. Authenticator apps need no QR-code rescan because seeds do not
   change. Rotating keys does not repair a prior seed compromise: reset affected
   TOTP devices and revoke their sessions if seeds were exposed.

## Recovery

- Missing/incorrect key: restore the correct ring from the secret manager and
  restart services. Do not replace it with a random key and expect old seeds to
  decrypt. Diagnose corruption using administrative tooling without logging seeds.
- Lost authenticator: users may log in with a valid unused backup code. Backup
  code validation does not decrypt a seed. Use the existing audited superuser
  `POST /api/superuser/users/<id>/remove-totp/` flow to remove an affected device and
  backup codes, revoke sessions through the existing force-logout action, then
  require fresh enrollment. Verify the user's identity first.
- All encryption keys irretrievably lost: encrypted seeds cannot be recovered.
  Recover operator access with a retained backup code or a tightly controlled
  break-glass operator created using `create_superuser_account` under a new key.
  That operator can reset affected accounts and revoke sessions as above. Do not
  disable the TOTP gate or invent a decryption fallback. Record the recovery in
  your operational audit trail.
- Restore an old database only with the key ring that decrypts that snapshot;
  verify it before accepting logins, then rotate forward. Pre-encryption backups
  require the one-way migration again before reopening the service.

## Portal-specific login

Updated Student, Institute, University and Superuser clients send their `portal`
role on both password login and TOTP verification. The backend compares that
role after password validation but **before** issuing an enrollment access token
or MFA challenge. A mismatch returns the same `401 Invalid credentials.` as a bad
password, without disclosing the account's actual role. MFA challenges are bound
to the requested portal; changing/omitting it or changing/deactivating the account
between steps cannot complete that challenge.

Deploy the backend before the updated clients. The field remains optional for
older released clients; generic logins still require MFA and all existing API
role permissions remain enforced. A caller-supplied portal is a login-flow
constraint, not a substitute for API authorization or proof of browser origin.
Sign out any pre-existing wrong-portal sessions when verifying the updated UI.

References: [Fernet and MultiFernet rotation](https://cryptography.io/en/latest/fernet/).
