"""Redaction rules ported from GitHub AI core/github.py."""
import re

def redact(text):
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED KEY]', text, flags=re.S)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{20,})\b', '[REDACTED TOKEN]', text)
    text = re.sub(r'(?im)^([^\n]*(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*).+$', r'\1[REDACTED]', text)
    return text
