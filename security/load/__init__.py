"""Abuse / load simulation tests.

DANGER LAYER. These simulate hostile traffic — request floods, oversized
resolutions, disk-cache growth, Spotfinder compute amplification — to
measure whether the abuse-resistance fixes from SECURITY_THREAT_MODEL.md
actually hold.

They are destructive by nature (they can fill disk and pin CPU) and MUST
NOT run in CI. run_security.py refuses to execute this layer unless BOTH
--load and --confirm are passed. See security/README.md for the full
warning, especially the disk-cache quota prerequisite.
"""
