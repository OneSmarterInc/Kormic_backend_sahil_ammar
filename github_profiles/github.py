"""GitHub AI collection protocol using the student's existing OAuth grant."""
import base64
from urllib.parse import quote

import httpx
from .errors import ServiceError
from .redaction import redact

PROFILE_FIELDS = ('id', 'login', 'name', 'bio', 'html_url', 'avatar_url', 'public_repos', 'public_gists', 'followers', 'following', 'created_at', 'updated_at', 'company', 'blog', 'location', 'hireable', 'twitter_username')
REPO_FIELDS = ('id', 'name', 'full_name', 'description', 'html_url', 'private', 'fork', 'language', 'topics', 'default_branch', 'created_at', 'updated_at', 'pushed_at', 'stargazers_count', 'forks_count', 'open_issues_count', 'size', 'archived', 'disabled', 'homepage', 'license', 'owner')


class GitHub:
    def __init__(self, token, progress=lambda: None):
        self.token = token
        self.progress = progress

    def get(self, path, params=None, optional=False):
        if not path.startswith('/') or any(part in ('.', '..') for part in path.split('/')) or '://' in path:
            raise ServiceError('Invalid GitHub resource path.')
        self.progress()
        try:
            response = httpx.get('https://api.github.com' + path, params=params,
                headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}, timeout=25, follow_redirects=False)
            if optional and response.status_code == 404:
                return None
            if response.status_code == 401:
                raise ServiceError('GitHub access expired or was revoked. Reconnect your GitHub account.')
            if response.status_code in (403, 429):
                raise ServiceError('GitHub denied access or rate-limited this resource. Try again later.')
            if response.status_code != 200:
                raise ServiceError('GitHub could not return this resource; it may be empty or inaccessible.')
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceError('GitHub could not be reached. Previously saved information is retained.') from exc

    def pages(self, path, params=None):
        page = 1
        while True:
            batch = self.get(path, {**(params or {}), 'per_page': 100, 'page': page})
            if not isinstance(batch, list):
                raise ServiceError('GitHub returned an invalid repository page.')
            yield batch
            if len(batch) < 100:
                break
            page += 1

    def file(self, full_name, path, sha):
        if path.startswith('/') or any(part in ('.', '..') for part in path.split('/')):
            raise ServiceError('Invalid source path.')
        data = self.get('/repos/' + full_name + '/contents/' + quote(path, safe='/'), {'ref': sha})
        if not isinstance(data, dict) or data.get('type') != 'file' or data.get('size', 0) > 100000 or data.get('encoding') != 'base64':
            raise ServiceError('File is unavailable or not eligible for text analysis.')
        try:
            return redact(base64.b64decode(data['content']).decode('utf-8'))
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise ServiceError('File is not readable text.') from exc
