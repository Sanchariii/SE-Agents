"""Generate the RS256 keypair the service uses to sign and validate its JWTs.

    python scripts/developer_tools/generate_jwt_keys.py

Writes app/config/private and app/config/public. Both are gitignored. In hosted
environments these are mounted from a Kubernetes secret instead (see helm/values.yaml).
"""

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CONFIG_DIR = Path(__file__).resolve().parents[2] / "app" / "config"


def main() -> int:
    private_path, public_path = CONFIG_DIR / "private", CONFIG_DIR / "public"
    if private_path.exists() or public_path.exists():
        answer = input(f"{private_path.parent} already has keys. Overwrite? [y/N] ")
        if answer.strip().lower() != "y":
            print("aborted; existing keys kept")
            return 1

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    public_path.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))

    print(f"wrote {private_path}\nwrote {public_path}")
    print("Both files are gitignored — never commit them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
