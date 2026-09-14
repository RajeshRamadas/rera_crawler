"""
Authentication & JWT Token Helper Module for RERA Web Portal
Handles password hashing, password verification, and JWT access token creation/decoding.
"""

import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

# Patch passlib + bcrypt 4.x compatibility quirk if needed
try:
    import bcrypt
    if not hasattr(bcrypt, "__about__"):
        bcrypt.__about__ = type("about", (), {"__version__": getattr(bcrypt, "__version__", "4.0.0")})()
except Exception:
    pass

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    HAS_PASSLIB = True
except Exception:
    pwd_context = None
    HAS_PASSLIB = False

try:
    from jose import JWTError, jwt
    HAS_JOSE = True
except Exception:
    HAS_JOSE = False

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "rera_secure_jwt_secret_key_2026_super_secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


def hash_password(password: str) -> str:
    """Hash a plain text password using bcrypt (or SHA256 fallback)."""
    if HAS_PASSLIB and pwd_context:
        try:
            return pwd_context.hash(password)
        except Exception:
            pass
    # Secure fallback hashing using SHA-256 + secret salt
    salt = SECRET_KEY.encode('utf-8')
    return hmac.new(salt, password.encode('utf-8'), hashlib.sha256).hexdigest()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against the stored hash."""
    if HAS_PASSLIB and pwd_context:
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception:
            pass
    # Fallback verification
    return hash_password(plain_password) == hashed_password


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token encoding user payload."""
    to_encode = data.copy()
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if expires_delta:
        expire_ts = now_ts + int(expires_delta.total_seconds())
    else:
        expire_ts = now_ts + (ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    
    to_encode.update({"exp": expire_ts, "iat": now_ts})
    
    if HAS_JOSE:
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt
    
    # Fallback lightweight token encoding
    import json
    import base64
    payload_bytes = json.dumps(to_encode).encode('utf-8')
    sig = hmac.new(SECRET_KEY.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
    token = f"{base64.urlsafe_b64encode(payload_bytes).decode('utf-8')}.{sig}"
    return token


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT access token."""
    if HAS_JOSE:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            return payload
        except Exception:
            pass
    
    # Fallback lightweight token decoding
    try:
        import json
        import base64
        parts = token.split('.')
        if len(parts) != 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0].encode('utf-8'))
        sig = hmac.new(SECRET_KEY.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, parts[1]):
            return json.loads(payload_bytes.decode('utf-8'))
    except Exception:
        return None
    return None
