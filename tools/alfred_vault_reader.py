"""
title: Alfred Vault Reader
description: Busca notas no vault do Pedro por termo ou palavra-chave. Acesso somente leitura.
author: Alfred Pennyworth
version: 2.1.0
"""

import re
import sqlite3

from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/pedro",
            description="Caminho do vault do Pedro dentro do container",
        )

        db_path: str = Field(
            default="/vaults/alfred/.alfred/alfred.db",
            description="Índice SQLite FTS5 gerado pelo Alfred (inbox-assistant.py indexar); ausente = busca por substring",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _buscar_fts(self, query: str, cofre: str, folder: Optional[str], max_results: int) -> Optional[list]:
        """
        Busca no índice SQLite FTS5 (alfred.db — gerado no host por `inbox-assistant.py indexar`): sem acento,
        com ranking bm25 e título pesando mais. Devolve [] se o índice não achou nada e None se não há
        índice ou deu erro; nos dois casos a busca por substring abaixo assume.
        """
        db = Path(self.valves.db_path)
        termos = re.findall(r"\w+", query or "", flags=re.UNICODE)
        if not termos or not db.exists():
            return None
        sql = ("SELECT caminho, snippet(notas, 1, '', '', ' … ', 24), bm25(notas, 8.0, 1.0) "
               "FROM notas WHERE notas MATCH ? AND cofre = ?")
        extra = [cofre]
        if folder:
            sql += " AND caminho LIKE ? ESCAPE '\\'"
            extra.append(folder.strip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        sql += " ORDER BY 3 LIMIT ?"
        extra.append(int(max_results or 5))
        partes = [f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in termos]
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                for juncao in (" ", " OR "):
                    linhas = con.execute(sql, (juncao.join(partes), *extra)).fetchall()
                    if linhas or len(partes) == 1:
                        break
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return [f"### {c} (score: {-s:.1f})\n{(t or '').replace(chr(10), ' ').strip()}" for c, t, s in linhas]

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

            achados = self._buscar_fts(query, "pedro", folder, max_results)
            if achados:
                return f"{len(achados)} resultado(s) para '{query}':\n\n" + "\n\n".join(achados)

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
