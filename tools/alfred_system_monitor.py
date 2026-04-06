"""
title: Alfred System Monitor
description: Gera relatório completo de recursos do sistema — CPU, RAM, disco e GPU — em uma única chamada.
author: Alfred Pennyworth
version: 2.0.0
"""

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        executor_url: str = Field(
            default="http://172.17.0.1:7070",
            description="URL do Shell Executor no host",
        )
        token: str = Field(
            default="",
            description="Token Bearer — configure nas Valves após importar",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _run(self, cmd: str) -> str:
        try:
            resp = requests.post(
                f"{self.valves.executor_url}/execute",
                headers={"Authorization": f"Bearer {self.valves.token}",
                         "Content-Type": "application/json"},
                json={"command": cmd, "timeout": 10},
                timeout=12,
            )
            if resp.status_code == 200:
                return resp.json().get("stdout", "").strip() or resp.json().get("stderr", "").strip()
            return f"[erro {resp.status_code}]"
        except Exception as e:
            return f"[erro]: {e}"

    def system_report(self) -> str:
        """
        Gera relatório completo de recursos do sistema em uma única chamada.
        Coleta CPU, RAM, disco, GPU e containers Docker ativos.

        Use quando Pedro perguntar sobre o estado geral da máquina,
        performance, uso de recursos ou saúde do sistema.

        :return: Relatório formatado do sistema
        """
        sections = []

        cpu = self._run("lscpu | grep -E 'Model name|CPU\\(s\\)|MHz' | head -5")
        load = self._run("uptime")
        sections.append(f"## CPU\n{cpu}\n{load}")

        ram = self._run("free -h")
        sections.append(f"## RAM\n{ram}")

        disk = self._run("df -h / /home")
        sections.append(f"## Disco\n{disk}")

        gpu = self._run("nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader")
        sections.append(f"## GPU\n{gpu}")

        containers = self._run("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null")
        sections.append(f"## Docker\n{containers}")

        return "\n\n".join(sections)

    def disk_status(self, path: str = "/ /home") -> str:
        """
        Retorna uso de disco.
        Use quando a pergunta for especificamente sobre espaço em disco.

        :param path: Caminho específico para verificar (padrão: / e /home)
        :return: Uso de disco
        """
        return self._run(f"df -h {path}")

    def gpu_status(self) -> str:
        """
        Retorna apenas o status detalhado da GPU — VRAM, temperatura e utilização.
        Use quando a pergunta for especificamente sobre a GPU ou VRAM.

        :return: Status da GPU
        """
        return self._run("nvidia-smi")
