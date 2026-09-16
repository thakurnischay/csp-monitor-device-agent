"""Password hashing for the admin login — thin wrapper over Werkzeug's
implementation (already a Flask dependency, no extra package needed)."""
from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return check_password_hash(hashed, password)
    except Exception:
        return False
