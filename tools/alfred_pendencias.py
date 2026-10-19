"""
title: Alfred Pendências e Jornal
description: Fila de convites/contatos que aguardam "vou / não vou" (gerada pelo Inbox Assistant a partir do Thunderbird) e a última edição do jornal diário. Lê e escreve em VAULT_ALFRED/pendencias/ e VAULT_ALFRED/jornal/.
author: Alfred Pennyworth
version: 1.0.0
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred (dentro do container: /vaults/alfred)",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── helpers ───────────────────────────────────────────────
    def _vault(self) -> Path:
        return Path(self.valves.vault_path)

    def _pend_file(self) -> Path:
        return self._vault() / "pendencias" / "pendencias.yml"

    def _load(self) -> dict:
        f = self._pend_file()
        if not f.exists():
            return {"pendencias": []}
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) and isinstance(data.get("pendencias"), list) else {"pendencias": []}
        except Exception:
            return {"pendencias": []}

    def _save(self, data: dict):
        f = self._pend_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _add_task(self, title: str, due_date: Optional[str], tags: list) -> Optional[int]:
        f = self._vault() / "tasks.yml"
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None
        except Exception:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            data = {"tasks": []}
        tid = max((t.get("id", 0) for t in data["tasks"]), default=0) + 1
        data["tasks"].append({
            "id": tid, "title": title, "status": "open", "priority": "normal", "tags": tags,
            "due_date": due_date, "created_at": datetime.now().isoformat(timespec="minutes"), "completed_at": None,
        })
        f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return tid

    # ── tools ─────────────────────────────────────────────────
    def listar_pendencias(self, status: Optional[str] = "aguardando") -> str:
        """
        Lista convites para eventos e contatos profissionais que aguardam a decisão de Pedro.

        Use quando Pedro perguntar "tenho algum convite?", "o que está pendente?",
        "quais eventos chegaram?", "tem algo esperando resposta?".

        :param status: "aguardando" (padrão), "todas", "vou", "nao_vou", "expirada"
        :return: Lista formatada com id, tipo, data e título
        """
        pend = self._load()["pendencias"]
        if status not in (None, "", "todas"):
            pend = [p for p in pend if p.get("status") == status]
        if not pend:
            return "Nenhuma pendência" + ("" if status in (None, "", "todas") else f" com status '{status}'") + "."
        linhas = []
        for p in pend:
            ev = p.get("evento") or {}
            quando = ev.get("data_inicio") or "data não informada"
            local = ev.get("local") or ("online" if ev.get("online") else "")
            linhas.append(
                f"• [{p['id']}] ({p.get('tipo')}, {p.get('status')}) **{p.get('titulo')}** — {quando}"
                + (f" · {local}" if local else "")
                + (f"\n  {ev.get('resumo')}" if ev.get("resumo") else "")
            )
        return f"{len(pend)} pendência(s):\n" + "\n".join(linhas) + \
               "\n\nPara decidir: responder_pendencia(id, 'vou' ou 'nao_vou')."

    def ver_pendencia(self, pendencia_id: str) -> str:
        """
        Mostra o texto explicativo completo que Alfred escreveu sobre uma pendência
        (o que é, por que interessa, o que precisa decidir).

        :param pendencia_id: ID da pendência (ex.: 20261019-meetup-hostgator)
        :return: Conteúdo markdown da pendência
        """
        p = next((x for x in self._load()["pendencias"] if x.get("id") == pendencia_id), None)
        if not p:
            return f"Pendência não encontrada: {pendencia_id}"
        md = self._vault() / ((p.get("arquivos") or {}).get("md") or "")
        if md.is_file():
            txt = md.read_text(encoding="utf-8")
            if txt.startswith("---"):
                end = txt.find("\n---", 3)
                if end > 0:
                    txt = txt[end + 4:].lstrip("\n")
            return txt
        return yaml.dump(p, allow_unicode=True, sort_keys=False)

    def responder_pendencia(self, pendencia_id: str, resposta: str, nota: Optional[str] = "") -> str:
        """
        Registra a decisão de Pedro sobre um convite/contato. "vou" cria uma tarefa em tasks.yml.

        Use quando Pedro disser "vou", "não vou", "aceito", "recuso", "ignora esse" sobre uma pendência.

        :param pendencia_id: ID da pendência
        :param resposta: "vou", "nao_vou" (ou "não vou") ou "ignorar"
        :param nota: Observação opcional de Pedro (ex.: "só a palestra da tarde")
        :return: Confirmação
        """
        r = (resposta or "").strip().lower().replace("-", "_").replace(" ", "_")
        if r in ("vou", "sim", "aceito", "yes"):
            r = "vou"
        elif r in ("nao_vou", "não_vou", "nao", "não", "recuso", "no"):
            r = "nao_vou"
        elif r in ("ignorar", "ignorada"):
            r = "ignorada"
        else:
            return f"Resposta inválida: {resposta!r}. Use 'vou', 'nao_vou' ou 'ignorar'."

        data = self._load()
        p = next((x for x in data["pendencias"] if x.get("id") == pendencia_id), None)
        if not p:
            return f"Pendência não encontrada: {pendencia_id}"
        if p.get("status") != "aguardando":
            return f"Pendência {pendencia_id} já está com status '{p.get('status')}'."

        now = datetime.now().astimezone()
        p["status"] = r
        p["resposta"] = r
        p["respondido_em"] = now.isoformat(timespec="minutes")
        p["nota"] = (nota or "").strip() or None
        task_id = None
        if r == "vou":
            due = ((p.get("evento") or {}).get("data_inicio") or "")[:10] or None
            prefixo = "Ir: " if p.get("tipo") == "evento" else "Responder: "
            task_id = self._add_task(prefixo + p.get("titulo", ""), due, ["pendencia", p.get("tipo", "evento")])
            p["task_id"] = task_id
        self._save(data)

        md = self._vault() / ((p.get("arquivos") or {}).get("md") or "")
        if md.is_file():
            txt = md.read_text(encoding="utf-8")
            txt = re.sub(r"^status: aguardando$", f"status: {r}", txt, count=1, flags=re.M)
            rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}[r]
            txt = txt.rstrip() + f"\n\n## Resposta\n\n**{rotulo}** — {now.strftime('%d/%m/%Y %H:%M')} (via chat)"
            if (nota or "").strip():
                txt += f"\n\nNota: {nota.strip()}"
            if task_id:
                txt += f"\n\nTarefa criada em `tasks.yml` (id {task_id})."
            md.write_text(txt + "\n", encoding="utf-8")

        rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}[r]
        return f"Anotado: {rotulo} para \"{p.get('titulo')}\"." + \
               (f" Tarefa #{task_id} criada em tasks.yml." if task_id else "")

    def jornal_de_hoje(self) -> str:
        """
        Devolve a última edição do jornal diário montado a partir das newsletters do e-mail.

        Use quando Pedro pedir "jornal de hoje", "edição do dia", "o que chegou nas newsletters".

        :return: Markdown da última edição (ou aviso se não houver)
        """
        d = self._vault() / "jornal"
        if not d.is_dir():
            return "Nenhuma edição do jornal foi gerada ainda."
        mds = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mds:
            return "Nenhuma edição do jornal foi gerada ainda."
        txt = mds[0].read_text(encoding="utf-8")
        if txt.startswith("---"):
            end = txt.find("\n---", 3)
            if end > 0:
                txt = txt[end + 4:].lstrip("\n")
        mp3 = mds[0].with_suffix(".mp3")
        if mp3.exists():
            txt += f"\n\n*(Áudio disponível: {mp3.name})*"
        return txt
