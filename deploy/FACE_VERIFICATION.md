# Student face verification — testing rollout

The integration uses the supplied `det_10g.onnx` detector and `w600k_r50.onnx`
recognition model through CPU ONNX Runtime. The supplied `2d106det.onnx` is
checksum-validated during installation but is not used by the current pose check.
No model weights are committed to Git or included in the Docker build context.

## Flow

1. Password login, followed by TOTP (or an existing backup code).
2. A short-lived face-only access token, without a refresh token.
3. Consent and a server-issued challenge: face forward, two randomly ordered
   head turns, then face forward again. Each image is evaluated on the server.
4. First scan creates an encrypted, account-bound face template. Later scans
   must match that template. This endpoint cannot replace an existing template.
5. Only a completed scan issues normal access and refresh credentials. The app
   can then save profile details and proceed to GitHub, LinkedIn and CV setup.

Other portal roles are unchanged. Existing student refresh tokens issued before
activation cannot bypass the new factor: students must sign in again. Normal
refresh of a face-verified session remains supported; an app resume is not a new
password login. This is account face matching, not proof of a legal identity.

## Prepare and enable

Deploy backend migrations and the updated student client together. Keep
`STUDENT_FACE_AUTH_REQUIRED=false` until the phone checks below pass in a test
environment. Do not turn it on against an old APK.

```bash
python manage.py install_face_models /path/to/kormic-liveness-models.zip
python manage.py migrate
```

The command extracts only the three pinned model filenames and verifies SHA-256
and ONNX loading. For Docker, install/extract them into the host `face_models/`
folder before starting the stack. `compose.aws.yml` mounts that folder read-only
at `/app/face_models`; do not put the archive or models in the image.

Generate a Fernet key once, put it in the deployment's secret environment and
retain it securely with database backups:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

```dotenv
STUDENT_FACE_ENCRYPTION_KEY=<the-generated-key>
FACE_MODEL_DIRECTORY=/app/face_models
STUDENT_FACE_MATCH_THRESHOLD=0.42
STUDENT_FACE_AUTH_REQUIRED=true
```

Changing or losing the key makes stored templates unreadable. Do not commit it.
The matching threshold is an initial testing value, not an accuracy guarantee.
Run `python manage.py install_face_models` inside the deployed web container to
check its actual mounted files and runtime before activation. Models load lazily
per web worker; measure memory and latency on the 4 GiB EC2 instance before
increasing workers or concurrent scans.

## Storage and retention

Raw camera images are processed in backend memory. The app deletes its temporary
native camera photo after each submission. The database stores encrypted face
templates, challenge metadata, and SHA-256 capture fingerprints. Fingerprints
reject exact capture replay for the same account, including across challenges.
Completed, failed-attempt-limit and superseded challenges clear their temporary
template. Account deletion cascades to these records. Expired abandoned scans
are cleared hourly by the Celery beat task `accounts.tasks.cleanup_face_challenges`.
The command `python manage.py cleanup_face_challenges` also runs this cleanup
manually. Both Celery beat and the default-queue worker must run in deployment.

## Required physical-device acceptance checks

- New account: TOTP → face enrollment → profile/CV/GitHub setup.
- Existing account: TOTP → same-person success; different-person refusal.
- Camera denial, offline/timeout, expired scan, repeated capture, replayed
  challenge and repeated tap must not unlock the account.
- Check left/right instructions on front cameras, glasses, lighting and head
  turns. Ensure each supported Android device returns captures below 2 megapixels.
- Test printed photos, screen photos and recorded video attacks, and calibrate
  matching on a representative consented test set.

**Current security limit:** head-pose checks plus exact-frame replay detection
are not a validated presentation-attack detector. Re-encoded video, synthetic
frames and camera injection are not reliably rejected by these models alone.
Do not treat this build as production anti-spoof protection. A validated
anti-spoof component and device testing are still needed for that requirement.

InsightFace's published terms distinguish the MIT library from its pretrained
models, which are described as non-commercial research only. Confirm appropriate
rights for production use of these supplied weights or replace them with licensed
models and recalibrate matching:
https://github.com/deepinsight/insightface/tree/master/python-package#license
