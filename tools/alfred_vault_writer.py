"""
title: Alfred Vault Writer
description: Permite ao Alfred salvar pesquisas, logs e aprendizados no seu próprio vault do Obsidian.
author: Alfred Pennyworth
version: 2.1.0
"""

import re
import sqlite3

from pydantic import BaseModel, Field
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml


def _no_vault(vault: Path, relativo: Optional[str]) -> Optional[Path]:
    """Resolve `relativo` dentro do vault. None se sair dele (caminho absoluto, "..", symlink para fora) —
    o container do Open WebUI roda como root: sem isso a tool lia/gravava qualquer arquivo."""
    base = vault.resolve()
    alvo = (base / (relativo or "")).resolve()
    return alvo if alvo.is_relative_to(base) else None


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred dentro do container",
        )

        db_path: str = Field(
            default="/vaults/alfred/.alfred/alfred.db",
            description="Índice SQLite FTS5 gerado pelo Alfred (inbox-assistant.py indexar); ausente = busca por substring",
        )

    def __init__(self):
        self.valves = self.Valves()

    def save_to_vault(self, title: str, content: str, folder: Optional[str] = "logs") -> str:
        """
        Salva uma nota no vault do Alfred.

        Use para registrar: resultados de pesquisas, decisões técnicas,
        resumos de conversas importantes, erros resolvidos e aprendizados.

        NUNCA chame save_to_vault com placeholder ou conteúdo vazio.

        :param title: Título da nota (vira nome do arquivo)
        :param content: Conteúdo em markdown
        :param folder: Subpasta (padrão: logs). Use 'research' ou 'decisions'.
        :return: Confirmação com o caminho salvo
        """
        try:
            vault = Path(self.valves.vault_path).resolve()
            target_dir = _no_vault(vault, folder)
            if target_dir is None:
                return f"[erro ao salvar no vault]: pasta '{folder}' fora do vault — use um caminho relativo ao vault."
            target_dir.mkdir(parents=True, exist_ok=True)

            agora = datetime.now()
            date_str = agora.strftime("%Y-%m-%d")
            time_str = agora.strftime("%H:%M")
            safe_title = " ".join("".join(c if c.isalnum() or c in " -_" else "" for c in (title or "")).split())
            if not safe_title:
                safe_title = agora.strftime("nota-%H%M%S")   # título só de símbolos: antes virava "AAAA-MM-DD .md"
            filename = f"{date_str} {safe_title}.md"
            n = 2
            while (target_dir / filename).exists():           # mesmo título no mesmo dia: não sobrescreve
                filename = f"{date_str} {safe_title} ({n}).md"
                n += 1
            filepath = target_dir / filename

            # yaml.safe_dump: título com ":" ("Decisão: usar QUIC") gerava frontmatter inválido
            meta = {"title": title, "date": date_str, "time": time_str, "folder": folder}
            frontmatter = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n"
            with open(filepath, "x", encoding="utf-8") as fh:
                fh.write(frontmatter + content)
            return f"Nota salva em: {filepath.relative_to(vault)}"

        except Exception as e:
            return f"[erro ao salvar no vault]: {str(e)}"

    def list_vault(self, folder: Optional[str] = "") -> str:
        """
        Lista arquivos no vault do Alfred.

        :param folder: Subpasta para listar (vazio = raiz do vault)
        :return: Lista de arquivos encontrados
        """
        try:
            vault = Path(self.valves.vault_path).resolve()
            target = _no_vault(vault, folder)
            if target is None:
                return f"Pasta '{folder}' fora do vault — use um caminho relativo ao vault."

            if not target.exists():
                return f"Pasta '{folder}' não encontrada no vault."

            files = sorted(target.rglob("*.md"))
            if not files:
                return "Vault vazio — nenhuma nota encontrada ainda."

            lines = [f"- {f.relative_to(vault)}" for f in files]
            return f"{len(files)} notas no vault:\n" + "\n".join(lines)

        except Exception as e:
            return f"[erro ao listar vault]: {str(e)}"

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

    def search_vault(self, query: str, folder: Optional[str] = "", max_results: Optional[int] = 5) -> str:
        """
        Pesquisa notas no vault do Alfred por palavras-chave.
        Use ANTES de pesquisar na web — evita duplicar conhecimento já registrado.

        :param query: Termos a buscar (case-insensitive)
        :param folder: Subpasta para restringir a busca (vazio = vault inteiro)
        :param max_results: Máximo de resultados (padrão: 5)
        :return: Notas relevantes com trechos
        """
        try:
            vault = Path(self.valves.vault_path).resolve()
            target = _no_vault(vault, folder)
            if target is None:
                return f"Pasta '{folder}' fora do vault — use um caminho relativo ao vault."

            if not target.exists():
                return f"Pasta '{folder}' não encontrada no vault."

            terms = [t.lower() for t in query.split() if t]
            if not terms:
                return "Nenhum termo de busca fornecido."

            achados = self._buscar_fts(query, "alfred", folder, max_results)
            if achados:
                return f"{len(achados)} resultado(s) para '{query}':\n\n" + "\n\n".join(achados)

            files = sorted(target.rglob("*.md"))
            if not files:
                return "Vault vazio — nenhuma nota encontrada."

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
            return f"[erro ao pesquisar vault]: {str(e)}"
