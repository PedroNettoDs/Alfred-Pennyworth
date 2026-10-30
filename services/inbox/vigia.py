"""
Alfred — vigia (alfred-vigia.timer: 2 min após o boot e a cada 10 min).

Confere se tudo que faz o Alfred "ver" as mensagens está de pé e conserta o que dá sozinho:
  1. API do inbox (:7072)            → fora do ar: `systemctl --user restart alfred-inbox-api`
  2. containers da Evolution API     → parados: `docker start`
  3. conexão do WhatsApp (instância) → caiu (ex.: aparelho desvinculado): notificação no PC com
                                        [Mostrar QR code] — o pareamento precisa do celular de Pedro
  4. Thunderbird aberto?             → só informa (o ciclo de e-mail lê o que ele sincronizou)
  5. áudios de resumo do WhatsApp    → apaga os mais velhos que audio_reter_dias (padrão 2)
  6. agenda                          → avisa se há compromisso esperando a extensão há muito tempo

Estado entre execuções em logs/vigia_estado.json (para não repetir aviso a cada 10 min).
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

import core
from _shared.lib_alfred import log

ESTADO_FILE = core.PROJECT_ROOT / "logs" / "vigia_estado.json"
CONTAINERS = ("alfred-evolution-db", "alfred-evolution")
REAVISAR = timedelta(hours=3)
NOTIFICAR_DBUS = Path(__file__).parent / "notificar_dbus.py"


def _estado() -> dict:
    try:
        return json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_estado(e: dict) -> None:
    ESTADO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ESTADO_FILE.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")


def _pode_avisar(est: dict, chave: str) -> bool:
    ts = est.get("avisos", {}).get(chave)
    try:
        return not ts or datetime.now() - datetime.fromisoformat(ts) >= REAVISAR
    except ValueError:
        return True


def _marcar_aviso(est: dict, chave: str) -> None:
    est.setdefault("avisos", {})[chave] = datetime.now().isoformat(timespec="seconds")


# ── checagens ─────────────────────────────────────────────────

def checar_api(consertar: bool = True) -> dict:
    url = f"http://127.0.0.1:{os.getenv('PORT_INBOX', '7072')}/health"
    try:
        r = httpx.get(url, timeout=8)
        if r.status_code == 200:
            return {"ok": True}
        erro = f"HTTP {r.status_code}"
    except Exception as e:
        erro = str(e)[:120]
    res = {"ok": False, "erro": erro}
    if consertar and shutil.which("systemctl"):
        p = subprocess.run(["systemctl", "--user", "restart", "alfred-inbox-api"], capture_output=True, text=True)
        res["reiniciada"] = p.returncode == 0
        log(f"[vigia] API fora do ar ({erro}) → restart {'ok' if p.returncode == 0 else 'falhou: ' + p.stderr.strip()[:100]}")
    return res


def checar_containers(consertar: bool = True) -> dict:
    if not shutil.which("docker"):
        return {"ok": None, "erro": "docker não encontrado"}
    out = {}
    for nome in CONTAINERS:
        p = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", nome], capture_output=True, text=True)
        st = p.stdout.strip() if p.returncode == 0 else "ausente"
        out[nome] = st
        if st in ("exited", "created", "paused") and consertar:
            cmd = ["docker", "unpause" if st == "paused" else "start", nome]
            r = subprocess.run(cmd, capture_output=True, text=True)
            out[nome] = f"{st} → {'iniciado' if r.returncode == 0 else 'falhou'}"
            log(f"[vigia] container {nome} estava {st} → {' '.join(cmd[1:2])} {'ok' if r.returncode == 0 else 'falhou'}")
    return {"ok": all(v == "running" or v.endswith("iniciado") for v in out.values()), "containers": out}


def checar_whatsapp() -> dict:
    from _shared import lib_whatsapp as lw
    if not lw.configurado():
        return {"ok": None, "estado": "não configurado"}
    try:
        est = lw.estado_instancia()
    except Exception as e:
        return {"ok": False, "estado": "erro", "erro": str(e)[:120]}
    estado = est.get("estado") or est.get("erro") or f"HTTP {est.get('status')}"
    return {"ok": estado == "open", "estado": estado}


def thunderbird_aberto() -> bool:
    p = subprocess.run(["pgrep", "-f", "thunderbird-bin|/thunderbird$"], capture_output=True, text=True)
    return p.returncode == 0


def agenda_parada(minutos: int = 30) -> list[dict]:
    try:
        import agenda
    except Exception:
        return []
    limite = core._now() - timedelta(minutes=minutos)
    out = []
    for i in agenda.listar():
        if i.get("status") not in ("pendente", "em_execucao", "erro"):
            continue
        try:
            if datetime.fromisoformat(i["criado_em"]) < limite:
                out.append(i)
        except (KeyError, ValueError):
            continue
    return out


# ── avisos ────────────────────────────────────────────────────

def _aviso_whatsapp_caiu(estado: str) -> None:
    """Processo destacado: notificação com [Mostrar QR code] que espera o clique."""
    if core._tem_gui():
        core._iniciar_aguardador("_vigia-qr", estado)   # unit própria: sobrevive ao fim do vigia
    else:
        core._notify("Alfred — WhatsApp desconectado",
                     "Reconecte: services/inbox/run.sh whatsapp qrcode e escaneie no celular.")


def aguardar_qr(estado: str) -> int:
    """Chamado pelo `_vigia-qr`: mostra o aviso e, se Pedro clicar, gera e abre o QR code."""
    corpo = (f"A conexão do WhatsApp caiu (estado: {estado}). Até reconectar, o Alfred não vê as mensagens.\n\n"
             "Clique em “Mostrar QR code” e escaneie em WhatsApp → Aparelhos conectados → Conectar aparelho.")
    acao = None   # None = sem notificação com botões (script ausente, rc≠0, exceção) → aviso simples
    py = shutil.which("python3", path="/usr/bin:/bin")
    if py and NOTIFICAR_DBUS.exists():
        try:
            out = subprocess.run([py, "-I", str(NOTIFICAR_DBUS)],
                                 input=json.dumps({"titulo": "Alfred — WhatsApp desconectado", "corpo": corpo,
                                                   "icone": "network-disconnect",
                                                   "acoes": [["qr", "Mostrar QR code"], ["depois", "Depois"]]}),
                                 capture_output=True, text=True, timeout=60 * 60 * 6)
            acao = out.stdout.strip() if out.returncode == 0 else None
        except subprocess.TimeoutExpired:
            acao = ""   # o aviso ficou na tela sem clique: não repete
        except Exception:
            acao = None
    if acao is None:   # aviso simples, sem botão: o caminho é o comando
        core._notify("Alfred — WhatsApp desconectado",
                     f"A conexão do WhatsApp caiu (estado: {estado}). Até reconectar, o Alfred não vê as mensagens.\n\n"
                     "Para parear de novo: services/inbox/run.sh whatsapp qrcode e escaneie em WhatsApp → "
                     "Aparelhos conectados → Conectar aparelho.")
    if acao == "qr":
        subprocess.run([sys.executable, str(Path(__file__).parent / "inbox-assistant.py"), "whatsapp", "qrcode"],
                       capture_output=True, text=True, timeout=120)
    return 0


# ── execução ──────────────────────────────────────────────────

def rodar(consertar: bool = True, notificar: bool = True, limpar: bool = True) -> dict:
    est = _estado()
    agora = datetime.now().isoformat(timespec="seconds")
    res = {"em": agora, "api": checar_api(consertar), "containers": checar_containers(consertar)}

    wa = checar_whatsapp()
    res["whatsapp"] = wa
    anterior = est.get("whatsapp_estado")
    if wa["ok"] is False:
        if anterior != wa["estado"]:
            est["whatsapp_caiu_em"] = agora
            log(f"[vigia] WhatsApp desconectado (estado {wa['estado']})")
        if notificar and _pode_avisar(est, "whatsapp"):
            _aviso_whatsapp_caiu(wa["estado"])
            _marcar_aviso(est, "whatsapp")
    elif wa["ok"] and anterior not in (None, "open"):
        log("[vigia] WhatsApp reconectado")
        est.get("avisos", {}).pop("whatsapp", None)
        if notificar:
            core._notify("Alfred — WhatsApp reconectado", "Voltei a acompanhar suas mensagens.")
    if wa.get("estado"):
        est["whatsapp_estado"] = wa["estado"]

    res["thunderbird_aberto"] = thunderbird_aberto()

    if limpar:
        try:
            import whatsapp
            res["audios"] = whatsapp.limpar_audios()
        except Exception as e:
            res["audios"] = {"erro": str(e)[:160]}

    parados = agenda_parada()
    res["agenda_esperando_extensao"] = [i["id"] for i in parados]
    if parados and notificar and _pode_avisar(est, "agenda"):
        core._notify("Alfred — agenda esperando o Thunderbird",
                     f"{len(parados)} compromisso(s) ainda não foram para o calendário. "
                     "Abra o Thunderbird e confira se a extensão “Alfred — agenda” está instalada.")
        _marcar_aviso(est, "agenda")

    est["ultima_execucao"] = agora
    _salvar_estado(est)
    return res


def cmd_vigia(args) -> int:
    r = rodar(consertar=not args.sem_consertar, notificar=not args.sem_notificar, limpar=not args.sem_limpar)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        api = "ok" if r["api"]["ok"] else f"FORA ({r['api'].get('erro')})"
        cont = ", ".join(f"{k}={v}" for k, v in (r["containers"].get("containers") or {}).items())
        print(f"API inbox: {api}")
        print(f"Containers: {cont or r['containers'].get('erro')}")
        print(f"WhatsApp: {r['whatsapp'].get('estado')}")
        print(f"Thunderbird aberto: {'sim' if r['thunderbird_aberto'] else 'não'}")
        a = r.get("audios") or {}
        if "removidos" in a:
            print(f"Áudios de resumo: {len(a['removidos'])} apagado(s) (> {a['dias']:g} dias), {a['mantidos']} mantido(s)")
        if r["agenda_esperando_extensao"]:
            print(f"Agenda: {len(r['agenda_esperando_extensao'])} compromisso(s) esperando a extensão do Thunderbird")
    # código 0 mesmo com problema (o timer não fica "failed" a cada 10 min); --estrito devolve 1
    return 1 if args.estrito and (r["whatsapp"]["ok"] is False or not r["api"]["ok"]) else 0


def adicionar_parser(sub) -> None:
    sp = sub.add_parser("vigia", help="confere API, containers, WhatsApp e Thunderbird; apaga áudios antigos")
    sp.add_argument("--sem-consertar", action="store_true", help="não reinicia API nem containers")
    sp.add_argument("--sem-notificar", action="store_true")
    sp.add_argument("--sem-limpar", action="store_true", help="não apaga áudios de resumo")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--estrito", action="store_true", help="sai com código 1 se WhatsApp ou API estiverem fora")
    sp = sub.add_parser("_vigia-qr")
    sp.add_argument("estado", nargs="?", default="close")
