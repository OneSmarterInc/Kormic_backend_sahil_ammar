from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed


class FaceGatedAuthentication(JWTAuthentication):
    def authenticate(self, request):
        from accounts.face_auth import required
        result = super().authenticate(request)
        if not result:
            return result
        user, token = result
        if not required(user) or token.get('face_verified') is True:
            return result
        path = request.path_info
        # Enrollment access remains limited even after the DB's TOTP flag changes.
        if path in {'/api/auth/totp/enroll/', '/api/auth/totp/verify-enrollment/', '/api/auth/logout/'}:
            return result
        if token.get('face_pending') is True and (path.startswith('/api/auth/face/') or path == '/api/auth/me/'):
            return result
        raise AuthenticationFailed('Face verification is required. Please sign in again.', code='face_required')
