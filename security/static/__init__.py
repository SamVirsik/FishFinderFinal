"""Static analysis security tests.

No running server required. Tests in this layer read the source tree,
configuration, and dependency manifests directly — they assert on code
shape (e.g. the Flask bind is 127.0.0.1, debug is False, no hardcoded
secrets) rather than runtime behavior.

Add test modules here as test_*.py. They run every time (including in CI).
"""
