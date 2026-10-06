"""Student-only face factor, scoped to a fresh TOTP-authenticated login."""
import base64
import binascii
import hashlib
import json
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.authentication import FaceGatedAuthentication
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import Account, StudentFaceChallenge, StudentFaceCredential, StudentFaceCapture, TOTPDevice
from accounts.face_engine import FaceScanError, measure, similarity
from accounts.face_models import MODEL_HASHES


def required(user):
    return settings.STUDENT_FACE_AUTH_REQUIRED and Account.objects.filter(user=user, role='student').exists()


def pending_response(user, portal=None):
    from accounts.serializers import serialize_user
    token = AccessToken.for_user(user)
    token.set_exp(lifetime=timedelta(minutes=10))
    token['face_pending'] = True
    if portal:
        token['web_portal'] = portal
    data = serialize_user(user)
    data['face_verification_required'] = True
    data['face_enrolled'] = StudentFaceCredential.objects.filter(user=user).exists()
    return {'access': str(token), 'user': data, 'face_verification_required': True}



def cipher():
    key = settings.STUDENT_FACE_ENCRYPTION_KEY
    if not key:
        raise RuntimeError('Student face encryption key is not configured')
    return Fernet(key.encode())


def seal(user, vector):
    return cipher().encrypt(json.dumps({'user': user.pk, 'vector': vector}).encode())


def unseal(user, value):
    try:
        decoded = json.loads(cipher().decrypt(bytes(value)))
        if decoded['user'] != user.pk:
            raise ValueError('Owner mismatch')
        return decoded['vector']
    except (InvalidToken, ValueError, KeyError) as exc:
        raise FaceScanError('Face credentials are unavailable. Contact support.') from exc


