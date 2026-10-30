#!/usr/bin/env python3
"""
Alfred Inbox Assistant — CLI.

Uso:
    inbox-assistant.py                         → ciclo completo (inbox padrão do .env)
    inbox-assistant.py ciclo [--inbox NOME] [--desde HORAS] [--reprocessar] [--max N] [--sem-notificar]
    inbox-assistant.py scan  [--desde HORAS]   → só classifica e mostra (nada é gravado)
    inbox-assistant.py jornal [--desde HORAS]  → só a edição do dia (newsletters)
    inbox-assistant.py podcast [--dias 3] [--se-devido] [--semana AAAA-Www] [--sem-audio] [--dry-run] → podcast (VAULT_NOTICIAS/podcast)
    inbox-assistant.py limpeza [candidatos|propor|lotes|ver ID|aprovar ID|recusar ID]  → limpeza pós-podcast (LIMPEZA.md)
    inbox-assistant.py pendencias [--todas]    → fila de "vou / não vou"
    inbox-assistant.py perguntar [--ouvir]     → modo interativo: Alfred pergunta e espera a resposta no terminal
    inbox-assistant.py ver ID                  → texto explicativo da pendência
    inbox-assistant.py ouvir ID                → toca o áudio da pergunta
    inbox-assistant.py responder ID vou|nao-vou|ignorar [--nota "..."]
    inbox-assistant.py responder-texto ID "texto" [--sem-enviar]   → pendência de WhatsApp: registra e envia ao contato
    inbox-assistant.py whatsapp status                              → Evolution API: config, instância, fila
    inbox-assistant.py whatsapp qrcode                              → QR code para parear (salva logs/evolution_qr.png)
    inbox-assistant.py whatsapp webhook URL                         → aponta o webhook da instância para URL
    inbox-assistant.py whatsapp simular payload.json [--notificar] [--sem-enviar] [--imediato]  → processa um webhook gravado
    inbox-assistant.py whatsapp notificar ID                        → manda a Pedro o resumo + áudio da pergunta
    inbox-assistant.py whatsapp fila                                → mensagens esperando atraso_minutos (prazo, textos)
    inbox-assistant.py whatsapp processar-fila [--todos] [--sem-enviar]  → cria as pendências vencidas (--todos: já)

Saída final do ciclo: linhas JORNAL_FILE=... e PENDENCIA_NOVA=... (uma por pendência),
fáceis de consumir por shell/N8N.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import load_env, log  # noqa: E402

load_env(override=False)  # .env da raiz; variáveis já exportadas (run.sh, testes) têm prioridade

import core  # noqa: E402
import whatsapp  # noqa: E402
from _shared import lib_db  # noqa: E402
from _shared.lib_tts import tocar_audio, iniciar_audio, parar_audio, tocando  # noqa: E402


def _msg_erro(e: Exception) -> str:
    """str(KeyError("x")) vira "'x'" (com aspas) — para KeyError mostra a mensagem crua."""
    return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)


# ── Subcomandos ───────────────────────────────────────────────

def cmd_ciclo(args, dry_run=False, so_jornal=False, so_pendencias=False):
    resumo = core.ciclo(
        inbox_name=args.inbox, dry_run=dry_run, janela_horas=args.desde,
        reprocessar=args.reprocessar, so_jornal=so_jornal, so_pendencias=so_pendencias,
        max_emails=args.max, notificar=not args.sem_notificar,
    )
    if dry_run or args.json:
        _tabela_classificacao(resumo["classificacao"])
    if args.json:
        print(json.dumps(resumo, ensure_ascii=False, indent=2, default=str))
    if resumo.get("jornal"):
        print(f"JORNAL_FILE={resumo['jornal']['md']}")
        if resumo["jornal"].get("mp3"):
            print(f"JORNAL_AUDIO={resumo['jornal']['mp3']}")
    for p in resumo.get("pendencias_novas", []):
        print(f"PENDENCIA_NOVA={p['id']}")
    if resumo.get("pendencias_aguardando"):
        print(f"\n{len(resumo['pendencias_aguardando'])} pendência(s) aguardando resposta — "
              f"veja: inbox-assistant.py pendencias")
    return 0


def _tabela_classificacao(linhas: list[dict]):
    if not linhas:
        print("(nenhum e-mail novo na janela)")
        return
    print(f"\n{'categoria':<12} {'conf':>4}  {'data':<16} {'de':<28} assunto")
    print("-" * 100)
    for r in linhas:
        print(f"{r['categoria']:<12} {r['confianca']:>4.2f}  {r['date'][:16]:<16} {r['from'][:28]:<28} {r['subject'][:48]}")
    print()


def cmd_buscar(args):
    q = " ".join(args.consulta)
    if args.whatsapp:
        for r in lib_db.buscar_wa(q, limite=args.max):
            print(f"{r['ts'][:16]}  {'Pedro' if r['from_me'] else r['contato']:<22} {r['trecho']}")
    else:
        for r in lib_db.buscar_notas(q, cofre=args.cofre, limite=args.max):
            print(f"{r['score']:>6}  [{r['cofre']}] {r['caminho']}\n        {r['trecho'][:160]}")
    return 0


def cmd_pendencias(args):
    status = "todas" if args.todas else "aguardando"
    pend = core.listar_pendencias(status)
    if not pend:
        print("Nenhuma pendência" + ("" if args.todas else " aguardando resposta") + ".")
        return 0
    print(f"\n{'id':<42} {'status':<10} {'tipo':<9} {'quando / contato':<18} título")
    print("-" * 112)
    for p in pend:
        if p.get("tipo") == "whatsapp":
            quando = (p.get("whatsapp") or {}).get("contato") or "-"
        else:
            quando = (p.get("evento") or {}).get("data_inicio") or "-"
        print(f"{p['id']:<42} {p['status']:<10} {p['tipo']:<9} {str(quando)[:17]:<18} {p['titulo'][:40]}")
    print("\nResponder: inbox-assistant.py responder <id> vou | nao-vou   ·   WhatsApp: responder-texto <id> \"...\""
          "   ·   Ouvir: ouvir <id>\n")
    return 0


def cmd_ver(args):
    p = core.obter_pendencia(args.id)
    if not p:
        print(f"Pendência não encontrada: {args.id}")
        return 1
    md = core.vault() / ((p.get("arquivos") or {}).get("md") or "")   # respondida na espera: md = None
    if md.is_file():
        print(md.read_text(encoding="utf-8"))
    else:
        print(json.dumps(p, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_ouvir(args):
    p = core.obter_pendencia(args.id)
    if not p:
        print(f"Pendência não encontrada: {args.id}")
        return 1
    mp3 = (p.get("arquivos") or {}).get("mp3")
    if not mp3:
        print("Essa pendência não tem áudio.")
        return 1
    ok = tocar_audio(core.vault() / mp3, bloquear=True)
    return 0 if ok else 1


def cmd_responder(args):
    antes = (core.obter_pendencia(args.id) or {}).get("task_id")
    try:
        p = core.responder(args.id, args.resposta, args.nota or "", origem="via terminal")
    except (KeyError, ValueError) as e:
        print(f"Erro: {_msg_erro(e)}")
        return 1
    rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}.get(p["status"], p["status"])
    # só diz "criada" se a tarefa nasceu nesta chamada (re-resposta não cria outra)
    criada = p["_task_criada"] if "_task_criada" in p else bool(p.get("task_id")) and p.get("task_id") != antes
    print(f"{p['id']} → {rotulo}" + (f" · tarefa #{p['task_id']} criada em tasks.yml" if criada and p.get("task_id") else ""))
    return 0


def cmd_responder_texto(args):
    texto = " ".join(args.texto).strip()
    try:
        p = whatsapp.responder_texto(args.id, texto, enviar=not args.sem_enviar, origem="via terminal", nota=args.nota or "")
    except (KeyError, ValueError) as e:
        print(f"Erro: {_msg_erro(e)}")
        return 1
    wa = p.get("whatsapp") or {}
    envio = wa.get("envio") or {}
    if p["status"] == "ignorada":
        print(f"{p['id']} → IGNORADA (nada enviado a {wa.get('contato')})")
    elif p["status"] == "respondida":
        como = "sem envio" if args.sem_enviar else ("envio SIMULADO — EVOLUTION_URL vazio ou WHATSAPP_DRY_RUN" if envio.get("simulado") else "enviado pelo WhatsApp")
        print(f"{p['id']} → RESPONDIDA a {wa.get('contato')} ({como})" + (f" · id {envio.get('message_id')}" if envio.get("message_id") else ""))
    else:
        print(f"{p['id']}: envio falhou ({envio.get('erro')}). Resposta guardada; repita o comando para reenviar.")
        return 1
    return 0


def cmd_whatsapp(args):
    from _shared import lib_whatsapp as lw
    sub = args.acao
    if sub == "status":
        print(json.dumps(whatsapp.status(), ensure_ascii=False, indent=2))
        return 0
    if sub == "qrcode":
        if not lw.configurado():
            print("EVOLUTION_URL / EVOLUTION_API_KEY não configurados no .env")
            return 1
        est = lw.estado_instancia()
        if not est.get("ok") and est.get("status") in (401, 403):
            print(f"A Evolution API recusou a chave (HTTP {est['status']}) — confira EVOLUTION_API_KEY no .env.")
            return 1
        if not est.get("ok") and est.get("status") != 404:   # rede/timeout ou outro erro: não é "instância não existe"
            print(f"Evolution API inacessível em {lw.url_base()}: "
                  f"{est.get('erro') or ('HTTP ' + str(est.get('status')))} — confira se o container está no ar.")
            return 1
        if not est.get("ok"):
            print(f"Instância '{lw.instancia()}' não existe ainda — criando...")
            r = lw.criar_instancia()
            if not r.get("ok"):
                print(f"Falha ao criar instância: {r.get('erro') or r.get('resposta')}")
                return 1
        elif est.get("estado") == "open":
            print(f"Instância '{lw.instancia()}' já está conectada (open). Nada a parear.")
            return 0
        r = lw.qrcode_instancia()
        if not r.get("ok"):
            print(f"Falha ao obter QR code: {r.get('erro') or r.get('resposta')}")
            return 1
        if r.get("pairingCode"):
            print(f"Código de pareamento (WhatsApp → Aparelhos conectados → Conectar com número): {r['pairingCode']}")
        b64 = r.get("base64") or ""
        if b64:
            import base64
            png = core.PROJECT_ROOT / "logs" / "evolution_qr.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(base64.b64decode(b64.split(",", 1)[-1]))
            print(f"QR code salvo em {png} — abra e escaneie em WhatsApp → Aparelhos conectados → Conectar aparelho")
            if shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(png)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif r.get("code"):
            print("Texto do QR (use um gerador de QR ou o Evolution Manager):")
            print(r["code"])
        else:
            print(json.dumps(r.get("resposta"), ensure_ascii=False, indent=2))
        return 0
    if sub == "webhook":
        url = args.arg
        if not url:
            print("Uso: whatsapp webhook http://172.17.0.1:7072/whatsapp/webhook-async  (ou a URL do Webhook do N8N)")
            return 1
        r = lw.configurar_webhook(url, headers={"apikey": lw.api_key()})
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0 if r.get("ok") else 1
    if sub == "simular":
        if not args.arg:
            print("Uso: whatsapp simular caminho/payload.json [--notificar] [--sem-enviar]")
            return 1
        payload = json.loads(Path(args.arg).read_text(encoding="utf-8"))
        r = whatsapp.processar_mensagem_whatsapp(payload, enviar=not args.sem_enviar, notificar=args.notificar,
                                                 atraso_minutos=0 if args.imediato else None)
        r.pop("pendencia", None) if not args.json else None
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    if sub == "fila":
        fila = whatsapp.listar_fila()
        if not fila:
            print("Fila vazia.")
        for i in fila:
            print(f"{i['contato']}  ·  {i['mensagens']} mensagem(ns)  ·  prazo {(i['prazo'] or '')[11:16]} "
                  f"(faltam {i['faltam_min']} min)  ·  {i['remote_jid']}")
            for t in i["textos"]:
                print(f"    “{t}”")
        return 0
    if sub == "processar-fila":
        # com a API no ar a thread dela já faz isso a cada ~60 s — rodar os dois juntos pode duplicar a pendência
        rs = whatsapp.processar_fila(todos=args.todos, enviar=False if args.sem_enviar else None)
        for r in rs:
            p = r.get("pendencia") or {}
            print(f"{r['acao']}: {p.get('id') or (r.get('fila') or {}).get('id')}  {r.get('motivo') or ''}")
        print(f"{len(rs)} item(ns) processado(s); {len(whatsapp.listar_fila())} na fila.")
        return 0
    if sub == "notificar":
        if not args.arg:
            print("Uso: whatsapp notificar <id da pendência>")
            return 1
        try:
            r = whatsapp.notificar_pedro(args.arg)
        except KeyError as e:
            print(f"Erro: {_msg_erro(e)}")
            return 1
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    if sub == "limpar-audios":
        r = whatsapp.limpar_audios(dias=args.dias, simular=args.simular)
        verbo = "seriam apagados" if r["simulado"] else "apagados"
        print(f"{len(r['removidos'])} áudio(s) de resumo {verbo} (> {r['dias']:g} dias, {r['bytes'] / 1e6:.1f} MB); "
              f"{r['mantidos']} mantido(s).")
        for pid in r["removidos"]:
            print(f"  {pid}")
        return 0
    print("Ações: status | qrcode | webhook URL | simular payload.json | notificar ID | fila | processar-fila | limpar-audios")
    return 1


def _notificacao_dbus(titulo: str, corpo: str, acoes: list[tuple[str, str]]) -> Optional[str]:
    """
    Notificação com botões direto no servidor de notificações e espera o clique — via notificar_dbus.py no
    Python do sistema (PyGObject). → chave do botão | "" (fechada sem clicar) | None (sem suporte → fallback).
    Ver o docstring de notificar_dbus.py: notify-send (AppArmor) e gdbus call (Plasma fecha) não servem.
    """
    py = shutil.which("python3", path="/usr/bin:/bin")
    if not py:
        return None
    try:
        out = subprocess.run([py, "-I", str(Path(__file__).parent / "notificar_dbus.py")],
                             input=json.dumps({"titulo": titulo, "corpo": corpo, "acoes": acoes}),
                             capture_output=True, text=True, timeout=60 * 60 * 24 * 7)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _perguntar_gui(titulo: str, corpo: str, tem_audio: bool) -> str:
    """
    Abre UMA pergunta gráfica e bloqueia até a resposta.
    Retorna "vou" | "nao" | "ouvir" | "" (descartada / sem suporte).
    Ordem: notificação com botões (D-Bus) → notify-send → kdialog → zenity.
    """
    semana = 60 * 60 * 24 * 7
    r = _notificacao_dbus(titulo, corpo, [("vou", "Vou"), ("nao", "Não vou")] + ([("ouvir", "Ouvir")] if tem_audio else []))
    if r is not None:
        return r
    if shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=Alfred", "--icon=mail-message-new", "--expire-time=0",
               "--action=vou=Vou", "--action=nao=Não vou"]
        if tem_audio:
            cmd.append("--action=ouvir=Ouvir")
        cmd += [titulo, corpo]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=semana)
            if out.returncode == 0 and "not supported" not in (out.stderr or "").lower():
                return (out.stdout or "").strip()
        except Exception:
            pass
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--title", titulo, "--yesnocancel", corpo,
               "--yes-label", "Vou", "--no-label", "Não vou",
               "--cancel-label", "Ouvir o áudio" if tem_audio else "Depois"]
        try:
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=semana).returncode
            return {0: "vou", 1: "nao", 2: "ouvir" if tem_audio else ""}.get(rc, "")
        except Exception:
            pass
    if shutil.which("zenity"):
        cmd = ["zenity", "--question", "--title", titulo, "--text", corpo,
               "--ok-label", "Vou", "--cancel-label", "Não vou"]
        if tem_audio:
            cmd += ["--extra-button", "Ouvir"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=semana)
            if "Ouvir" in (out.stdout or ""):
                return "ouvir"
            return {0: "vou", 1: "nao"}.get(out.returncode, "")
        except Exception:
            pass
    return ""


def _registrar_escolha(pid: str, escolha: str, origem: str) -> bool:
    """`origem` continua indo na nota (como antes); respondido_via = "notificação no PC".
    Checar e gravar na mesma transação: se outro caminho respondeu no meio, nada muda."""
    try:
        atual = core.responder(pid, "vou" if escolha == "vou" else "nao_vou", origem,
                               origem="via notificação no PC", so_se_aguardando=True)
    except (KeyError, core.PendenciaJaResolvida):
        return False
    core._notify("Alfred", f"Anotado: {'VOU' if escolha == 'vou' else 'NÃO VOU'} — {atual['titulo'][:50]}")
    return True


def cmd_aguardar_resposta(args):
    """
    Interno: fica ocioso numa pergunta gráfica (notify-send com botões → kdialog → zenity)
    até Pedro clicar Vou / Não vou / Ouvir. 'Ouvir' toca o áudio e pergunta de novo.
    Sai em silêncio se a pergunta for descartada ou já tiver sido respondida por outro caminho.
    """
    p = core.obter_pendencia(args.id)
    if not p or p.get("status") != "aguardando":
        return 0
    ev = p.get("evento") or {}
    quando = ev.get("data_inicio") or "data não informada"
    titulo = "Alfred — " + ("Convite" if p["tipo"] == "evento" else "Contato profissional")
    corpo = f"{p['titulo']}\n{quando}" + (f"\n{ev['local']}" if ev.get("local") else "")
    if ev.get("resumo"):
        corpo += f"\n\n{ev['resumo'][:300]}"
    corpo += "\n\nVou ou não vou?"
    mp3 = (p.get("arquivos") or {}).get("mp3")

    for _ in range(4):  # no máximo 4 reaberturas (Ouvir)
        escolha = _perguntar_gui(titulo, corpo, bool(mp3))
        if escolha == "ouvir" and mp3:
            tocar_audio(core.vault() / mp3, bloquear=True)
            continue
        if escolha in ("vou", "nao"):
            _registrar_escolha(args.id, escolha, "via notificação")
        return 0
    return 0


# ── WhatsApp no PC: Enviar (rascunho) / Não enviar / Agenda / Ouvir ─────────

def _menu_whatsapp_gui(titulo: str, corpo: str, tem_audio: bool, audio_tocando: bool = False) -> str:
    """Uma notificação com botões; bloqueia até o clique.
    → "enviar" | "nao" | "agenda" | "ouvir" | "parar" | "" (descartada)."""
    semana = 60 * 60 * 24 * 7
    botao_audio = [("parar", "⏹ Parar áudio")] if audio_tocando else ([("ouvir", "▶ Ouvir")] if tem_audio else [])
    acoes = [("enviar", "Enviar"), ("nao", "Não enviar"), ("agenda", "Agenda")] + botao_audio
    r = _notificacao_dbus(titulo, corpo, acoes)
    if r is not None:
        return r
    if shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=Alfred", "--icon=mail-message-new", "--expire-time=0"]
        cmd += [f"--action={k}={v}" for k, v in acoes] + [titulo, corpo]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=semana)
            if out.returncode == 0 and "not supported" not in (out.stderr or "").lower():
                return (out.stdout or "").strip()
        except Exception:
            pass
    pares = [x for par in acoes for x in par]
    if shutil.which("kdialog"):
        try:
            out = subprocess.run(["kdialog", "--title", titulo, "--menu", corpo] + pares,
                                 capture_output=True, text=True, timeout=semana)
            return (out.stdout or "").strip() if out.returncode == 0 else ""
        except Exception:
            pass
    if shutil.which("zenity"):
        try:
            out = subprocess.run(["zenity", "--list", "--title", titulo, "--text", corpo, "--column", "id",
                                  "--column", "Ação", "--hide-column=1", "--print-column=1"] + pares,
                                 capture_output=True, text=True, timeout=semana)
            return (out.stdout or "").strip() if out.returncode == 0 else ""
        except Exception:
            pass
    return ""


def _caixa_texto_gui(titulo: str, pergunta: str, inicial: str = "", multilinha: bool = True) -> Optional[str]:
    """Caixa de texto (kdialog → zenity) já preenchida com `inicial`. None = cancelada / sem GUI."""
    dia = 60 * 60 * 24
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--title", titulo, "--textinputbox" if multilinha else "--inputbox", pergunta, inicial]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--entry", "--title", titulo, "--text", pergunta, "--entry-text", inicial, "--width=520"]
    else:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=dia)
    except Exception:
        return None
    return out.stdout.rstrip("\n") if out.returncode == 0 else None


def _corpo_whatsapp(p: dict) -> str:
    wa = p.get("whatsapp") or {}
    tipo = "áudio" if wa.get("tipo_msg") == "audio" else (wa.get("tipo_msg") or "mensagem")
    corpo = f"{tipo} · {wa.get('categoria') or ''} · urgência {wa.get('urgencia') or '-'}\n"
    if wa.get("texto"):
        corpo += f"“{wa['texto'][:300]}”\n"
    resumo = (p.get("evento") or {}).get("resumo")
    if resumo:
        corpo += f"\n{resumo[:220]}\n"
    if wa.get("rascunho"):
        corpo += f"\n💡 Sugestão: {wa['rascunho'][:220]}"
    agenda = wa.get("agenda") or {}
    if agenda.get("quando"):
        corpo += f"\n⏰ Lembrete agendado ({agenda.get('titulo')})"
    return corpo


def _responder_whatsapp_gui(pid: str, texto: str, contato: str):
    try:
        e = whatsapp.responder_texto(pid, texto, enviar=True, origem="via notificação no PC")
    except (KeyError, ValueError) as ex:
        core._notify("Alfred", f"Não respondido: {ex}")
        return
    envio = (e.get("whatsapp") or {}).get("envio") or {}
    if e["status"] == "ignorada":
        core._notify("Alfred", f"Ok, não respondo a {contato}.")
    elif e["status"] == "respondida":
        core._notify("Alfred", f"✔ Enviado a {contato}" + (" (simulado)" if envio.get("simulado") else "")
                     + f":\n“{texto[:160]}”")
    else:
        core._notify("Alfred — envio falhou", f"{contato}: {envio.get('erro')}\n"
                     f"A resposta ficou guardada: run.sh responder-texto {pid} \"...\"")


def _agendar_gui(p: dict) -> Optional[dict]:
    """Botão Agenda: pergunta quando → tarefa (tasks.yml) + lembrete + Google Agenda no navegador."""
    pergunta = "Quando? (ex.: 20/10 14:00 · amanhã 10h · terça 15h)"
    inicial = whatsapp.sugestao_quando(p)
    for _ in range(3):
        txt = _caixa_texto_gui("Alfred — anotar na agenda", pergunta, inicial, multilinha=False)
        if txt is None:
            return None
        try:
            q = whatsapp.interpretar_quando(txt)
        except ValueError:   # data inexistente: pergunta de novo em vez de derrubar o aguardador
            q = None
        if q:
            break
        pergunta, inicial = f"Não entendi “{txt}”. Use dia/mês e hora, ex.: 20/10 14:00", txt
    else:
        return None
    try:
        r = whatsapp.agendar_pendencia(p["id"], q[0], q[1])
    except (KeyError, ValueError) as ex:
        core._notify("Alfred", f"Não anotado: {ex}")
        return None
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", r["google_url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    quando = q[0].strftime("%d/%m %H:%M" if q[1] else "%d/%m")
    core._notify("Alfred — anotado na agenda",
                 f"{r['titulo']} — {quando}\nTarefa #{r['task_id']} no tasks.yml · Google Agenda aberto no navegador "
                 f"(clique Salvar) · te lembro aqui nesse horário")
    return r


def _dormir_ate(iso: str, pid: str):
    """Espera até o lembrete; acorda antes se a pendência for respondida por outro caminho."""
    import time
    from datetime import datetime
    try:
        alvo = datetime.fromisoformat(iso)
    except ValueError:
        return
    while True:
        falta = (alvo - core._now()).total_seconds()
        if falta <= 0:
            return
        time.sleep(min(falta, 300))
        p = core.obter_pendencia(pid)
        if not p or p.get("status") != "aguardando":
            return


def _tocar_ao_notificar() -> bool:
    env = os.getenv("INBOX_AUTOPLAY", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    try:
        cfg = whatsapp.carregar_cfg()[1]
        return bool((cfg.get("pendencias") or {}).get("tocar_ao_notificar", True))
    except Exception:
        return True


def cmd_aguardar_agenda(args):
    """Interno: notificação "anotado na agenda" com [Ok] [Desfazer]; ociosa até o clique."""
    import agenda
    item = agenda.obter(args.id)
    if not item:
        return 0
    titulo, corpo = agenda.texto_notificacao(item)
    r = _notificacao_dbus(titulo, corpo, [("ok", "Ok"), ("desfazer", "Desfazer")])
    if r is None:
        core._notify(titulo, corpo + f"\nDesfazer: run.sh agenda desfazer {item['id']}")
        return 0
    if r == "desfazer":
        it = agenda.desfazer(args.id)
        core._notify("Alfred — agenda", f"Desfeito: {it['titulo']}"
                     + (" (sai do calendário na próxima sincronização da extensão)" if it["status"] == "desfazer" else ""))
    return 0


def cmd_aguardar_whatsapp(args):
    """
    Interno: notificação no PC de uma pendência de WhatsApp, ociosa até o clique.
      Enviar     → caixa com o rascunho do Alfred para editar → responder_texto() (sai pela Evolution API)
      Não enviar → fecha como ignorada
      Agenda     → "Quando?" (sugere o compromisso citado) → tarefa em tasks.yml + Google Agenda no navegador
                   + a notificação volta nesse horário (a pendência continua aguardando)
      Ouvir      → toca o áudio da pergunta e mostra de novo
    Sai em silêncio se a notificação for descartada ou a pendência já tiver sido respondida por outro caminho.
    """
    player = None
    primeira = True
    for _ in range(20):
        p = core.obter_pendencia(args.id)
        if not p or p.get("status") != "aguardando":
            parar_audio(player)
            return 0
        wa = p.get("whatsapp") or {}
        contato = wa.get("contato") or p["titulo"]
        mp3 = (p.get("arquivos") or {}).get("mp3")
        mp3_ok = bool(mp3) and (core.vault() / mp3).exists()
        # o resumo em áudio começa junto com a notificação (perfis.yml: pendencias.tocar_ao_notificar)
        if primeira and mp3_ok and _tocar_ao_notificar():
            player = iniciar_audio(core.vault() / mp3)
        primeira = False
        escolha = _menu_whatsapp_gui(f"Alfred — WhatsApp de {contato}", _corpo_whatsapp(p), mp3_ok,
                                     audio_tocando=tocando(player))
        if escolha != "parar" and escolha != "ouvir":
            parar_audio(player)
        p = core.obter_pendencia(args.id)
        if not p or p.get("status") != "aguardando":
            parar_audio(player)
            return 0
        if escolha == "parar":
            parar_audio(player)
            continue
        if escolha == "ouvir" and mp3_ok:
            parar_audio(player)
            player = iniciar_audio(core.vault() / mp3)
            continue
        if escolha == "enviar":
            texto = _caixa_texto_gui(f"Alfred — resposta para {contato}",
                                     "Edite e confirme — vai pelo WhatsApp (Cancelar volta à notificação):",
                                     wa.get("rascunho") or "")
            if texto and texto.strip():
                _responder_whatsapp_gui(args.id, texto.strip(), contato)
                return 0
            continue
        if escolha == "nao":
            _responder_whatsapp_gui(args.id, "ignorar", contato)
            return 0
        if escolha == "agenda":
            r = _agendar_gui(p)
            if r:
                _dormir_ate(r["lembrar_em"], args.id)
                # a thread da API (disparar_lembretes) também lembra nesse horário: só um dos dois mostra
                if not whatsapp.marcar_lembrete(args.id, r["lembrar_em"]):
                    parar_audio(player)
                    return 0
            continue
        return 0
    return 0


def _perguntar_whatsapp(p: dict, ouvir: bool) -> str:
    """Pendência de WhatsApp no modo interativo: Alfred mostra a mensagem e espera o texto da resposta."""
    wa = p.get("whatsapp") or {}
    print(f"  de: {wa.get('contato')} ({wa.get('numero')})   {wa.get('tipo_msg')} · {wa.get('categoria')} · urgência {wa.get('urgencia')}")
    if wa.get("texto"):
        print("  ❝ " + wa["texto"][:400].replace("\n", "\n    ") + " ❞")
    if ouvir and (p.get("arquivos") or {}).get("mp3"):
        tocar_audio(core.vault() / p["arquivos"]["mp3"], bloquear=True)
    while True:
        try:
            r = input("  O que você quer responder? (texto | ignorar | ouvir | ver | pular | sair) › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "sair"
        low = r.lower()
        if low in ("sair", "q", "quit"):
            return "sair"
        if low in ("pular", "p", "", "depois"):
            return "pular"
        if low in ("ouvir", "o") and (p.get("arquivos") or {}).get("mp3"):
            tocar_audio(core.vault() / p["arquivos"]["mp3"], bloquear=True)
            continue
        if low in ("ver", "v"):
            md = core.vault() / ((p.get("arquivos") or {}).get("md") or "")
            print(md.read_text(encoding="utf-8") if md.is_file() else "(sem texto)")
            continue
        try:
            e = whatsapp.responder_texto(p["id"], r, enviar=True, origem="via terminal")
        except (KeyError, ValueError) as ex:
            print(f"  erro: {ex}")
            continue
        envio = (e.get("whatsapp") or {}).get("envio") or {}
        if e["status"] == "ignorada":
            print("  ✔ ignorada")
        elif e["status"] == "respondida":
            print("  ✔ enviado" + (" (simulado)" if envio.get("simulado") else ""))
        else:
            print(f"  ✖ envio falhou: {envio.get('erro')} — resposta guardada, repita depois")
        return "ok"


def cmd_perguntar(args):
    """
    Modo interativo no terminal: Alfred apresenta cada pendência (e toca o áudio, se
    pedido) e espera a resposta. Funciona em qualquer lugar — SSH, tty, sem GUI.
    """
    pend = core.listar_pendencias("aguardando")
    if not pend:
        print("Nenhuma pendência aguardando resposta.")
        return 0
    print(f"\n{len(pend)} pendência(s) aguardando. Para cada uma: vou | nao | ouvir | ver | pular | sair\n")
    for p in pend:
        ev = p.get("evento") or {}
        print("─" * 90)
        print(f"[{p['tipo']}] {p['titulo']}")
        if p.get("tipo") == "whatsapp":
            if _perguntar_whatsapp(p, args.ouvir) == "sair":
                return 0
            continue
        print(f"  quando: {ev.get('data_inicio') or 'não informado'}   local: {ev.get('local') or ('online' if ev.get('online') else 'não informado')}")
        if ev.get("resumo"):
            print(f"  {ev['resumo']}")
        if args.ouvir and (p.get("arquivos") or {}).get("mp3"):
            tocar_audio(core.vault() / p["arquivos"]["mp3"], bloquear=True)
        while True:
            try:
                r = input("  Vou ou não vou? › ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if r in ("sair", "q", "quit"):
                return 0
            if r in ("pular", "p", "", "depois"):
                break
            if r in ("ouvir", "o") and (p.get("arquivos") or {}).get("mp3"):
                tocar_audio(core.vault() / p["arquivos"]["mp3"], bloquear=True)
                continue
            if r in ("ver", "v"):
                md = core.vault() / ((p.get("arquivos") or {}).get("md") or "")
                print(md.read_text(encoding="utf-8") if md.is_file() else "(sem texto)")
                continue
            if r in ("vou", "sim", "s", "v+", "nao", "não", "n", "nao vou", "não vou"):
                escolha = "vou" if r in ("vou", "sim", "s", "v+") else "nao"
                try:
                    nota = input("  Nota (opcional) › ").strip()
                except EOFError:   # stdin acabou: grava a resposta sem nota
                    print()
                    nota = ""
                core.responder(p["id"], "vou" if escolha == "vou" else "nao_vou", nota or "via terminal",
                               origem="via terminal")
                print(f"  ✔ anotado: {'VOU' if escolha == 'vou' else 'NÃO VOU'}")
                break
            print("  responda: vou | nao | ouvir | ver | pular | sair")
    print("─" * 90)
    return 0


# ── Main ──────────────────────────────────────────────────────

SUBCOMANDOS = ("ciclo", "scan", "jornal", "pendencias", "perguntar", "ver", "ouvir", "responder", "responder-texto",
               "whatsapp", "semanal", "limpeza", "agenda", "vigia", "_aguardar-resposta", "_aguardar-whatsapp",
               "_aguardar-agenda", "_vigia-qr")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Alfred Inbox Assistant")
    sub = ap.add_subparsers(dest="cmd")

    def _opts_ciclo(sp):
        sp.add_argument("--inbox", default=None, help="nome da inbox em perfis.yml (default: INBOX_DEFAULT)")
        sp.add_argument("--desde", type=int, default=None, help="janela em horas (default: perfis.yml)")
        sp.add_argument("--reprocessar", action="store_true", help="ignora logs/inbox_seen.json")
        sp.add_argument("--max", type=int, default=None, help="máximo de e-mails")
        sp.add_argument("--sem-notificar", action="store_true", help="não dispara notify-send")
        sp.add_argument("--json", action="store_true", help="imprime o resumo em JSON")

    for nome in ("ciclo", "scan", "jornal"):
        _opts_ciclo(sub.add_parser(nome))

    sp = sub.add_parser("pendencias"); sp.add_argument("--todas", action="store_true")
    sp = sub.add_parser("perguntar"); sp.add_argument("--ouvir", action="store_true", help="toca o áudio de cada pendência antes de perguntar")
    sp = sub.add_parser("ver"); sp.add_argument("id")
    sp = sub.add_parser("ouvir"); sp.add_argument("id")
    sp = sub.add_parser("responder"); sp.add_argument("id"); sp.add_argument("resposta"); sp.add_argument("--nota", default="")
    sp = sub.add_parser("responder-texto", help="pendência de WhatsApp: registra o texto e envia ao contato")
    sp.add_argument("id"); sp.add_argument("texto", nargs="+", help="texto da resposta (ou 'ignorar')")
    sp.add_argument("--sem-enviar", action="store_true", help="só registra, não chama a Evolution API")
    sp.add_argument("--nota", default="")
    sp = sub.add_parser("whatsapp", help="Evolution API: status | qrcode | webhook URL | simular payload.json | "
                                         "notificar ID | fila | processar-fila")
    sp.add_argument("acao", choices=("status", "qrcode", "webhook", "simular", "notificar", "fila", "processar-fila",
                                     "limpar-audios"))
    sp.add_argument("--dias", type=float, default=None, help="limpar-audios: idade mínima (default: audio_reter_dias)")
    sp.add_argument("--simular", action="store_true", help="limpar-audios: só lista o que seria apagado")
    sp.add_argument("arg", nargs="?", default=None)
    sp.add_argument("--notificar", action="store_true", help="simular: o Python manda o resumo a Pedro (sem N8N)")
    sp.add_argument("--sem-enviar", action="store_true", help="simular / processar-fila: nunca chama a Evolution API")
    sp.add_argument("--json", action="store_true", help="simular: inclui a pendência inteira na saída")
    sp.add_argument("--imediato", action="store_true", help="simular: ignora atraso_minutos (pendência na hora)")
    sp.add_argument("--todos", action="store_true", help="processar-fila: não espera o prazo vencer")
    sp = sub.add_parser("semanal", aliases=["podcast"],
                        help="podcast do Alfred (a cada N dias): notícias do período, roteiro em diálogo, .md/.mp3/manifesto")
    sp.add_argument("--semana", default=None, help="modo antigo: semana ISO AAAA-Www")
    sp.add_argument("--dias", type=int, default=None, help="tamanho do período (default: podcast.intervalo_dias = 3)")
    sp.add_argument("--se-devido", action="store_true",
                    help="só gera se o último episódio tiver sido gerado há --dias dias ou mais (usado pelo timer)")
    sp.add_argument("--inbox", default=None)
    sp.add_argument("--sem-audio", action="store_true", help="só roteiro e .md (sem edge-tts)")
    sp.add_argument("--dry-run", action="store_true", help="só mostra a seleção de notícias (nada gravado)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("indexar", help="reindexa os cofres em alfred.db (SQLite FTS5) e poda o histórico do WhatsApp")
    sp = sub.add_parser("buscar", help="busca FTS5: notas dos cofres (padrão) ou --whatsapp (histórico das conversas)")
    sp.add_argument("consulta", nargs="+"); sp.add_argument("--whatsapp", action="store_true")
    sp.add_argument("--cofre", default=None, help="alfred | pedro"); sp.add_argument("--max", type=int, default=5)
    sp = sub.add_parser("_aguardar-resposta"); sp.add_argument("id")
    import limpeza
    limpeza.adicionar_parser(sub)
    sp = sub.add_parser("_aguardar-whatsapp"); sp.add_argument("id")
    import agenda
    agenda.adicionar_parser(sub)
    import vigia
    vigia.adicionar_parser(sub)
    return ap


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["ciclo"] + argv
    args = build_parser().parse_args(argv)

    if args.cmd == "ciclo":
        return cmd_ciclo(args)
    if args.cmd == "scan":
        return cmd_ciclo(args, dry_run=True)
    if args.cmd == "jornal":
        return cmd_ciclo(args, so_jornal=True)
    if args.cmd == "indexar":
        print(json.dumps({"indice": core.indexar_cofres(), "db": lib_db.estatisticas()}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "buscar":
        return cmd_buscar(args)
    if args.cmd == "limpeza":
        import limpeza
        return limpeza.cmd_limpeza(args)
    if args.cmd in ("semanal", "podcast"):
        import semanal
        if args.dias is not None and args.dias < 1:
            print(f"Erro: --dias precisa ser 1 ou mais (recebi {args.dias}).")
            return 2
        try:
            return semanal.cmd_semanal(args)
        except ValueError as e:   # semana/dias inválidos: mensagem, sem traceback
            print(f"Erro: {_msg_erro(e)}")
            return 2
    if args.cmd == "pendencias":
        return cmd_pendencias(args)
    if args.cmd == "perguntar":
        return cmd_perguntar(args)
    if args.cmd == "ver":
        return cmd_ver(args)
    if args.cmd == "ouvir":
        return cmd_ouvir(args)
    if args.cmd == "responder":
        return cmd_responder(args)
    if args.cmd == "responder-texto":
        return cmd_responder_texto(args)
    if args.cmd == "whatsapp":
        return cmd_whatsapp(args)
    if args.cmd == "_aguardar-resposta":
        return cmd_aguardar_resposta(args)
    if args.cmd == "_aguardar-whatsapp":
        return cmd_aguardar_whatsapp(args)
    if args.cmd == "agenda":
        import agenda
        return agenda.cmd_agenda(args)
    if args.cmd == "_aguardar-agenda":
        return cmd_aguardar_agenda(args)
    if args.cmd == "vigia":
        import vigia
        return vigia.cmd_vigia(args)
    if args.cmd == "_vigia-qr":
        import vigia
        return vigia.aguardar_qr(args.estado)
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log(f"[ERRO] {e}")
        raise
