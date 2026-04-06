"""
title: Alfred Vault Writer
description: Permite ao Alfred salvar pesquisas, logs e aprendizados no seu próprio vault do Obsidian.
author: Alfred Pennyworth
version: 2.0.0
"""

from pydantic import BaseModel, Field
from datetime import datetime
from pathlib import Path
from typing import Optional


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred dentro do container",
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
            vault = Path(self.valves.vault_path)
            target_dir = vault / folder
            target_dir.mkdir(parents=True, exist_ok=True)

            date_str = datetime.now().strftime("%Y-%m-%d")
            time_str = datetime.now().strftime("%H:%M")
            safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
            filename = f"{date_str} {safe_title}.md"
            filepath = target_dir / filename

            frontmatter = f"---\ntitle: {title}\ndate: {date_str}\ntime: {time_str}\nfolder: {folder}\n---\n\n"
            filepath.write_text(frontmatter + content, encoding="utf-8")
            return f"Nota salva em: {folder}/{filename}"

        except Exception as e:
            return f"[erro ao salvar no vault]: {str(e)}"

    def list_vault(self, folder: Optional[str] = "") -> str:
        """
        Lista arquivos no vault do Alfred.

        :param folder: Subpasta para listar (vazio = raiz do vault)
        :return: Lista de arquivos encontrados
        """
        try:
            vault = Path(self.valves.vault_path)
            target = vault / folder if folder else vault

            if not target.exists():
                return f"Pasta '{folder}' não encontrada no vault."

            files = sorted(target.rglob("*.md"))
            if not files:
                return "Vault vazio — nenhuma nota encontrada ainda."

            lines = [f"- {f.relative_to(vault)}" for f in files]
            return f"{len(files)} notas no vault:\n" + "\n".join(lines)

        except Exception as e:
            return f"[erro ao listar vault]: {str(e)}"

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
            vault = Path(self.valves.vault_path)
            target = vault / folder if folder else vault

            if not target.exists():
                return f"Pasta '{folder}' não encontrada no vault."

            terms = [t.lower() for t in query.split() if t]
            if not terms:
                return "Nenhum termo de busca fornecido."

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