class FaceBase(APIView):
    authentication_classes = [FaceGatedAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_scope = 'face'

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not required(request.user) or request.auth.get('face_pending') is not True or not TOTPDevice.objects.filter(user=request.user, confirmed_at__isnull=False).exists():
            raise AuthenticationFailed('A fresh TOTP verification is required.')


class FaceStartView(FaceBase):
    def post(self, request):
        if request.data.get('consent') is not True:
            return Response({'detail': 'Consent is required to scan and store your encrypted face template.'}, status=400)
        try:
            cipher()
        except (RuntimeError, ValueError):
            return Response({'detail': 'Face verification is temporarily unavailable. Please contact support.'}, status=503)
        now = timezone.now()
        with transaction.atomic():
            Account.objects.select_for_update().get(user=request.user)
            # One completed challenge cannot mint another session from the same TOTP token.
            previous = StudentFaceChallenge.objects.filter(user=request.user, login_jti=request.auth['jti'])
            if previous.filter(passed=True).exists() or previous.count() >= 5:
                return Response({'detail': 'Please sign in again to start a new verification.'}, status=403)
            StudentFaceChallenge.objects.filter(user=request.user, finished_at__isnull=True).update(finished_at=now, encrypted_reference=b'')
            turns = ['left', 'right']
            secrets.SystemRandom().shuffle(turns)
            scan = StudentFaceChallenge.objects.create(user=request.user, login_jti=request.auth['jti'],
                sequence=['center', *turns, 'center'], step_started_at=now,
                expires_at=now + timedelta(minutes=3))
        return Response({'id': str(scan.pk), 'action': scan.sequence[0], 'step': 0, 'total_steps': 4,
                         'mode': 'verify' if StudentFaceCredential.objects.filter(user=request.user).exists() else 'enroll'})


class FaceStepView(FaceBase):
    def post(self, request, challenge_id):
        with transaction.atomic():
            Account.objects.select_for_update().get(user=request.user)
            scan = StudentFaceChallenge.objects.select_for_update().filter(pk=challenge_id, user=request.user,
                login_jti=request.auth['jti']).first()
            now = timezone.now()
            if not scan or scan.finished_at or scan.expires_at <= now:
                if scan and scan.encrypted_reference:
                    scan.encrypted_reference = b''
                    scan.finished_at = now
                    scan.save(update_fields=['encrypted_reference', 'finished_at'])
                return Response({'detail': 'This face scan has expired or already been used.'}, status=403)
            if request.data.get('step') != scan.step:
                return Response({'detail': 'The scan step is out of order.'}, status=400)
            if (now - scan.step_started_at).total_seconds() < 1:
                return Response({'detail': 'Hold the requested pose briefly before capturing.'}, status=400)
            image = request.data.get('image')
            if not isinstance(image, str) or len(image) > 1400000:
                return Response({'detail': 'Invalid camera capture.'}, status=400)
            try:
                capture_hash = hashlib.sha256(base64.b64decode(image, validate=True)).hexdigest()
            except (ValueError, binascii.Error):
                return Response({'detail': 'Invalid camera capture.'}, status=400)
            if StudentFaceCapture.objects.filter(user=request.user, digest=capture_hash).exists():
                return Response({'detail': 'Use a fresh camera capture for each step.'}, status=400)
            scan.attempts += 1
            try:
                yaw, vector = measure(image)
                action = scan.sequence[scan.step]
                pose_ok = abs(yaw) < 0.16 if action == 'center' else yaw > 0.18 if action == 'left' else yaw < -0.18
                if not pose_ok:
                    raise FaceScanError('Follow the requested head movement, then try again.')
                credential = StudentFaceCredential.objects.filter(user=request.user).first()
                if credential and credential.model_sha256 != MODEL_HASHES['w600k_r50.onnx']:
                    raise FaceScanError('Your stored face model requires support assistance.')
                reference = unseal(request.user, scan.encrypted_reference) if scan.encrypted_reference else vector
                if similarity(reference, vector) < settings.STUDENT_FACE_MATCH_THRESHOLD:
                    raise FaceScanError('Face did not match. Access denied.')
                if credential and similarity(unseal(request.user, credential.encrypted_template), vector) < settings.STUDENT_FACE_MATCH_THRESHOLD:
                    raise FaceScanError('Face did not match this account. Access denied.')
                if not scan.encrypted_reference:
                    scan.encrypted_reference = seal(request.user, vector)
            except FaceScanError as exc:
                if scan.attempts >= 10:
                    scan.finished_at = now
                    scan.encrypted_reference = b''
                scan.save(update_fields=['attempts', 'finished_at', 'encrypted_reference'])
                return Response({'detail': str(exc)}, status=403 if scan.finished_at else 400)
            except (RuntimeError, OSError, ValueError):
                return Response({'detail': 'Face verification is unavailable. Please retry later.'}, status=503)
            StudentFaceCapture.objects.create(user=request.user, digest=capture_hash)
            scan.capture_hashes = [*scan.capture_hashes, capture_hash]
            scan.step += 1
            scan.step_started_at = now
            if scan.step < len(scan.sequence):
                scan.save()
                return Response({'id': str(scan.pk), 'action': scan.sequence[scan.step], 'step': scan.step, 'total_steps': 4})
            if not credential:
                # The account row lock prevents concurrent enrollment/replacement.
                StudentFaceCredential.objects.create(user=request.user,
                    encrypted_template=scan.encrypted_reference, consent_at=scan.created_at,
                    model_sha256=MODEL_HASHES['w600k_r50.onnx'])
            scan.passed = True
            scan.finished_at = now
            scan.encrypted_reference = b''
            scan.save()
            from project_superuser.models import ActivityLog
            from project_superuser.services import log_activity
            log_activity(ActivityLog.Action.LOGIN_SUCCEEDED, actor=request.user, target_user=request.user)
            from accounts.views import _serialize_user_with_verification
            refresh = RefreshToken.for_user(request.user)
            refresh['face_verified'] = True
            refresh['face_challenge'] = str(scan.pk)
            data = _serialize_user_with_verification(request.user)
            data['face_verification_required'] = False
            data['face_enrolled'] = True
            response = Response({'access': str(refresh.access_token), 'refresh': str(refresh), 'user': data, 'passed': True})
            if request.auth.get('web_portal'):
                from accounts.web_auth import cookie_name
                refresh['web_portal'] = request.auth['web_portal']
                response.data.pop('refresh')
                response.data['access'] = str(refresh.access_token)
                response.set_cookie(cookie_name(request.auth['web_portal']), str(refresh),
                    max_age=int(refresh['exp'] - now.timestamp()), secure=not settings.DEBUG,
                    httponly=True, samesite=settings.WEB_COOKIE_SAMESITE, path='/')
            return response
