"""
title: Alfred Daily Digest
description: Resumo do que aconteceu no projeto hoje — briefings gerados, pesquisas salvas, tarefas concluídas e memórias prestes a expirar. Sem LLM, só agrega fatos reais do vault.
author: Alfred Pennyworth
version: 1.0.0
"""

from pydantic import BaseModel, Field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
import re
import yaml


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _vault(self) -> Path:
        return Path(self.valves.vault_path)

    def _extract_title(self, md_path: Path) -> str:
        """Extrai título do frontmatter YAML ou usa o nome do arquivo."""
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end > 0:
                    frontmatter = text[3:end]
                    for line in frontmatter.splitlines():
                        m = re.match(r'^title:\s*["\']?(.+?)["\']?\s*$', line)
                        if m:
                            return m.group(1).strip()
        except Exception:
            pass
        return md_path.stem

    def _files_created_since(self, folder: Path, since: datetime) -> list[Path]:
        """Lista .md criados ou modificados desde `since`."""
        if not folder.exists():
            return []
        return sorted(
            [f for f in folder.glob("*.md") if datetime.fromtimestamp(f.stat().st_mtime) >= since],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

    def _load_tasks(self) -> list[dict]:
        f = self._vault() / "tasks.yml"
        if not f.exists():
            return []
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            return data.get("tasks", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def _load_memories(self) -> list[dict]:
        f = self._vault() / "memory.yml"
        if not f.exists():
            return []
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            return data.get("memories", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def daily_digest(self, days_back: Optional[int] = 1) -> str:
        """
        Resumo do que Alfred fez e o que está pendente.

        Use quando Pedro perguntar "o que aconteceu hoje?", "me faz um resumo do dia",
        "o que você fez?", "resumo da semana" ou variações.

        Agrega sem LLM: lista arquivos criados no vault, tarefas e memórias próximas de expirar.

        :param days_back: Quantos dias atrás considerar (1 = hoje, 7 = semana)
        :return: Resumo formatado pronto para apresentar ao Pedro
        """
        since = datetime.now() - timedelta(days=max(1, days_back or 1))
        period = "hoje" if days_back <= 1 else f"nos últimos {days_back} dias"
        sections = []

        # ── Briefings gerados ──
        briefings = self._files_created_since(self._vault() / "briefings", since)
        if briefings:
            items = [f"  • {self._extract_title(f)}" for f in briefings[:5]]
            sections.append(f"**Briefings gerados {period} ({len(briefings)}):**\n" + "\n".join(items))

        # ── Pesquisas salvas ──
        research_dir = self._vault() / "research"
        research_items = []
        if research_dir.exists():
            for topic_dir in sorted(research_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
                synth = topic_dir / "synthesis.md"
                if synth.exists() and datetime.fromtimestamp(synth.stat().st_mtime) >= since:
                    research_items.append(f"  • {self._extract_title(synth)}")
        if research_items:
            sections.append(f"**Pesquisas realizadas {period} ({len(research_items)}):**\n" + "\n".join(research_items[:5]))

        # ── Notas salvas (decisions, logs) ──
        for folder_name in ("decisions", "logs"):
            files = self._files_created_since(self._vault() / folder_name, since)
            if files:
                items = [f"  • {self._extract_title(f)}" for f in files[:3]]
                sections.append(f"**{folder_name.capitalize()} ({len(files)}):**\n" + "\n".join(items))

        # ── Tarefas ──
        tasks = self._load_tasks()
        today_str = date.today().isoformat()
        since_str = since.date().isoformat()

        concluidas = [
            t for t in tasks
            if t.get("status") == "done"
            and t.get("completed_at", "")[:10] >= since_str
        ]
        if concluidas:
            items = [f"  ✓ #{t['id']} {t['title']}" for t in concluidas[:5]]
            sections.append(f"**Tarefas concluídas {period} ({len(concluidas)}):**\n" + "\n".join(items))

        abertas = [t for t in tasks if t.get("status") == "open"]
        vencidas = [t for t in abertas if t.get("due_date") and t["due_date"] < today_str]
        proximas = [
            t for t in abertas
            if t.get("due_date")
            and today_str <= t["due_date"] <= (date.today() + timedelta(days=7)).isoformat()
        ]

        if vencidas:
            items = [f"  ⚠️ #{t['id']} {t['title']} (venceu {t['due_date']})" for t in vencidas[:3]]
            sections.append(f"**Tarefas vencidas ({len(vencidas)}):**\n" + "\n".join(items))

        if proximas:
            items = [f"  → #{t['id']} {t['title']} (vence {t['due_date']})" for t in proximas[:3]]
            sections.append(f"**Próximos vencimentos (7 dias):**\n" + "\n".join(items))

        # ── Memórias expirando em breve ──
        memories = self._load_memories()
        expirando = []
        limite = (date.today() + timedelta(days=7)).isoformat()
        for m in memories:
            exp = m.get("expires_at")
            if exp and today_str <= exp[:10] <= limite:
                expirando.append(f"  • {m['key']}: {m['value']} (expira {exp[:10]})")
        if expirando:
            sections.append(f"**Memórias expirando em 7 dias:**\n" + "\n".join(expirando[:3]))

        if not sections:
            return f"Nada registrado {period}. Vault sem atividade recente."

        header = f"## Resumo — {date.today().strftime('%d/%m/%Y')}\n"
        return header + "\n\n".join(sections)
