"""
title: Alfred Tasks
description: Gerencia tarefas e lembretes do Pedro. Adiciona, lista, completa e verifica prazos vencidos. Armazena em VAULT_ALFRED/tasks.yml.
author: Alfred Pennyworth
version: 1.0.0
"""

from pydantic import BaseModel, Field
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import yaml


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _tasks_file(self) -> Path:
        return Path(self.valves.vault_path) / "tasks.yml"

    def _load(self) -> dict:
        f = self._tasks_file()
        if not f.exists():
            return {"tasks": []}
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "tasks" in data else {"tasks": []}
        except Exception:
            return {"tasks": []}

    def _save(self, data: dict):
        f = self._tasks_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _next_id(self, tasks: list) -> int:
        return max((t.get("id", 0) for t in tasks), default=0) + 1

    def add_task(
        self,
        title: str,
        due_date: Optional[str] = None,
        tags: Optional[str] = "",
        priority: Optional[str] = "normal",
    ) -> str:
        """
        Adiciona uma nova tarefa.

        Use quando Pedro mencionar algo que precisa ser feito, um prazo ou compromisso.
        Exemplos: "preciso estudar para a prova", "tenho que entregar o projeto até sexta".

        :param title: Descrição clara da tarefa
        :param due_date: Data de vencimento no formato YYYY-MM-DD (opcional)
        :param tags: Tags separadas por vírgula, ex: "fatec,estudo" (opcional)
        :param priority: "low", "normal" ou "high" (padrão: "normal")
        :return: Confirmação com ID da tarefa criada
        """
        data = self._load()
        task_id = self._next_id(data["tasks"])
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

        task = {
            "id": task_id,
            "title": title,
            "status": "open",
            "priority": priority or "normal",
            "tags": tag_list,
            "due_date": due_date or None,
            "created_at": datetime.now().isoformat(timespec="minutes"),
            "completed_at": None,
        }

        data["tasks"].append(task)
        self._save(data)

        due_str = f" (vence em {due_date})" if due_date else ""
        return f"Tarefa #{task_id} criada: \"{title}\"{due_str}"

    def list_tasks(self, filter: Optional[str] = "open") -> str:
        """
        Lista tarefas. Por padrão mostra apenas as abertas.

        Use ao iniciar conversa sobre o dia, semana ou quando Pedro perguntar "o que tenho pra fazer".

        :param filter: "open" (abertas), "done" (concluídas) ou "all" (todas)
        :return: Lista formatada de tarefas
        """
        data = self._load()
        tasks = data["tasks"]

        if filter == "open":
            tasks = [t for t in tasks if t.get("status") == "open"]
        elif filter == "done":
            tasks = [t for t in tasks if t.get("status") == "done"]

        if not tasks:
            return "Nenhuma tarefa encontrada." if filter == "all" else "Nenhuma tarefa aberta."

        today = date.today().isoformat()
        lines = []
        for t in sorted(tasks, key=lambda x: (x.get("due_date") or "9999", x["id"])):
            status_icon = "✓" if t.get("status") == "done" else "○"
            priority_icon = {"high": "!", "low": "↓"}.get(t.get("priority", ""), " ")
            due = t.get("due_date")
            due_str = ""
            if due:
                vencida = " ⚠️ VENCIDA" if due < today and t.get("status") == "open" else ""
                due_str = f" · vence {due}{vencida}"
            tags_str = f" [{', '.join(t['tags'])}]" if t.get("tags") else ""
            lines.append(f"{status_icon} [{priority_icon}] #{t['id']} {t['title']}{due_str}{tags_str}")

        header = {"open": "Tarefas abertas", "done": "Tarefas concluídas", "all": "Todas as tarefas"}.get(filter, "Tarefas")
        return f"**{header} ({len(lines)}):**\n" + "\n".join(lines)

    def complete_task(self, task_id: int) -> str:
        """
        Marca uma tarefa como concluída.

        Use quando Pedro disser que terminou algo ou pediu para marcar uma tarefa como feita.

        :param task_id: ID numérico da tarefa (obtido com list_tasks)
        :return: Confirmação
        """
        data = self._load()
        for task in data["tasks"]:
            if task.get("id") == task_id:
                if task.get("status") == "done":
                    return f"Tarefa #{task_id} já estava concluída."
                task["status"] = "done"
                task["completed_at"] = datetime.now().isoformat(timespec="minutes")
                self._save(data)
                return f"Tarefa #{task_id} marcada como concluída: \"{task['title']}\""
        return f"Tarefa #{task_id} não encontrada."

    def list_overdue(self) -> str:
        """
        Lista tarefas vencidas (due_date anterior a hoje e ainda abertas).

        Use automaticamente ao iniciar conversa sobre agenda, dia ou semana.
        Se retornar tarefas, informe Pedro antes de qualquer outra coisa.

        :return: Lista de tarefas vencidas ou mensagem de que não há nenhuma
        """
        data = self._load()
        today = date.today().isoformat()
        overdue = [
            t for t in data["tasks"]
            if t.get("status") == "open"
            and t.get("due_date")
            and t["due_date"] < today
        ]

        if not overdue:
            return "Nenhuma tarefa vencida."

        lines = []
        for t in sorted(overdue, key=lambda x: x.get("due_date", "")):
            days_late = (date.today() - date.fromisoformat(t["due_date"])).days
            lines.append(f"⚠️ #{t['id']} \"{t['title']}\" — venceu em {t['due_date']} ({days_late} dia(s) atrás)")

        return f"**{len(overdue)} tarefa(s) vencida(s):**\n" + "\n".join(lines)
