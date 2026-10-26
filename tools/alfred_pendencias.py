"""
title: Alfred Pendências e Jornal
description: Fila de convites/contatos que aguardam "vou / não vou" (Inbox Assistant, Thunderbird), mensagens de WhatsApp que aguardam "o que você quer responder?" (Evolution API) e a última edição do jornal diário. Lê e escreve em VAULT_ALFRED/pendencias/ e VAULT_ALFRED/jornal/; respostas de WhatsApp são enviadas pela API do inbox (:7072).
author: Alfred Pennyworth
version: 1.2.0
"""

import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
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
        noticias_path: str = Field(
            default="/vaults/alfred/Noticias",
            description="Cofre de notícias (VAULT_NOTICIAS). O jornal é procurado em <noticias_path>/jornal e, se vazio, em <vault_path>/jornal",
        )
        inbox_api_url: str = Field(
            default="http://172.17.0.1:7072",
            description="API do Inbox Assistant (services/inbox/run.sh serve). Usada para enviar respostas de WhatsApp pela Evolution API",
        )
        inbox_api_token: str = Field(
            default="",
            description="SHELL_EXECUTOR_TOKEN do .env (Bearer da API do inbox)",
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

    @staticmethod
    @contextmanager
    def _trava(dados: Path, timeout: float = 30.0):
        """
        Mesma trava do services/inbox/core.py (trava_arquivo): flock em `.<arquivo>.lock` ao lado do arquivo.
        O vault é bind mount — o container e os processos do host (API, CLI, notificações) disputam o mesmo
        .lock; sem ela, uma gravação daqui apagava o que o host gravou no meio (e vice-versa).
        """
        lock = dados.parent / f".{dados.name}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o666)
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
                        raise TimeoutError(f"{dados.name} ocupado há mais de {timeout:g} s — tente de novo")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)   # fechar solta o flock

    @staticmethod
    def _gravar_atomico(f: Path, texto: str):
        """tmp + os.replace: quem lê no host nunca vê o arquivo pela metade. Chamar dentro da trava."""
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.unlink(missing_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(texto)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, f.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, f)

    def _save(self, data: dict):
        """Chamar DENTRO de `with self._trava(self._pend_file())` (o flock não é reentrante)."""
        self._gravar_atomico(self._pend_file(), yaml.dump(data, allow_unicode=True, sort_keys=False))

    def _add_task(self, title: str, due_date: Optional[str], tags: list) -> Optional[int]:
        f = self._vault() / "tasks.yml"
        with self._trava(f):
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
            self._gravar_atomico(f, yaml.dump(data, allow_unicode=True, sort_keys=False))
        return tid

    def _api(self, metodo: str, rota: str, body: Optional[dict] = None) -> dict:
        """Chama a API do Inbox Assistant. Levanta RuntimeError com mensagem legível em falha."""
        url = self.valves.inbox_api_url.rstrip("/") + rota
        dados = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(url, data=dados, method=metodo, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.valves.inbox_api_token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                detalhe = json.loads(e.read().decode("utf-8")).get("detail")
            except Exception:
                detalhe = e.reason
            raise RuntimeError(f"API do inbox respondeu {e.code}: {detalhe}")
        except Exception as e:
            raise RuntimeError(f"API do inbox inacessível em {url}: {e}")

    # ── tools ─────────────────────────────────────────────────
    def listar_pendencias(self, status: Optional[str] = "aguardando") -> str:
        """
        Lista o que aguarda a decisão de Pedro: convites para eventos, contatos profissionais
        (e-mail, "vou / não vou") e mensagens de WhatsApp ("o que você quer responder?").

        Use quando Pedro perguntar "tenho algum convite?", "o que está pendente?",
        "quais eventos chegaram?", "tem algo esperando resposta?", "quem me mandou WhatsApp?".

        :param status: "aguardando" (padrão), "todas", "vou", "nao_vou", "respondida", "expirada"
        :return: Lista formatada com id, tipo, data/contato e título
        """
        pend = self._load()["pendencias"]
        if status not in (None, "", "todas"):
            pend = [p for p in pend if p.get("status") == status]
        if not pend:
            return "Nenhuma pendência" + ("" if status in (None, "", "todas") else f" com status '{status}'") + "."
        linhas = []
        for p in pend:
            ev = p.get("evento") or {}
            if p.get("tipo") == "whatsapp":
                wa = p.get("whatsapp") or {}
                linhas.append(
                    f"• [{p['id']}] (whatsapp, {p.get('status')}) **{wa.get('contato') or p.get('titulo')}** "
                    f"({wa.get('numero') or '?'}) · {wa.get('tipo_msg')} · {wa.get('categoria')} · urgência {wa.get('urgencia')}"
                    + (f"\n  ❝ {(wa.get('texto') or '')[:300]} ❞" if wa.get("texto") else "")
                    + (f"\n  {ev.get('resumo')}" if ev.get("resumo") else "")
                    + (f"\n  resposta enviada: {p.get('resposta_texto')}" if p.get("resposta_texto") else "")
                )
                continue
            quando = ev.get("data_inicio") or "data não informada"
            local = ev.get("local") or ("online" if ev.get("online") else "")
            linhas.append(
                f"• [{p['id']}] ({p.get('tipo')}, {p.get('status')}) **{p.get('titulo')}** — {quando}"
                + (f" · {local}" if local else "")
                + (f"\n  {ev.get('resumo')}" if ev.get("resumo") else "")
            )
        return f"{len(pend)} pendência(s):\n" + "\n".join(linhas) + \
               "\n\nPara decidir: responder_pendencia(id, 'vou' ou 'nao_vou'); pendência de WhatsApp: " \
               "responder_pendencia(id, '<texto da resposta>') — Alfred envia ao contato."

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
        Registra a decisão de Pedro sobre uma pendência.
        - Convite/contato de e-mail: "vou" (cria tarefa em tasks.yml), "nao_vou" ou "ignorar".
        - Mensagem de WhatsApp: `resposta` é o TEXTO LIVRE que Alfred deve enviar ao contato
          pelo WhatsApp (ou "ignorar" para não responder). Envie exatamente o que Pedro ditou.

        Use quando Pedro disser "vou", "não vou", "aceito", "recuso", "ignora esse" sobre um convite,
        ou "responde pro Carlos que ...", "manda pra ela: ..." sobre uma mensagem de WhatsApp.

        :param pendencia_id: ID da pendência
        :param resposta: "vou" | "nao_vou" | "ignorar" — ou, para WhatsApp, o texto da resposta
        :param nota: Observação opcional de Pedro (ex.: "só a palestra da tarde")
        :return: Confirmação
        """
        p = next((x for x in self._load()["pendencias"] if x.get("id") == pendencia_id), None)
        if not p:
            return f"Pendência não encontrada: {pendencia_id}"
        if p.get("status") != "aguardando":
            return f"Pendência {pendencia_id} já está com status '{p.get('status')}'."
        if p.get("tipo") == "whatsapp":
            return self._responder_whatsapp(p, resposta or "", nota or "")

        r = (resposta or "").strip().lower().replace("-", "_").replace(" ", "_")
        if r in ("vou", "sim", "aceito", "yes"):
            r = "vou"
        elif r in ("nao_vou", "não_vou", "nao", "não", "recuso", "no"):
            r = "nao_vou"
        elif r in ("ignorar", "ignorada"):
            r = "ignorada"
        else:
            return f"Resposta inválida: {resposta!r}. Use 'vou', 'nao_vou' ou 'ignorar'."

        now = datetime.now().astimezone()
        task_id = None
        # ler → checar → mudar → gravar dentro da trava (a API, o CLI ou a notificação podem ter respondido)
        with self._trava(self._pend_file()):
            data = self._load()
            p = next((x for x in data["pendencias"] if x.get("id") == pendencia_id), None)
            if not p:
                return f"Pendência não encontrada: {pendencia_id}"
            if p.get("status") != "aguardando":
                return f"Pendência {pendencia_id} já está com status '{p.get('status')}'."
            p["status"] = r
            p["resposta"] = r
            p["respondido_em"] = now.isoformat(timespec="minutes")
            p["nota"] = (nota or "").strip() or None
            p["respondido_via"] = "chat"
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

    def _responder_whatsapp(self, p: dict, texto: str, nota: str) -> str:
        """Texto livre → API do inbox (que grava e envia pela Evolution API). Sem API: só registra no YAML."""
        texto = texto.strip()
        if not texto:
            return "Diga o texto da resposta (ou 'ignorar')."
        wa = p.get("whatsapp") or {}
        contato = wa.get("contato") or p.get("titulo")
        try:
            e = self._api("POST", f"/pendencias/{p['id']}/responder-texto", {"texto": texto, "enviar": True, "nota": nota})
            envio = (e.get("whatsapp") or {}).get("envio") or {}
            if e.get("status") == "ignorada":
                return f"Anotado: não respondo a {contato}."
            if envio.get("simulado"):
                return f"Resposta registrada para {contato} (envio SIMULADO — EVOLUTION_URL não configurada):\n“{texto}”"
            return f"Enviado a {contato} pelo WhatsApp:\n“{texto}”"
        except RuntimeError as err:
            # fallback: registra no YAML; o envio fica pendente (run.sh responder-texto reenviará)
            now = datetime.now().astimezone()
            ignorar = texto.lower().rstrip(".!") in ("ignorar", "ignora", "não responder", "nao responder")
            with self._trava(self._pend_file()):
                data = self._load()
                p = next((x for x in data["pendencias"] if x.get("id") == p["id"]), None)
                if not p or p.get("status") != "aguardando":
                    return (f"Não registrei: a pendência já foi resolvida por outro caminho "
                            f"(status '{(p or {}).get('status')}').")
                p["status"] = "ignorada" if ignorar else "aguardando"
                p["resposta_texto"] = None if ignorar else texto
                p["nota"] = nota.strip() or None
                if ignorar:
                    p["resposta"] = "ignorada"
                    p["respondido_em"] = now.isoformat(timespec="minutes")
                    p["respondido_via"] = "chat"
                else:
                    p.setdefault("whatsapp", {})["envio"] = {"ok": False, "erro": f"API indisponível ({err})",
                                                             "solicitado_em": now.isoformat(timespec="minutes")}
                self._save(data)
            if ignorar:
                return f"Anotado: não respondo a {contato} (API do inbox indisponível, registrado só no vault)."
            return (f"Guardei a resposta para {contato}, mas NÃO consegui enviar: {err}. "
                    f"Reenvie com: services/inbox/run.sh responder-texto {p['id']} \"{texto}\"")

    def jornal_de_hoje(self) -> str:
        """
        Devolve a última edição do jornal diário montado a partir das newsletters do e-mail.

        Use quando Pedro pedir "jornal de hoje", "edição do dia", "o que chegou nas newsletters".

        :return: Markdown da última edição (ou aviso se não houver)
        """
        mds = []
        for d in (Path(self.valves.noticias_path) / "jornal", self._vault() / "jornal"):
            if d.is_dir():
                mds = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if mds:
                break
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

    # ── limpeza pós-podcast (services/inbox/LIMPEZA.md) ──────
    def listar_limpezas(self) -> str:
        """
        Mostra os pedidos de limpeza da caixa de e-mail ("posso apagar estes N e-mails?") que aguardam
        a decisão de Pedro, com os e-mails de cada lote e o motivo.

        Use quando Pedro perguntar "o que você quer apagar?", "tem limpeza pendente?", "quais e-mails vão pra lixeira?".

        :return: Lotes aguardando, com id, quantidade e amostra dos e-mails
        """
        try:
            lotes = self._api("GET", "/limpeza/lotes?status=aguardando").get("lotes") or []
        except RuntimeError as e:
            return str(e)
        if not lotes:
            return "Nenhum pedido de limpeza aguardando."
        partes = []
        for l in lotes:
            amostra = "\n".join(f"  • {i['date'][:10]} {i['from']} — {(i.get('subject') or '')[:70]} ({i['motivo']})"
                                for i in l["itens"][:15])
            mais = f"\n  … e mais {l['total'] - 15}" if l["total"] > 15 else ""
            partes.append(f"[{l['id']}] {l['total']} e-mail(s) → Lixeira\n{amostra}{mais}")
        return "\n\n".join(partes) + \
            "\n\nSó aprove se Pedro disser claramente que sim: responder_limpeza(id, 'aprovar' | 'recusar')."

    def responder_limpeza(self, lote_id: str, decisao: str, nota: Optional[str] = "") -> str:
        """
        Registra a decisão de Pedro sobre um pedido de limpeza. 'aprovar' libera a extensão do Thunderbird
        para MOVER os e-mails do lote para a Lixeira. Chame com 'aprovar' SOMENTE quando Pedro tiver
        confirmado explicitamente esse lote nesta conversa ("pode apagar", "aprovo a limpeza X");
        na dúvida, pergunte antes.

        :param lote_id: ID do lote (ex.: limpeza-20261019-2210)
        :param decisao: "aprovar" ou "recusar"
        :param nota: observação opcional
        :return: Confirmação
        """
        d = (decisao or "").strip().lower()
        if d not in ("aprovar", "recusar"):
            return "Decisão inválida: use 'aprovar' ou 'recusar'."
        try:
            r = self._api("POST", f"/limpeza/{lote_id}/{d}", {"nota": nota or "via Open WebUI"})
        except RuntimeError as e:
            return str(e)
        if d == "recusar":
            return f"Limpeza {lote_id} recusada — nada será apagado."
        return (f"Limpeza {lote_id} aprovada: {r.get('total')} e-mail(s) irão para a Lixeira na próxima passada "
                f"da extensão do Thunderbird.")
