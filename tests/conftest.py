import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("DESIGN_AUTH_SECRET", "unit-test-secret-which-is-at-least-32-characters")
