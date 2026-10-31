"""
title: Alfred Tech Briefing
description: Gera briefing de notícias tech com síntese IA e áudio TTS. Use quando Pedro pedir notícias, briefing ou novidades de tecnologia.
author: Alfred Pennyworth
version: 1.2.0
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
            default="http://172.17.0.1:7071",
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
        alfred_dir_host: str = Field(
            default="/home/netto/Alfred-Pennyworth",
            description="Pasta do repositório do Alfred NO HOST (a do services/scripts/instalar.sh)",
        )
        vault_alfred_host: str = Field(
            default="/home/netto/Documentos/Obsidian - Alfred",
            description="VAULT_ALFRED do .env (caminho no host) — para achar o briefing dentro do container",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _exec(self, cmd: str, timeout: int = 600) -> tuple:
        """(saída, returncode). returncode None quando o executor não rodou o comando (HTTP ≠ 200, rede, timeout)."""
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
                return data.get("stdout", "") + data.get("stderr", ""), data.get("returncode")
            try:
                detalhe = resp.json().get("detail", "")
            except Exception:
                detalhe = ""
            return f"[erro {resp.status_code}]: {detalhe}", None
        except requests.exceptions.Timeout:
            return f"[timeout]: briefing excedeu {timeout}s", None
        except Exception as e:
            return f"[erro]: {e}", None

    @staticmethod
    def _corpo(path: Path) -> str:
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            return parts[2].strip() if len(parts) >= 3 else raw
        return raw

    def _no_container(self, briefing_file: str) -> Path:
        """BRIEFING_FILE (caminho no host) → caminho no container, pela Valve vault_path (antes "/vaults/alfred" fixo)."""
        host = str(Path(self.valves.vault_alfred_host)).rstrip("/")
        vault = Path(self.valves.vault_path)
        if briefing_file.startswith(host + "/"):
            return vault / briefing_file[len(host) + 1:]
        return vault / "briefings" / Path(briefing_file).name   # Valve vault_alfred_host desatualizada

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

        cmd = f"{self.valves.alfred_dir_host.rstrip('/')}/services/briefing/run.sh"
        output, returncode = self._exec(cmd, timeout=600)

        elapsed = time.time() - start
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Concluído em {elapsed:.0f}s")

        if returncode != 0:
            # falhou (Ollama fora, sem notícias, executor recusou...): nunca mostrar um briefing antigo como novo
            fim = "\n".join(output.strip().splitlines()[-25:])
            situacao = f"código de saída {returncode}" if returncode is not None else "o executor não rodou o script"
            return (
                f"**Falha ao gerar o briefing** ({situacao}, {elapsed:.0f}s). Nenhum briefing novo foi criado.\n\n"
                f"Fim do log:\n```\n{fim}\n```"
            )

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
            try:
                p = self._no_container(briefing_file)
                if p.exists():
                    report_content = self._corpo(p)
                    logs.append(f"  Lido: {p.name}")
            except Exception:
                pass

        # Fallback: o mais recente (por data de modificação) — só se foi gravado durante esta execução
        if not report_content:
            briefing_dir = Path(self.valves.vault_path) / "briefings"
            if briefing_dir.exists():
                md_files = sorted(briefing_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                if md_files and md_files[0].stat().st_mtime >= start - 5:
                    try:
                        report_content = self._corpo(md_files[0])
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

        # mais recentes por data de modificação (o nome antigo AAAA-MM-DD_<slug> não tinha hora)
        md_files = sorted(briefing_dir.glob("*.md"), key=lambda f: f.stat().st_mtime,
                          reverse=True)[:max(1, quantidade or 5)]

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
                audio_flag = " (com áudio)" if mp3.exists() and mp3.stat().st_size > 0 else ""

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
        pasta = (Path(self.valves.vault_path) / "briefings").resolve()
        filepath = (pasta / arquivo).resolve()

        if not filepath.exists():
            # Tentar sem extensão
            filepath = (pasta / f"{arquivo}.md").resolve()

        if not filepath.is_relative_to(pasta):
            return f"Caminho fora da pasta de briefings: {arquivo}"

        if not filepath.is_file():
            return f"Briefing não encontrado: {arquivo}\nUse `ver_briefings()` para listar os disponíveis."

        try:
            raw = filepath.read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                return parts[2].strip() if len(parts) >= 3 else raw
            return raw
        except Exception as e:
            return f"Erro ao ler: {e}"
