"""
Alfred Shell Executor — FastAPI
Executa comandos no host sob whitelist restrita.
Token via variável de ambiente SHELL_EXECUTOR_TOKEN.
"""

import os
import subprocess

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Alfred Shell Executor", version="2.0.0")

TOKEN = os.getenv("SHELL_EXECUTOR_TOKEN", "")

ALLOWED_PREFIXES = [
    "docker",
    "df",
    "free",
    "uptime",
    "nvidia-smi",
    "ollama",
    "systemctl status",
    "systemctl is-active",
    "ps aux",
    "top -bn1",
    "cat /proc",
    "lsblk",
    "ip addr",
    "ss -tlnp",
    "uname",
    "lscpu",
    "sensors",
    "du -sh",
    "wc -l",
    "head",
    "tail",
    "grep",
    "find",
    "ls",
    "pwd",
    "whoami",
    "date",
    "curl http://localhost",
    "curl http://127.0.0.1",
]


class CommandRequest(BaseModel):
    command: str
    timeout: int = 15


def is_allowed(cmd: str) -> bool:
    cmd_stripped = cmd.strip()
    return any(cmd_stripped.startswith(prefix) for prefix in ALLOWED_PREFIXES)


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


@app.post("/execute")
def execute(
    req: CommandRequest,
    authorization: str = Header(default=""),
):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Token inválido")

    if not is_allowed(req.command):
        raise HTTPException(
            status_code=403,
            detail=f"Comando não permitido: '{req.command}'. Consulte ALLOWED_PREFIXES.",
        )

    try:
        result = subprocess.run(
            req.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Timeout de {req.timeout}s excedido")
