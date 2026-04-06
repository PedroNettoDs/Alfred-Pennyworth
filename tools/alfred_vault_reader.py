"""
title: Alfred Vault Reader
description: Busca notas no vault do Pedro por termo ou palavra-chave. Acesso somente leitura.
author: Alfred Pennyworth
version: 2.0.0
"""

from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/pedro",
            description="Caminho do vault do Pedro dentro do container",
        )

    def __init__(self):
        self.valves = self.Valves()

    def search_vault(
        self, query: str, folder: Optional[str] = "", max_results: Optional[int] = 5
    ) -> str:
        """
        Pesquisa notas no vault do Pedro por palavras-chave.
        Use quando Pedro perguntar sobre seus próprios projetos, anotações ou decisões.

        :param query: Termos a buscar (case-insensitive)
        :param folder: Subpasta para restringir a busca (vazio = vault inteiro)
        :param max_results: Máximo de resultados (padrão: 5)
        :return: Notas relevantes com trechos
        """
        try:
            vault = Path(self.valves.vault_path)
            target = vault / folder if folder else vault

            if not target.exists():
                return f"Pasta '{folder}' não encontrada no vault."

            terms = [t.lower() for t in query.split() if t]
            if not terms:
                return "Nenhum termo de busca fornecido."

            files = sorted(target.rglob("*.md"))
            if not files:
                return "Vault vazio."

            results = []
            for f in files:
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    content_lower = content.lower()
                    name_lower = f.name.lower()
                    score = sum(content_lower.count(t) for t in terms)
                    score += sum(10 for t in terms if t in name_lower)
                    if score > 0:
                        preview = content[:300].replace("\n", " ").strip()
                        results.append((score, f, preview))
                except Exception:
                    continue

            results.sort(key=lambda x: x[0], reverse=True)
            top = results[:max_results]

            if not top:
                return f"Nenhuma nota encontrada para '{query}'."

            lines = []
            for score, f, preview in top:
                rel = f.relative_to(vault)
                lines.append(f"### {rel} (score: {score})\n{preview}...")

            return f"{len(top)} resultado(s) para '{query}':\n\n" + "\n\n".join(lines)

        except Exception as e:
            return f"[erro]: {str(e)}"

    def read_note(self, path: str) -> str:
        """
        Lê o conteúdo completo de uma nota do vault do Pedro.

        :param path: Caminho relativo da nota (ex: "projetos/meu-projeto.md")
        :return: Conteúdo completo da nota
        """
        try:
            vault = Path(self.valves.vault_path)
            filepath = vault / path
            if not filepath.exists():
                return f"Nota não encontrada: {path}"
            return filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return f"[erro]: {str(e)}"
