"""
title: Alfred Vault Reader
description: Busca notas no vault do Pedro por termo ou palavra-chave. Use para consultar o conhecimento pessoal do Pedro antes de responder sobre seus projetos, estudos ou anotações.
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
            description="Caminho do vault do Pedro dentro do container (read-only)",
        )
        max_results: int = Field(
            default=5, description="Número máximo de notas retornadas por busca"
        )
        max_chars_per_note: int = Field(
            default=800, description="Máximo de caracteres exibidos por nota"
        )

    def __init__(self):
        self.valves = self.Valves()

    def search_vault(self, query: str, folder: Optional[str] = "") -> str:
        """
        Busca notas no vault do Pedro por termo ou palavra-chave.

        Use ANTES de responder perguntas sobre projetos, estudos, anotações
        ou qualquer coisa que Pedro possa ter documentado no Obsidian.
        Não use para buscar informações gerais — apenas para consultar o
        conhecimento pessoal do Pedro.

        :param query: Termo de busca (palavra ou frase)
        :param folder: Subpasta para limitar a busca (vazio = vault inteiro)
        :return: Trechos das notas relevantes encontradas
        """
        try:
            vault = Path(self.valves.vault_path)
            target = vault / folder if folder else vault

            if not target.exists():
                return f"Vault não encontrado em: {target}"

            md_files = list(target.rglob("*.md"))
            if not md_files:
                return "Vault vazio ou sem arquivos .md."

            terms = query.lower().split()
            results = []

            for md_file in md_files:
                try:
                    content = md_file.read_text(encoding="utf-8", errors="ignore")
                    content_lower = content.lower()

                    content_score = sum(content_lower.count(t) for t in terms)
                    title_score = sum(md_file.stem.lower().count(t) for t in terms) * 3
                    total = content_score + title_score

                    if total > 0:
                        results.append((total, md_file, content))
                except Exception:
                    continue

            if not results:
                return f"Nenhuma nota encontrada para '{query}' no vault do Pedro."

            results.sort(key=lambda x: x[0], reverse=True)
            top = results[: self.valves.max_results]

            output = [
                f"Encontrei {len(results)} nota(s) para '{query}'. Exibindo as {len(top)} mais relevantes:\n"
            ]

            for score, md_file, content in top:
                rel_path = md_file.relative_to(vault)
                excerpt = content.strip()[: self.valves.max_chars_per_note]
                if len(content.strip()) > self.valves.max_chars_per_note:
                    excerpt += "..."
                output.append(f"### {md_file.stem}\n📄 {rel_path}\n\n{excerpt}\n\n---")

            return "\n".join(output)

        except Exception as e:
            return f"[erro ao ler vault]: {str(e)}"

    def read_note(self, path: str) -> str:
        """
        Lê o conteúdo completo de uma nota específica do vault do Pedro.

        Use quando search_vault encontrar uma nota relevante e você precisar
        do conteúdo completo para responder com mais detalhes.

        :param path: Caminho relativo da nota (ex: 'projetos/SubMax.md')
        :return: Conteúdo completo da nota
        """
        try:
            vault = Path(self.valves.vault_path)
            filepath = vault / path

            if not filepath.exists():
                return f"Nota não encontrada: {path}"

            if filepath.suffix != ".md":
                return "Apenas arquivos .md são suportados."

            content = filepath.read_text(encoding="utf-8", errors="ignore")
            return f"### {filepath.stem}\n📄 {path}\n\n{content}"

        except Exception as e:
            return f"[erro ao ler nota]: {str(e)}"

    def list_vault_folders(self) -> str:
        """
        Lista as pastas disponíveis no vault do Pedro.
        Use para entender a estrutura antes de buscar em uma pasta específica.

        :return: Estrutura de pastas do vault
        """
        try:
            vault = Path(self.valves.vault_path)
            if not vault.exists():
                return f"Vault não encontrado em: {vault}"

            folders = sorted(
                set(
                    str(p.parent.relative_to(vault))
                    for p in vault.rglob("*.md")
                    if str(p.parent.relative_to(vault)) != "."
                )
            )

            total = len(list(vault.rglob("*.md")))
            if not folders:
                return f"Vault sem subpastas. {total} arquivo(s) na raiz."

            lines = [f"Vault do Pedro — {total} notas em {len(folders)} pasta(s):\n"]
            for f in folders:
                count = len(list((vault / f).glob("*.md")))
                lines.append(f"  📁 {f}/ ({count} notas)")

            return "\n".join(lines)

        except Exception as e:
            return f"[erro ao listar vault]: {str(e)}"