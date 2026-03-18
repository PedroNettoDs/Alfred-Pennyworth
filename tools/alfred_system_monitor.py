"""
title: Alfred System Monitor
description: Gera relatório completo de recursos do sistema — CPU, RAM, disco e GPU — em uma única chamada. Use quando Pedro perguntar sobre o estado geral da máquina.
author: Alfred Pennyworth
version: 2.0.0
"""

from pydantic import BaseModel, Field
from typing import Optional
import requests


class Tools:
    class Valves(BaseModel):
        executor_url: str = Field(
            default="http://172.17.0.1:7070",
            description="URL do Shell Executor no host",
        )
        token: str = Field(
            default="4695e1b8e210d1f7e4eefc9d3fb4d91488e950fd2fb0334845dbde0165416879",
            description="Token Bearer do Shell Executor",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _run(self, command: str, timeout: int = 10) -> str:
        """Executa comando via Shell Executor e retorna o output."""
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
                return (
                    data.get("stdout", "").strip()
                    or data.get("stderr", "").strip()
                    or "[sem output]"
                )
            elif resp.status_code == 403:
                return f"[bloqueado]: '{command}' não está na whitelist. Adicione ao ALLOWED_PREFIXES no main.py."
            elif resp.status_code == 401:
                return "[erro]: token inválido. Atualize o campo token nas Valves desta tool E na alfred_shell_executor."
            return f"[erro {resp.status_code}]"
        except requests.exceptions.ConnectionError:
            return "[Shell Executor offline — verifique: systemctl status alfred-executor]"
        except Exception as e:
            return f"[erro]: {str(e)}"

    def system_report(self) -> str:
        """
        Gera relatório completo de recursos do sistema em uma única chamada.
        Coleta CPU, RAM, disco, GPU e containers Docker ativos.

        Use quando Pedro perguntar sobre o estado geral da máquina,
        performance, uso de recursos ou saúde do sistema.

        :return: Relatório formatado com todos os recursos do sistema
        """
        sections = []

        uptime = self._run("uptime")
        sections.append(f"UPTIME\n{uptime}")

        ram = self._run("free -h")
        sections.append(f"MEMÓRIA RAM\n{ram}")

        disk = self._run("df -h /mnt/SSD /")
        sections.append(f"DISCO\n{disk}")

        gpu = self._run(
            "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu "
            "--format=csv,noheader"
        )
        sections.append(f"GPU\n{gpu}")

        containers = self._run(
            'docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"'
        )
        sections.append(f"CONTAINERS DOCKER\n{containers}")

        return "\n\n".join(f"{'─'*40}\n{s}" for s in sections)

    def gpu_status(self) -> str:
        """
        Retorna apenas o status detalhado da GPU — VRAM, temperatura e utilização.
        Use quando a pergunta for especificamente sobre a GPU ou VRAM.

        :return: Status detalhado da GPU
        """
        return self._run(
            "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,"
            "temperature.gpu,power.draw --format=csv,noheader"
        )

    def disk_status(self, path: Optional[str] = "/mnt/SSD /") -> str:
        """
        Retorna uso de disco. Use quando a pergunta for especificamente sobre espaço em disco.

        :param path: Caminho específico para verificar (padrão: SSD e raiz)
        :return: Uso de disco
        """
        return self._run(f"df -h {path}")