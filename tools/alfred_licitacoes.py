"""
title: Alfred Licitações
description: Escaneia portais de licitação do Brasil, analisa relevância para a AttanoTech e retorna relatório. Use quando Pedro pedir para buscar licitações.
author: Alfred Pennyworth
version: 1.0.0
"""

import os
import time
import requests
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional


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
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred dentro do container",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _exec(self, cmd: str, timeout: int = 600) -> str:
        try:
            resp = requests.post(
                f"{self.valves.executor_url}/execute",
                headers={
                    "Authorization": f"Bearer {self.valves.token}",
                    "Content-Type": "application/json",
                },
                json={"command": cmd, "timeout": timeout},
                timeout=timeout + 5,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("stdout", "") + data.get("stderr", "")
            return f"[erro {resp.status_code}]: {resp.json().get('detail', '')}"
        except requests.exceptions.Timeout:
            return f"[timeout]: scanner excedeu {timeout}s"
        except Exception as e:
            return f"[erro]: {e}"

    def buscar_licitacoes(self, filtro_uf: Optional[str] = "") -> str:
        """
        Escaneia todos os portais de licitação do Brasil e retorna um relatório
        com as oportunidades mais relevantes para a AttanoTech.

        Busca em 36 portais (27 estaduais + 3 federais + 6 privados), analisa
        com IA e classifica por score de relevância.

        Use quando Pedro pedir para buscar, pesquisar ou verificar licitações.

        :param filtro_uf: Filtrar por UF específica (ex: "SP", "MG"). Vazio = nacional.
        :return: Relatório completo com oportunidades classificadas por relevância
        """
        start = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        logs = []

        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando scanner de licitações...")

        if filtro_uf:
            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Filtro UF: {filtro_uf}")

        # Rodar o scanner via Shell Executor
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Executando scanner (pode levar 3-5 minutos)...")

        cmd = "/home/netto/Alfred-Pennyworth/services/licitacoes/run.sh"
        output = self._exec(cmd, timeout=600)

        elapsed = time.time() - start
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Scanner finalizado em {elapsed:.0f}s")

        # Extrair logs do scanner
        if output:
            for line in output.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("[licitacoes]"):
                    logs.append(f"  {line}")

        # Ler o relatório gerado
        vault = Path(self.valves.vault_path)
        report_path = vault / "licitacoes" / f"{today}.md"

        report_content = ""
        if report_path.exists():
            try:
                raw = report_path.read_text(encoding="utf-8")
                # Remover frontmatter
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        report_content = parts[2].strip()
                    else:
                        report_content = raw
                else:
                    report_content = raw

                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Relatório lido: {len(report_content)} chars")
            except Exception as e:
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Erro ao ler relatório: {e}")
        else:
            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Relatório não encontrado em {report_path}")

        # Filtrar por UF se solicitado
        if filtro_uf and report_content:
            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Aplicando filtro UF={filtro_uf}")

        # Montar resposta
        log_block = "\n".join(logs)
        separator = "\n\n---\n\n"

        if report_content:
            return (
                f"**Scanner de licitações concluído em {elapsed:.0f}s**\n\n"
                f"```\n{log_block}\n```"
                f"{separator}"
                f"{report_content}"
            )
        else:
            return (
                f"**Scanner executado, mas sem resultados hoje.**\n\n"
                f"```\n{log_block}\n```\n\n"
                f"Saída do scanner:\n```\n{output[:2000]}\n```"
            )

    def ver_licitacoes(self, data: Optional[str] = "") -> str:
        """
        Mostra o relatório de licitações de um dia específico (já gerado anteriormente).
        Não executa uma nova busca — apenas lê o relatório salvo no vault.

        :param data: Data no formato YYYY-MM-DD. Vazio = relatório de hoje.
        :return: Conteúdo do relatório
        """
        if not data:
            data = datetime.now().strftime("%Y-%m-%d")

        vault = Path(self.valves.vault_path)
        report_path = vault / "licitacoes" / f"{data}.md"

        if not report_path.exists():
            # Listar relatórios disponíveis
            lic_dir = vault / "licitacoes"
            if lic_dir.exists():
                files = sorted(lic_dir.glob("*.md"), reverse=True)
                if files:
                    disponiveis = "\n".join(f"- {f.stem}" for f in files[:10])
                    return f"Relatório de {data} não encontrado.\n\nRelatórios disponíveis:\n{disponiveis}"
            return f"Nenhum relatório encontrado. Use `buscar_licitacoes()` para executar o scanner."

        try:
            raw = report_path.read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return raw
        except Exception as e:
            return f"Erro ao ler relatório: {e}"
