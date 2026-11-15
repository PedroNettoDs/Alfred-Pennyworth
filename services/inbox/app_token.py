"""
Alfred — token semanal do aplicativo do celular (app/, Flutter).

Fora de casa, o aplicativo alcança o Painel pela Tailscale (PAINEL_HOSTS_APP no .env; vazio = recurso desligado) e
entra com um token que vale 7 dias, no lugar do SHELL_EXECUTOR_TOKEN: esse nunca é aceito de um aparelho da
tailnet (painel.login, main._PortaoTailnet). A sessão do aplicativo é assinada com o token da semana
(painel._nova_sessao), então trocar o token derruba o aplicativo na hora.

O laço de 60 s da API (main._laco_fila_whatsapp → passar) gera o token quando falta ou venceu e o manda ao
WhatsApp de Pedro pelo número do Alfred. A tela /painel/aplicativo (só em 127.0.0.1) mostra o token e o QR code
e tem o botão que gera outro antes da hora. O token fica em logs/app_token.json (modo 600) e nunca vai para o
log nem para o feed de avisos.
"""

import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from datetime import datetime
from typing import Optional

import core
import whatsapp
from _shared import lib_whatsapp
from _shared.lib_alfred import log, PROJECT_ROOT

ARQUIVO = PROJECT_ROOT / "logs" / "app_token.json"
VALIDADE_S = 7 * 86400
NOVA_TENTATIVA_S = 600            # o envio ao WhatsApp falhou (Evolution fora, sem sessão assistente): tenta de novo
TAILNET = ipaddress.ip_network("100.64.0.0/10")   # a faixa da Tailscale; a API escuta só em IPv4

_TRAVA = threading.Lock()         # o laço da API e o botão do Painel gravam o mesmo arquivo


def hosts() -> tuple[str, ...]:
    """Os nomes e IPs (como vêm no cabeçalho Host, sem a porta) pelos quais o aplicativo chama o Painel:
    PAINEL_HOSTS_APP do .env, separados por vírgula. O primeiro vai no QR code. Vazio = recurso desligado."""
    return tuple(h for h in (p.strip().lower() for p in os.getenv("PAINEL_HOSTS_APP", "").split(",")) if h)


def da_tailnet(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip or "") in TAILNET
    except ValueError:
        return False


def url() -> str:
    """O endereço que o aplicativo usa ("" com o recurso desligado)."""
    h = hosts()
    return f"http://{h[0]}:{os.getenv('PORT_INBOX', '7073')}" if h else ""


def _ler() -> dict:
    try:
        d = json.loads(ARQUIVO.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(d: dict) -> None:
    ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO.with_name(f".{ARQUIVO.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # já nasce 600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, ARQUIVO)


def atual(agora: Optional[float] = None) -> Optional[dict]:
    """O token em vigor: {token, criado_em, expira_em, enviado_em} (horas em epoch), ou None se falta ou venceu."""
    d = _ler()
    try:
        vale = bool(d.get("token")) and float(d.get("expira_em") or 0) > (agora or time.time())
    except (TypeError, ValueError):
        vale = False
    return d if vale else None


def conferir(candidato: str) -> Optional[dict]:
    """O token em vigor, se `candidato` for ele; senão None."""
    d = atual()
    if d and candidato and hmac.compare_digest(candidato.encode("utf-8", "surrogateescape"),
                                               str(d["token"]).encode("utf-8")):
        return d
    return None


def renovar(agora: Optional[float] = None) -> dict:
    """Token novo, válido por 7 dias. O anterior deixa de valer na hora, e com ele as sessões do aplicativo."""
    agora = agora or time.time()
    d = {"token": secrets.token_urlsafe(24), "criado_em": agora, "expira_em": agora + VALIDADE_S, "enviado_em": None}
    with _TRAVA:
        _gravar(d)
    log("[app] token semanal do aplicativo renovado")
    return d


def _enviar(d: dict) -> bool:
    """Duas mensagens pelo número do Alfred: o aviso com o endereço e, sozinho, o token (fácil de copiar)."""
    inst = next((s["instancia"] for s in whatsapp.sessoes() if s["papel"] == "assistente"), None)
    numero = lib_whatsapp.numero_pedro()
    if not (inst and numero):
        return False
    texto = (f"📱 *Aplicativo do Alfred*: token novo, válido até {datetime.fromtimestamp(d['expira_em']):%d/%m às %H:%M}."
             f"\nEndereço: {url()}\nO token vem na próxima mensagem: copie e cole no aplicativo.")
    return bool(lib_whatsapp.enviar_texto(numero, texto, inst=inst, aviso=False).get("ok")
                and lib_whatsapp.enviar_texto(numero, d["token"], inst=inst, aviso=False).get("ok"))


def passar(agora: Optional[float] = None) -> None:
    """Uma volta do laço: gera o token que falta ou venceu e manda a Pedro o que ainda não foi enviado. No modo
    simulado o token não é enviado (o envio simulado grava o texto em logs/whatsapp_enviados.jsonl)."""
    if not hosts():
        return
    agora = agora or time.time()
    d = atual(agora)
    if not d:
        d = renovar(agora)
        core._notify("Alfred — aplicativo", "O token do aplicativo do celular mudou. O novo está em "
                     f"{core.url_painel('aplicativo')}", aviso=False)
    if d.get("enviado_em") or lib_whatsapp.simulado() or agora - (d.get("tentado_em") or 0) < NOVA_TENTATIVA_S:
        return
    enviou = _enviar(d)
    with _TRAVA:
        no_disco = _ler()
        if no_disco.get("token") != d["token"]:
            return                             # Pedro gerou outro pelo Painel enquanto este saía
        no_disco["enviado_em" if enviou else "tentado_em"] = agora
        _gravar(no_disco)
    if not enviou:
        log("[app] não consegui mandar o token do aplicativo ao WhatsApp (sessão assistente ou envio); tento de novo")
