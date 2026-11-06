#!/usr/bin/env python3
"""
Custo de CPU de verificar um token de API: bcrypt (como o Odysseus guarda os tokens) × SHA-256 + compare_digest.

Microbenchmark local, token gerado na hora; não toca em serviço nenhum.
    python3 -I docs/avaliacoes/scripts/auth_custo_verificacao.py     (precisa do pacote bcrypt)
"""
import hashlib
import hmac
import secrets
import time

import bcrypt

tok = secrets.token_urlsafe(32).encode()
h = bcrypt.hashpw(tok, bcrypt.gensalt())          # custo padrão (12)
n = 8
t = time.perf_counter()
for _ in range(n):
    bcrypt.checkpw(tok, h)
b = (time.perf_counter() - t) / n * 1000

esperado = hashlib.sha256(tok).digest()
t = time.perf_counter()
for _ in range(20000):
    hmac.compare_digest(hashlib.sha256(tok).digest(), esperado)
s = (time.perf_counter() - t) / 20000 * 1e6

print(f"bcrypt.checkpw custo 12: {b:.0f} ms/verificação | sha256+compare_digest: {s:.2f} us/verificação")
