"""Local encryption shared by model and startup configuration."""

import ctypes
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretVault:
    def __init__(self, directory):
        self.path = Path(directory) / "model-key.bin"

    @staticmethod
    def _dpapi(data, decrypt=False):
        class Blob(ctypes.Structure):
            _fields_ = [
                ("size", ctypes.c_ulong),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        function = (
            ctypes.windll.crypt32.CryptUnprotectData
            if decrypt
            else ctypes.windll.crypt32.CryptProtectData
        )
        if not function(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        ):
            raise RuntimeError("无法访问本机模型密钥，请重新设置 API Key")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    def _cipher(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            protected = self._dpapi(key) if os.name == "nt" else key
            try:
                descriptor = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(protected)
        protected = self.path.read_bytes()
        key = self._dpapi(protected, decrypt=True) if os.name == "nt" else protected
        return Fernet(key)

    def encrypt(self, secret):
        return self._cipher().encrypt(secret.encode()).decode()

    def decrypt(self, secret):
        try:
            return self._cipher().decrypt(secret.encode()).decode()
        except (InvalidToken, ValueError, OSError):
            raise RuntimeError("无法解密模型密钥，请重新设置 API Key") from None
