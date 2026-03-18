"""
title: Alfred Shell Executor
description: Executa comandos reais no sistema do Pedro via Shell Executor local. Use para docker, disco, memória, GPU, rede e processos.
author: Alfred Pennyworth
version: 1.0.0
"""

import requests
from pydantic import BaseModel, Field
from typing import Optional


class Tools:
    class Valves(BaseModel):
        executor_url: str = Field(
            default="http://172.17.0.1:7070",
            description="URL do Shell Executor no host",
        )
        token: str = Field(
            default="4695e1b8e210d1f7e4eefc9d3fb4d91488e950fd2fb0334845dbde0165416879",
            description="Token de autenticação Bearer",
        )

    def __init__(self):
        self.valves = self.Valves()

    def execute_command(self, command: str, timeout: Optional[int] = 15) -> str:
        """
        Executa um comando shell no sistema do Pedro e retorna o output real.

        Quando usar: sempre que Pedro perguntar sobre estado do sistema.
        Exemplos: "docker ps", "df -h", "free -h", "nvidia-smi", "systemctl status alfred-executor", "uptime"

        REGRA: nunca invente ou suponha o resultado. Execute e retorne o output real.

        :param command: Comando a executar. Ex: "docker ps", "nvidia-smi", "df -h"
        :param timeout: Timeout em segundos (padrão 15)
        :return: Output real do comando
        """
        try:
            resp = requests.post(
                f"{self.valves.executor_url}/execute",
                headers={
                    "Authorization": f"Bearer {self.valves.token}",
                    "Content-Type": "application/json",
                },
                json={"command": command, "timeout": timeout},
                timeout=timeout + 2,
            )
            if resp.status_code == 200:
                data = resp.json()
                stdout = data.get("stdout", "").strip()
                stderr = data.get("stderr", "").strip()
                returncode = data.get("returncode", 0)

                if stdout:
                    return stdout
                if stderr:
                    return f"[stderr rc={returncode}]: {stderr}"
                return f"[comando executado, sem output, rc={returncode}]"

            elif resp.status_code == 403:
                return f"[bloqueado]: '{command}' não está na whitelist do executor. Adicione ao ALLOWED_PREFIXES no main.py."
            elif resp.status_code == 401:
                return "[erro]: token inválido. Verifique o campo token nas Valves da tool."
            else:
                detail = resp.json().get("detail", "erro desconhecido")
                return f"[erro {resp.status_code}]: {detail}"

        except requests.exceptions.ConnectionError:
            return "[erro]: Shell Executor offline. Verifique: systemctl status alfred-executor"
        except requests.exceptions.Timeout:
            return f"[erro]: comando excedeu {timeout}s de timeout."
        except Exception as e:
            return f"[erro inesperado]: {str(e)}"
