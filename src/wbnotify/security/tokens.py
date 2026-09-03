"""Шифрование WB-токенов магазинов перед хранением в БД (cryptography.Fernet)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["encrypt_token", "decrypt_token", "InvalidToken", "generate_key"]


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_token(raw_token: str, key: str) -> bytes:
    return Fernet(key.encode()).encrypt(raw_token.encode())


def decrypt_token(ciphertext: bytes, key: str) -> str:
    return Fernet(key.encode()).decrypt(ciphertext).decode()
