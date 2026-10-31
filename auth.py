"""
Authentication & JWT Token Helper Module for RERA Web Portal
Handles password hashing, password verification, and JWT access token creation/decoding.
"""

import os
import sys
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import jwt

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
MIN_PASSWORD_LENGTH = 8

# Never fall back to a hardcoded secret: anyone reading the repo could forge tokens.
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    print(
        "[!] JWT_SECRET_KEY is not set -- using a random per-process secret. "
        "Logins will not survive a restart and will not work across multiple workers. "
        "Set JWT_SECRET_KEY in production.",
        file=sys.stderr,
    )

# Secret used only to verify password hashes created by the old HMAC-SHA256 fallback.
_LEGACY_HMAC_SECRET = os.getenv("LEGACY_PASSWORD_SALT", "rera_secure_jwt_secret_key_2026_super_secret")


def _pw_bytes(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes; bcrypt>=4.1 raises instead of truncating.
    # Pre-hash long passwords so every character still counts.
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raw = hashlib.sha256(raw).hexdigest().encode("ascii")
    return raw


def hash_password(password: str) -> str:
    """Hash a plain text password with bcrypt."""
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def is_legacy_hash(hashed_password: str) -> bool:
    """True for hashes made by the old unsalted HMAC-SHA256 fallback (64 hex chars)."""
    return not hashed_password.startswith("$2")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored bcrypt (or legacy HMAC) hash."""
    if not hashed_password:
        return False
    if is_legacy_hash(hashed_password):
        legacy = hmac.new(_LEGACY_HMAC_SECRET.encode("utf-8"),
                          plain_password.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(legacy, hashed_password)
    try:
        return bcrypt.checkpw(_pw_bytes(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT access token."""
    to_encode = data.copy()
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": int(expire.timestamp()), "iat": int(now.timestamp())})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT access token. Returns None if invalid or expired."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def parse_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an 'Authorization: Bearer <token>' header."""
    if not authorization:
        return None
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]
