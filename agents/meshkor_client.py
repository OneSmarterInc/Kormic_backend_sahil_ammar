import hashlib
import logging
from pathlib import Path
from django.conf import settings
from meshkor.integrations.kormic import KormicMeshKorIntegration

logger = logging.getLogger(__name__)

# Finding 2c: One module-level instance reused at all call sites
meshkor_client = KormicMeshKorIntegration()

# Finding 2a: Real constitution hash computed once at module load
CONSTITUTION_PATH = Path(settings.BASE_DIR) / "personas" / "aria_constitution.py"
try:
    CONSTITUTION_HASH = hashlib.sha256(CONSTITUTION_PATH.read_bytes()).hexdigest()
except Exception as e:
    logger.warning(f"Could not compute constitution hash: {e}")
    CONSTITUTION_HASH = "pilot_hash_1"

# Finding 2b: Real-schema manifests per class
MANIFEST_STUDENT = {
    "allowed_tools": ["advising_tools", "profile_tools", "roadmap_tools"],
    "read_scopes": ["student_profile", "chat_history", "academic_data"],
    "allowed_egress": ["api.openai.com", "api.anthropic.com"],
}

MANIFEST_UNIVERSITY = {
    "allowed_tools": ["university_tools", "knowledge_base", "officer_tools"],
    "read_scopes": ["university_data", "student_context"],
    "allowed_egress": ["api.openai.com", "api.anthropic.com"],
}

MANIFEST_VERIFICATION = {
    "allowed_tools": ["verification_engine"],
    "read_scopes": ["profile", "resume", "github", "linkedin"],
    "allowed_egress": ["api.github.com", "api.anthropic.com"],
}

import contextvars
current_ain = contextvars.ContextVar('current_ain', default=None)

import meshkor.integrations.kormic
# Overriding strict 0.2s fail-open timeout for Pilot environment (AWS geographic distance)
meshkor.integrations.kormic.NETWORK_TIMEOUT_SEC = 5.0