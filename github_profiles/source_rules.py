"""Ported source selection and finding schema from GitHub AI core/analysis.py."""
from pathlib import PurePosixPath
from pydantic import BaseModel, Field

class Skill(BaseModel):
    name: str = Field(max_length=100)
    evidence_ids: list[int] = Field(min_length=1, max_length=5)


class ProjectFinding(BaseModel):
    summary: str = Field(max_length=1800)
    domains: list[str] = Field(max_length=5)
    skills: list[Skill] = Field(max_length=12)
    limitations: list[str] = Field(max_length=8)


def eligible(path):
    p = PurePosixPath(path)
    blocked = {'node_modules', 'vendor', 'dist', 'build', '.git', '.venv', 'venv', '__pycache__', '.next', '.nuxt', 'coverage'}
    name = p.name.lower()
    lockfiles = {'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'bun.lockb',
                 'composer.lock', 'pipfile.lock', 'poetry.lock', 'uv.lock', 'yarn.lock',
                 'cargo.lock', 'packages.lock.json'}
    generated_suffixes = ('.min.js', '.min.css', '.map', '.generated.ts', '.generated.js',
                          '.g.cs', '.g.dart', '.freezed.dart', '_pb2.py', '_pb2_grpc.py', '.pb.go')
    if (set(part.lower() for part in p.parts) & blocked or name.startswith('.env')
            or name in lockfiles or name.endswith(generated_suffixes)
            or p.suffix.lower() in {'.pem', '.key', '.lock', '.svg'}):
        return False
    return p.suffix.lower() in {'.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.md', '.toml', '.json', '.yml', '.yaml', '.sql', '.cs', '.rb', '.html', '.css', '.scss', '.c', '.cpp', '.h', '.php', '.swift', '.kt', '.dart', '.r'} or p.name in {'Dockerfile', 'requirements.txt', 'go.mod'}


def rank(path):
    name = PurePosixPath(path).name.lower()
    if name.startswith('readme'):
        return 0
    if name in {'package.json', 'pyproject.toml', 'requirements.txt', 'cargo.toml', 'go.mod'}:
        return 1
    if any(word in path.lower() for word in ('route', 'model', 'view', 'test', 'main', 'app', 'src/')):
        return 2
    return 3
