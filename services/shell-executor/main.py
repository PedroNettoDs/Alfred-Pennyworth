from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import subprocess
import os

app = FastAPI()

TOKEN = os.getenv("SHELL_EXECUTOR_TOKEN", "token-secreto-alfred")

ALLOWED_PREFIXES = [
    "docker",
    "systemctl status",
    "df",
    "free",
    "uptime",
    "ip addr",
    "ss ",
    "ps aux",
    "ls ",
    "cat /proc",
    "nvidia-smi",
    "ollama",
    "ping",
    "curl",
    "tree",
    "WEBUI_TOKEN=",
    "python3",
]

def is_allowed(command: str) -> bool:
    cmd = command.strip()
    return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

class CommandRequest(BaseModel):
    command: str
    timeout: Optional[int] = 15

@app.post("/execute")
def execute(req: CommandRequest, authorization: Optional[str] = Header(None)):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not is_allowed(req.command):
        raise HTTPException(status_code=403, detail=f"Comando nao permitido: {req.command}")
    try:
        result = subprocess.run(
            req.command, shell=True, capture_output=True,
            text=True, timeout=req.timeout
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Timeout")

@app.get("/health")
def health():
    return {"status": "ok"}