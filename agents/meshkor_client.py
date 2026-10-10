import hashlib
import logging
from pathlib import Path
from django.conf import settings
from meshkor.integrations.kormic import KormicMeshKorIntegration
import meshkor.integrations.kormic
# Monkey-patch the strict 0.2s timeout to 5.0s to allow AWS EC2 internet latency from India
meshkor.integrations.kormic.NETWORK_TIMEOUT_SEC = 5.0

logger = logging.getLogger(__name__)

# Finding 2c: One module-level instance reused at all call sites
meshkor_client = KormicMeshKorIntegration()

CLASS_STUDENT = "STU"
CLASS_UNIVERSITY = "UNI"

# Finding 2a: Real constitution hash computed once at module load
CONSTITUTION_PATH = Path(settings.BASE_DIR) / "personas" / "aria_constitution.py"
CONSTITUTION_HASH = hashlib.sha256(CONSTITUTION_PATH.read_bytes()).hexdigest()

# Finding 2b: Real-schema manifests per class
MANIFEST_STUDENT = {
    "allowed_tools": [],
    "read_scopes": ["student_profile", "chat_history", "academic_data"],
    "allowed_egress": ["host.docker.internal"],
}

MANIFEST_UNIVERSITY = {
    "allowed_tools": [],
    "read_scopes": ["university_data", "student_context"],
    "allowed_egress": ["host.docker.internal"],
}

MANIFEST_VERIFICATION = {
    "allowed_tools": [],
    "read_scopes": ["profile", "resume", "github", "linkedin"],
    "allowed_egress": ["api.github.com", "host.docker.internal"],
}
