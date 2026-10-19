#!/usr/bin/env python3
"""
Alfred Inbox Assistant — CLI.

Uso:
    inbox-assistant.py                         → ciclo completo (inbox padrão do .env)
    inbox-assistant.py ciclo [--inbox NOME] [--desde HORAS] [--reprocessar] [--max N] [--sem-notificar]
    inbox-assistant.py scan  [--desde HORAS]   → só classifica e mostra (nada é gravado)
    inbox-assistant.py jornal [--desde HORAS]  → só a edição do dia (newsletters)
    inbox-assistant.py pendencias [--todas]    → fila de "vou / não vou"
    inbox-assistant.py perguntar [--ouvir]     → modo interativo: Alfred pergunta e espera a resposta no terminal
    inbox-assistant.py ver ID                  → texto explicativo da pendência
    inbox-assistant.py ouvir ID                → toca o áudio da pergunta
    inbox-assistant.py responder ID vou|nao-vou|ignorar [--nota "..."]

Saída final do ciclo: linhas JORNAL_FILE=... e PENDENCIA_NOVA=... (uma por pendência),
fáceis de consumir por shell/N8N.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import load_env, log  # noqa: E402

load_env(override=False)  # .env da raiz; variáveis já exportadas (run.sh, testes) têm prioridade

import core  # noqa: E402
from _shared.lib_tts import tocar_audio  # noqa: E402


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


def cmd_pendencias(args):
    status = "todas" if args.todas else "aguardando"
    pend = core.listar_pendencias(status)
    if not pend:
        print("Nenhuma pendência" + ("" if args.todas else " aguardando resposta") + ".")
        return 0
    print(f"\n{'id':<42} {'status':<10} {'tipo':<8} {'quando':<17} título")
    print("-" * 110)
    for p in pend:
        quando = (p.get("evento") or {}).get("data_inicio") or "-"
        print(f"{p['id']:<42} {p['status']:<10} {p['tipo']:<8} {str(quando)[:16]:<17} {p['titulo'][:40]}")
    print("\nResponder: inbox-assistant.py responder <id> vou | nao-vou   ·   Ouvir: inbox-assistant.py ouvir <id>\n")
    return 0


def cmd_ver(args):
    p = core.obter_pendencia(args.id)
    if not p:
        print(f"Pendência não encontrada: {args.id}")
        return 1
    md = core.vault() / (p.get("arquivos") or {}).get("md", "")
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
    try:
        p = core.responder(args.id, args.resposta, args.nota or "")
    except (KeyError, ValueError) as e:
        print(f"Erro: {e}")
        return 1
    rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}.get(p["status"], p["status"])
    print(f"{p['id']} → {rotulo}" + (f" · tarefa #{p['task_id']} criada em tasks.yml" if p.get("task_id") else ""))
    return 0


def _perguntar_gui(titulo: str, corpo: str, tem_audio: bool) -> str:
    """
    Abre UMA pergunta gráfica e bloqueia até a resposta.
    Retorna "vou" | "nao" | "ouvir" | "" (descartada / sem suporte).
    Ordem: notify-send com botões → kdialog → zenity.
    """
    semana = 60 * 60 * 24 * 7
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
    atual = core.obter_pendencia(pid)
    if not atual or atual.get("status") != "aguardando":
        return False
    core.responder(pid, "vou" if escolha == "vou" else "nao_vou", origem)
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
                md = core.vault() / (p.get("arquivos") or {}).get("md", "")
                print(md.read_text(encoding="utf-8") if md.is_file() else "(sem texto)")
                continue
            if r in ("vou", "sim", "s", "v+", "nao", "não", "n", "nao vou", "não vou"):
                escolha = "vou" if r in ("vou", "sim", "s", "v+") else "nao"
                nota = input("  Nota (opcional) › ").strip()
                core.responder(p["id"], "vou" if escolha == "vou" else "nao_vou", nota or "via terminal")
                print(f"  ✔ anotado: {'VOU' if escolha == 'vou' else 'NÃO VOU'}")
                break
            print("  responda: vou | nao | ouvir | ver | pular | sair")
    print("─" * 90)
    return 0


# ── Main ──────────────────────────────────────────────────────

SUBCOMANDOS = ("ciclo", "scan", "jornal", "pendencias", "perguntar", "ver", "ouvir", "responder", "_aguardar-resposta")


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
    sp = sub.add_parser("_aguardar-resposta"); sp.add_argument("id")
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
    if args.cmd == "_aguardar-resposta":
        return cmd_aguardar_resposta(args)
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
