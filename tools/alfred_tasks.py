"""
title: Alfred Tasks
description: Gerencia tarefas e lembretes do Pedro. Adiciona, lista, completa e verifica prazos vencidos. Armazena em VAULT_ALFRED/tasks.yml.
author: Alfred Pennyworth
version: 1.2.0
"""

from pydantic import BaseModel, Field
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import fcntl
import os
import re
import time
import yaml


class TasksIlegivel(Exception):
    """tasks.yml existe mas não dá para ler (erro de YAML, formato errado): nada é gravado por cima."""


def _data_iso(valor) -> Optional[str]:
    """Normaliza uma data de vencimento para AAAA-MM-DD. Aceita AAAA-MM-DD (também com mês/dia de um dígito),
    DD/MM/AAAA e date do YAML (data sem aspas editada à mão). None/"" → None. Inválida → ValueError."""
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    txt = str(valor).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    raise ValueError(f"data inválida: {txt!r}")


def _data_ou_none(valor) -> Optional[str]:
    try:
        return _data_iso(valor)
    except ValueError:
        return None


def _herdar_dono(caminho, referencia) -> None:
    """Rodando como root (container do Open WebUI), o arquivo novo herda dono e grupo de `referencia`
    (a pasta do arquivo, do usuário do host): sem isso, cada gravação deixava pendencias.yml / tasks.yml de root no vault."""
    if os.geteuid() != 0:
        return
    try:
        st = os.stat(referencia)
        if isinstance(caminho, int):
            os.fchown(caminho, st.st_uid, st.st_gid)
        else:
            os.chown(caminho, st.st_uid, st.st_gid)
    except OSError:
        pass


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
        """Lê tasks.yml. Arquivo ilegível levanta TasksIlegivel — antes virava "lista vazia" e a próxima
        gravação apagava todas as tarefas (e reaproveitava os ids)."""
        f = self._tasks_file()
        if not f.exists():
            return {"tasks": []}
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            linha = getattr(getattr(e, "problem_mark", None), "line", None)
            onde = f" (linha {linha + 1})" if linha is not None else ""
            raise TasksIlegivel(f"{f.name} está com erro de YAML{onde} — corrija o arquivo antes; nada foi alterado.")
        except OSError as e:
            raise TasksIlegivel(f"não consegui ler {f.name}: {e}")
        if data is None:
            return {"tasks": []}
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(tasks or [], list) \
                or not all(isinstance(t, dict) for t in tasks or []):
            raise TasksIlegivel(f"{f.name} não tem o formato esperado (tasks: lista de tarefas) — corrija o arquivo "
                                "antes; nada foi alterado.")
        data["tasks"] = tasks or []
        return data

    @contextmanager
    def _trava(self, timeout: float = 30.0):
        """
        Mesma trava do services/inbox/core.py (trava_arquivo): flock em `.tasks.yml.lock` ao lado do arquivo.
        O Alfred no host (pendências "vou", botão Agenda, agenda) também cria tarefas — o vault é bind mount,
        então todos disputam o mesmo .lock e nenhuma tarefa some nem repete id.
        """
        f = self._tasks_file()
        lock = f.parent / f".{f.name}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o666)
            _herdar_dono(fd, lock.parent)
        except PermissionError:
            fd = os.open(str(lock), os.O_RDONLY)
        try:
            limite = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= limite:
                        raise TimeoutError(f"tasks.yml ocupado há mais de {timeout:g} s — tente de novo")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)   # fechar solta o flock

    def _save(self, data: dict):
        """Gravação atômica (tmp + os.replace). Chamar DENTRO de `with self._trava()`."""
        f = self._tasks_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.unlink(missing_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(yaml.dump(data, allow_unicode=True, sort_keys=False))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, f.stat().st_mode & 0o777)
        except OSError:
            pass
        _herdar_dono(tmp, f.parent)
        os.replace(tmp, f)

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
        :param due_date: Data de vencimento no formato YYYY-MM-DD (aceita DD/MM/AAAA; opcional)
        :param tags: Tags separadas por vírgula, ex: "fatec,estudo" (opcional)
        :param priority: "low", "normal" ou "high" (padrão: "normal")
        :return: Confirmação com ID da tarefa criada
        """
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        try:
            due_date = _data_iso(due_date)
        except ValueError:
            return (f"Data de vencimento inválida: {due_date!r}. Use AAAA-MM-DD (ex.: 2026-10-31) ou DD/MM/AAAA. "
                    "Tarefa não criada.")
        with self._trava():   # ler → próximo id → gravar sem ninguém no meio
            try:
                data = self._load()
            except TasksIlegivel as e:
                return f"Erro: {e} Tarefa não criada."
            task_id = self._next_id(data["tasks"])
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

        :param filter: "open" (abertas), "done" (concluídas), "cancelled" (canceladas) ou "all" (todas)
        :return: Lista formatada de tarefas
        """
        try:
            data = self._load()
        except TasksIlegivel as e:
            return f"Erro: {e}"
        tasks = data["tasks"]

        if filter == "open":
            tasks = [t for t in tasks if t.get("status") == "open"]
        elif filter == "done":
            tasks = [t for t in tasks if t.get("status") == "done"]
        elif filter == "cancelled":   # o Alfred cancela a tarefa de um "vou" que virou "não vou"
            tasks = [t for t in tasks if t.get("status") == "cancelled"]

        if not tasks:
            return "Nenhuma tarefa encontrada." if filter == "all" else "Nenhuma tarefa aberta."

        today = date.today().isoformat()
        lines = []
        for t in sorted(tasks, key=lambda x: (_data_ou_none(x.get("due_date")) or "9999", x.get("id", 0))):
            status_icon = {"done": "✓", "cancelled": "✗"}.get(t.get("status"), "○")
            priority_icon = {"high": "!", "low": "↓"}.get(t.get("priority", ""), " ")
            raw_due = t.get("due_date")
            due = _data_ou_none(raw_due)
            due_str = ""
            if due:
                vencida = " ⚠️ VENCIDA" if due < today and t.get("status") == "open" else ""
                due_str = f" · vence {due}{vencida}"
            elif raw_due:
                due_str = f" · vence {raw_due} (data inválida)"
            tags_str = f" [{', '.join(t['tags'])}]" if t.get("tags") else ""
            lines.append(f"{status_icon} [{priority_icon}] #{t['id']} {t['title']}{due_str}{tags_str}")

        header = {"open": "Tarefas abertas", "done": "Tarefas concluídas", "cancelled": "Tarefas canceladas",
                  "all": "Todas as tarefas"}.get(filter, "Tarefas")
        return f"**{header} ({len(lines)}):**\n" + "\n".join(lines)

    def complete_task(self, task_id: int) -> str:
        """
        Marca uma tarefa como concluída.

        Use quando Pedro disser que terminou algo ou pediu para marcar uma tarefa como feita.

        :param task_id: ID numérico da tarefa (obtido com list_tasks)
        :return: Confirmação
        """
        with self._trava():
            try:
                data = self._load()
            except TasksIlegivel as e:
                return f"Erro: {e}"
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
        try:
            data = self._load()
        except TasksIlegivel as e:
            return f"Erro: {e}"
        today = date.today().isoformat()
        abertas = [t for t in data["tasks"] if t.get("status") == "open" and t.get("due_date")]
        overdue = [(_data_ou_none(t["due_date"]), t) for t in abertas]
        invalidas = [t for d, t in overdue if d is None]
        overdue = [(d, t) for d, t in overdue if d and d < today]

        if not overdue and not invalidas:
            return "Nenhuma tarefa vencida."

        lines = []
        for d, t in sorted(overdue, key=lambda x: x[0]):
            days_late = (date.today() - date.fromisoformat(d)).days
            lines.append(f"⚠️ #{t.get('id')} \"{t.get('title')}\" — venceu em {d} ({days_late} dia(s) atrás)")
        for t in invalidas:
            lines.append(f"? #{t.get('id')} \"{t.get('title')}\" — data de vencimento inválida: {t['due_date']!r}")

        if not overdue:
            return "Nenhuma tarefa vencida.\n" + "\n".join(lines)
        return f"**{len(overdue)} tarefa(s) vencida(s):**\n" + "\n".join(lines)
