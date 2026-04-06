"""
title: Alfred Tech Briefing
description: Gera briefing de notícias tech com síntese IA e áudio TTS. Use quando Pedro pedir notícias, briefing ou novidades de tecnologia.
author: Alfred Pennyworth
version: 1.0.0
"""

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
            return f"[timeout]: briefing excedeu {timeout}s"
        except Exception as e:
            return f"[erro]: {e}"

    def gerar_briefing(self) -> str:
        """
        Gera um briefing completo de notícias de tecnologia.
        Coleta de SearXNG + RSS, sintetiza com IA, gera áudio TTS.
        Leva 2-4 minutos para completar.

        Use quando Pedro pedir: notícias, briefing, novidades tech,
        "o que está acontecendo em tech", "me atualiza".

        :return: Briefing formatado com logs de execução
        """
        start = time.time()
        logs = []
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando Tech Briefing...")

        cmd = "/home/netto/Alfred-Pennyworth/services/briefing/run.sh"
        output = self._exec(cmd, timeout=600)

        elapsed = time.time() - start
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Concluído em {elapsed:.0f}s")

        # Extrair logs relevantes
        briefing_file = ""
        if output:
            for line in output.strip().split("\n"):
                line = line.strip()
                if line.startswith("BRIEFING_FILE="):
                    briefing_file = line.split("=", 1)[1]
                elif line and not line.startswith("[briefing]"):
                    logs.append(f"  {line}")

        # Tentar ler o briefing gerado
        report_content = ""

        # Primeiro: tentar via path impresso pelo script
        if briefing_file:
            # Converter path do host para path do container
            container_path = briefing_file.replace(
                str(Path("/home/netto/Documentos/Obsidian - Alfred")),
                "/vaults/alfred"
            )
            try:
                p = Path(container_path)
                if p.exists():
                    raw = p.read_text(encoding="utf-8")
                    if raw.startswith("---"):
                        parts = raw.split("---", 2)
                        report_content = parts[2].strip() if len(parts) >= 3 else raw
                    else:
                        report_content = raw
            except Exception:
                pass

        # Fallback: buscar o mais recente na pasta briefings
        if not report_content:
            vault = Path(self.valves.vault_path)
            briefing_dir = vault / "briefings"
            if briefing_dir.exists():
                md_files = sorted(briefing_dir.glob("*.md"), reverse=True)
                if md_files:
                    try:
                        raw = md_files[0].read_text(encoding="utf-8")
                        if raw.startswith("---"):
                            parts = raw.split("---", 2)
                            report_content = parts[2].strip() if len(parts) >= 3 else raw
                        else:
                            report_content = raw
                        logs.append(f"  Lido: {md_files[0].name}")
                    except Exception as e:
                        logs.append(f"  Erro ao ler: {e}")

        log_block = "\n".join(logs)

        if report_content:
            return (
                f"**Tech Briefing gerado em {elapsed:.0f}s**\n\n"
                f"```\n{log_block}\n```\n\n---\n\n"
                f"{report_content}"
            )
        else:
            return (
                f"**Briefing executado mas sem conteúdo legível.**\n\n"
                f"```\n{log_block}\n```\n\n"
                f"Saída do script:\n```\n{output[:2000]}\n```"
            )

    def ver_briefings(self, quantidade: Optional[int] = 5) -> str:
        """
        Lista os briefings disponíveis no vault.
        Não gera um novo — apenas mostra os já existentes.

        :param quantidade: Quantos briefings listar (padrão: 5)
        :return: Lista dos briefings com títulos e datas
        """
        vault = Path(self.valves.vault_path)
        briefing_dir = vault / "briefings"

        if not briefing_dir.exists():
            return "Nenhum briefing encontrado. Use `gerar_briefing()` para criar o primeiro."

        md_files = sorted(briefing_dir.glob("*.md"), reverse=True)[:quantidade]

        if not md_files:
            return "Pasta de briefings vazia."

        lines = []
        for f in md_files:
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore")
                # Extrair título do frontmatter
                title = f.stem
                if raw.startswith("---"):
                    for line in raw.split("---", 2)[1].split("\n"):
                        if line.strip().startswith("title:"):
                            title = line.split(":", 1)[1].strip().strip('"')
                            break

                # Verificar se tem áudio
                mp3 = f.with_suffix(".mp3")
                audio_flag = " (com áudio)" if mp3.exists() else ""

                lines.append(f"- **{title}**{audio_flag}\n  Arquivo: `{f.name}`")
            except Exception:
                lines.append(f"- {f.name}")

        return f"{len(md_files)} briefings recentes:\n\n" + "\n".join(lines)

    def ler_briefing(self, arquivo: str) -> str:
        """
        Lê o conteúdo completo de um briefing específico.

        :param arquivo: Nome do arquivo (ex: "2026-04-06_0700_ia-revoluciona-saude.md")
        :return: Conteúdo do briefing
        """
        vault = Path(self.valves.vault_path)
        filepath = vault / "briefings" / arquivo

        if not filepath.exists():
            # Tentar sem extensão
            filepath = vault / "briefings" / f"{arquivo}.md"

        if not filepath.exists():
            return f"Briefing não encontrado: {arquivo}\nUse `ver_briefings()` para listar os disponíveis."

        try:
            raw = filepath.read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                return parts[2].strip() if len(parts) >= 3 else raw
            return raw
        except Exception as e:
            return f"Erro ao ler: {e}"
