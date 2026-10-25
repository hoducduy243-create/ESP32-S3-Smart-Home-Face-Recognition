
import os
import hmac
import hashlib
import secrets
from dotenv import load_dotenv

load_dotenv()


def hash_password(password: str) -> str:
    """Băm mật khẩu bằng PBKDF2-HMAC-SHA256."""
    salt = secrets.token_bytes(16)
    iterations = 600_000

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations
    )

    return f"{iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Kiểm tra mật khẩu mà không lưu mật khẩu gốc."""
    try:
        iterations_str, salt_hex, expected_hex = stored_hash.split("$")

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_str)
        )

        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError):
        return False


def check_admin_password(password: str) -> bool:
    stored_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    return bool(stored_hash) and verify_password(password, stored_hash)
