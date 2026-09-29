import time
import pytest
import os
from django.test import TestCase
from django_api.models import AgentIdentity
from agents.identity_registry import get_or_create_identity
from agents.meshkor_client import meshkor_client

class MeshKorFailOpenTest(TestCase):
    def test_hq_down_circuit_breaker(self):
        # Point to unroutable address
        meshkor_client.hq_url = "http://10.255.255.1:8080"
        # Reset circuit breaker just in case
        import meshkor.integrations.kormic as k_mod
        k_mod._breaker_tripped_until = 0

        start_time = time.time()

        for i in range(100):
            identity = get_or_create_identity("student", f"test_student_{i}", f"Aria_{i}")
            self.assertIsNone(identity.ain)
            # Prevent DB spam from saving identically multiple times if we rerun
            identity.delete()

        end_time = time.time()
        total_time = end_time - start_time

        # Ensure total wall time is well under a second (proves circuit breaker works)
        self.assertLess(total_time, 1.0, f"Circuit breaker failed: took {total_time} seconds")
