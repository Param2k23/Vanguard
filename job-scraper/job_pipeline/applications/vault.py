"""Passphrase-encrypted local vault. No plaintext fallback or persisted master key."""
import base64
import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from .storage import save_json


class VaultError(ValueError):
    pass


class CredentialVault:
    def __init__(self, path, passphrase):
        self.path = Path(path)
        if not isinstance(passphrase, str) or len(passphrase) < 12:
            raise VaultError('Vault requires a master passphrase of at least 12 characters')
        self._passphrase = passphrase.encode()

    @classmethod
    def from_environment(cls, root):
        return cls(Path(root) / 'data/credentials/vault.json', os.environ.get('JOB_PORTAL_VAULT_PASSPHRASE', ''))

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self.path.with_suffix('.lock').open('a') as lock:
            os.chmod(self.path.with_suffix('.lock'), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _cipher(self, salt):
        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
            key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(self._passphrase)
            return Fernet(base64.urlsafe_b64encode(key))
        except ImportError:
            raise VaultError('Install the cryptography dependency; plaintext storage is forbidden') from None

    def _read(self):
        try:
            envelope = json.loads(self.path.read_text())
            if envelope.get('version') != 1:
                raise VaultError('Unsupported vault version')
            cipher = self._cipher(base64.b64decode(envelope['salt'], validate=True))
            data = json.loads(cipher.decrypt(envelope['ciphertext'].encode()))
            if not isinstance(data, dict):
                raise VaultError('Malformed vault')
            return data, envelope['salt'], cipher
        except VaultError:
            raise
        except Exception:
            raise VaultError('Vault is locked, missing, corrupt or has an incorrect passphrase') from None

    def initialize(self):
        with self.locked():
            if self.path.exists():
                self._read()
                return
            salt = base64.b64encode(secrets.token_bytes(16)).decode()
            cipher = self._cipher(base64.b64decode(salt))
            save_json(self.path, dict(version=1, salt=salt, ciphertext=cipher.encrypt(b'{}').decode()))

    def get(self, reference):
        with self.locked():
            data, _, _ = self._read()
            return data.get(reference)

    def put(self, reference, value):
        with self.locked():
            data, salt, cipher = self._read()
            data[reference] = value
            save_json(self.path, dict(version=1, salt=salt, ciphertext=cipher.encrypt(json.dumps(data).encode()).decode()))
