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

    def save_to_vault(
        self, title: str, content: str, folder: Optional[str] = "logs"
    ) -> str:
        """
        Salva uma nota no vault do Alfred.

        Use para registrar: resultados de pesquisas, decisões técnicas,
        resumos de conversas importantes, erros resolvidos e aprendizados.

        ORDEM OBRIGATÓRIA quando a tarefa envolve pesquisa + salvar:
        1. Execute a pesquisa web primeiro
        2. Processe e sintetize os resultados
        3. Somente então chame save_to_vault com o conteúdo completo

        NUNCA chame save_to_vault com placeholder ou conteúdo vazio.

        :param title: Título da nota (vira nome do arquivo)
        :param content: Conteúdo em markdown
        :param folder: Subpasta dentro do vault (padrão: logs). Use 'research' para pesquisas, 'decisions' para decisões técnicas.
        :return: Confirmação com o caminho salvo
        """
        try:
            vault = Path(self.valves.vault_path)
            target_dir = vault / folder
            target_dir.mkdir(parents=True, exist_ok=True)

            date_str = datetime.now().strftime("%Y-%m-%d")
            time_str = datetime.now().strftime("%H:%M")
            safe_title = "".join(
                c if c.isalnum() or c in " -_" else "" for c in title
            ).strip()
            filename = f"{date_str} {safe_title}.md"
            filepath = target_dir / filename

            frontmatter = f"""---
title: {title}
date: {date_str}
time: {time_str}
folder: {folder}
---

"""
            full_content = frontmatter + content
            filepath.write_text(full_content, encoding="utf-8")
            return f"Nota salva em: {folder}/{filename}"

        except Exception as e:
            return f"[erro ao salvar no vault]: {str(e)}"

    def list_vault(self, folder: Optional[str] = "") -> str:
        """
        Lista arquivos no vault do Alfred.

        Use para verificar o que já foi registrado antes de pesquisar algo.
        Evita duplicar pesquisas já feitas.

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

    def search_vault(
        self,
        query: str,
        folder: Optional[str] = "",
        max_results: Optional[int] = 5,
    ) -> str:
        """
        Pesquisa notas no vault do Alfred por palavras-chave.

        Use ANTES de pesquisar na web — evita duplicar conhecimento já registrado.
        Busca no título e no conteúdo de todas as notas .md.
        Notas com mais ocorrências dos termos e matches no título aparecem primeiro.

        :param query: Termos a buscar (case-insensitive, múltiplos separados por espaço)
        :param folder: Subpasta para restringir a busca (vazio = vault inteiro)
        :param max_results: Número máximo de resultados (padrão: 5)
        :return: Lista de notas relevantes com trechos do contexto encontrado
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

            candidates = []

            for md_file in files:
                try:
                    content = md_file.read_text(encoding="utf-8", errors="ignore")
                    content_lower = content.lower()

                    # Frequência no conteúdo + peso 3x no título (igual ao vault_reader)
                    content_score = sum(content_lower.count(t) for t in terms)
                    title_score = sum(md_file.stem.lower().count(t) for t in terms) * 3
                    total_score = content_score + title_score

                    if total_score == 0:
                        continue

                    # Quantos termos distintos aparecem (para o label)
                    matched_terms = [t for t in terms if t in content_lower]

                    # Trecho de contexto ao redor do primeiro termo encontrado
                    idx = content_lower.find(matched_terms[0])
                    start = max(0, idx - 80)
                    end = min(len(content), idx + 160)
                    snippet = content[start:end].strip().replace("\n", " ")

                    if snippet.startswith("---"):
                        snippet = content[end:end + 200].strip().replace("\n", " ")

                    candidates.append({
                        "path": str(md_file.relative_to(vault)),
                        "snippet": snippet,
                        "score": total_score,
                        "matched": len(matched_terms),
                    })

                except Exception:
                    continue

            if not candidates:
                return f"Nenhuma nota encontrada para: '{query}'"

            candidates.sort(key=lambda x: x["score"], reverse=True)
            results = candidates[:max_results]

            lines = [f"{len(candidates)} nota(s) encontrada(s) para '{query}'. Exibindo as {len(results)} mais relevantes:\n"]
            for i, r in enumerate(results, 1):
                label = f"[{r['matched']}/{len(terms)} termos]" if len(terms) > 1 else ""
                lines.append(f"**{i}. {r['path']}** {label}")
                lines.append(f"   ...{r['snippet']}...")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"[erro ao pesquisar no vault]: {str(e)}"