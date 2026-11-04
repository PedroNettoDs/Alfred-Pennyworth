"""
Avaliação Skills — tokens, voltas e tempo do gemma4:12b resolvendo um pedido com e sem skill.

Braços (mesmo pedido, mesmo modelo, mesmo num_ctx da Conversa):
    zero               personalidade (docs/alfred-system-prompt.md) + as 35 ferramentas; o modelo planeja do zero
    skill              personalidade + corpo da skill certa no system + as 35 ferramentas
    skill_ferramentas  personalidade + corpo da skill + só as ferramentas listadas no frontmatter da skill

As ferramentas NÃO rodam: cada chamada recebe uma saída sintética fixa (simular()), igual nos três braços. Nada
toca caixa de e-mail, WhatsApp, cofres nem o executor. O loop imita conversa._turno: até 6 voltas com tools,
depois uma chamada sem tools. O system começa com um nonce: a 1ª volta sempre avalia o prompt inteiro (sem
reaproveitar o cache de prefixo de outro braço); as voltas seguintes reaproveitam, como num turno real.

Mede por braço: voltas, chamadas de ferramenta, tokens avaliados (soma de prompt_eval_count), tokens gerados
(eval_count), tempo, carga de modelo (ruído da produção/outras avaliações), cobertura dos passos da skill e
comandos que o executor recusaria (is_allowed do services/shell-executor/main.py).

Uso (da raiz do repositório; o venv do executor tem o fastapi que o main.py importa). Um flock por pedido,
para não segurar a GPU das outras avaliações por muito tempo:
    PY=services/shell-executor/venv/bin/python
    flock /tmp/alfred-bench-gpu.lock $PY docs/avaliacoes/scripts/skills_tokens.py calibrar
    for i in 1 4 7 9 11 14 17 19 22 25; do
        flock /tmp/alfred-bench-gpu.lock $PY docs/avaliacoes/scripts/skills_tokens.py rodar --id $i --saida /tmp/tokens.jsonl
    done
    $PY docs/avaliacoes/scripts/skills_tokens.py resumo /tmp/tokens.jsonl
"""

import argparse
import importlib.util
import json
import re
import secrets
import statistics
import time
from pathlib import Path

import skills_seletor as sel

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parents[2]
MAX_VOLTAS = 6
BRACOS = ["zero", "skill", "skill_ferramentas"]
PERIGOSAS = {"executar_comando", "responder_pendencia", "responder_limpeza", "completar_tarefa",
             "criar_compromisso", "salvar_nota"}

# Saídas sintéticas, sem dado pessoal. Mesmo texto para os três braços.
_LOG = "\n".join(["Nov 03 08:00:01 run.sh[2259]: INFO:     127.0.0.1 - \"GET /api/painel HTTP/1.1\" 200 OK"] * 6 + [
    "Nov 03 08:01:10 run.sh[2259]: Traceback (most recent call last):",
    "Nov 03 08:01:10 run.sh[2259]:   File \"core.py\", line 812, in coletar",
    "Nov 03 08:01:10 run.sh[2259]: sqlite3.OperationalError: database is locked",
    "Nov 03 08:01:11 systemd[1958]: alfred-inbox.service: Main process exited, code=exited, status=1/FAILURE"])
_NVIDIA = ("| NVIDIA GeForce RTX 3060 | 11634MiB / 12288MiB | 41% | 62C |\n"
           "| PID 3120 ollama 8402MiB | PID 2201 laya 0MiB | PID 1980 plasmashell 410MiB |")
_OLLAMA_PS = ("NAME          ID            SIZE     PROCESSOR    CONTEXT    UNTIL\n"
              "gemma4:12b    6c1b3f2a9d10  8.4 GB   100% GPU     16384      4 minutes from now\n"
              "qwen3.6:35b-a3b 1f2e3d4c5b6a 22 GB  55%/45% CPU/GPU 8192   2 minutes from now")
_DF = ("Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme0n1p2  915G  861G   54G  95% /\n"
       "/dev/nvme0n1p2  915G  861G   54G  95% /home")
_DOCKER = ("NAMES                STATUS                          PORTS\nalfred-evolution     Restarting (1) 12 seconds ago\n"
           "alfred-evolution-db  Up 6 hours                      5432/tcp\nalfred-chromadb      Up 6 hours                      "
           "0.0.0.0:8001->8000/tcp\nalfred-searxng       Up 6 hours                      0.0.0.0:8889->8080/tcp")


def _comando(cmd: str) -> str:
    c = cmd.strip()
    regras = [
        (r"^systemctl --user is-active", "failed"),
        (r"^systemctl --user status", "× alfred-inbox.service - Alfred Inbox\n     Active: failed (Result: exit-code) since Mon 08:01:11"),
        (r"^journalctl", _LOG),
        (r"^curl .*health", "curl: (7) Failed to connect to 127.0.0.1 port 7073: Connection refused"),
        (r"^curl .*keep_alive", "{\"model\":\"qwen3.6:35b-a3b\",\"done\":true,\"done_reason\":\"unload\"}"),
        (r"^nvidia-smi", _NVIDIA),
        (r"^ollama ps", _OLLAMA_PS),
        (r"^ollama list", "gemma4:12b 8.0 GB\ngemma4:e4b 6.6 GB\nqwen3.6:35b-a3b 22 GB\nbge-m3:latest 1.2 GB"),
        (r"^df", _DF),
        (r"^du -sh", "212G\t/home/pedro/.ollama/models"),
        (r"^docker system df", "TYPE    TOTAL  ACTIVE  SIZE    RECLAIMABLE\nImages  31     9       84.2GB  51.7GB (61%)\n"
                               "Build Cache 212 0 23.4GB 23.4GB"),
        (r"^docker ps", _DOCKER),
        (r"^docker logs", "\n".join(["[Evolution] ERROR Connection Closed: restart required"] * 4)),
        (r"^docker stats", "alfred-evolution 0.00% 0B / 0B\nalfred-chromadb 0.4% 310MiB / 31GiB"),
    ]
    for padrao, saida in regras:
        if re.search(padrao, c):
            return saida
    return "[comando executado sem saída, rc=0]"


def simular(nome: str, args: dict) -> str:
    fixos = {
        "status_gpu": _NVIDIA, "status_disco": _DF,
        "relatorio_sistema": f"## GPU\n{_NVIDIA}\n\n## Disco\n{_DF}\n\n## Docker\n{_DOCKER}",
        "ver_agenda": "Hoje 15:00 Reunião com fornecedor (online)\nAmanhã 09:30 Dentista",
        "listar_pendencias": "3 pendências aguardando:\n[p1] e-mail · Fornecedor A · orçamento do servidor · 2 dias\n"
                             "[p2] WhatsApp · Contato B · confirmar horário · 5 h\n[p3] e-mail · Banco · documento pendente · 4 dias",
        "ver_pendencia": "[p1] e-mail de Fornecedor A: pede aprovação do orçamento de R$ 4.300 até sexta.",
        "tarefas_vencidas": "1 tarefa vencida: pagar conta de luz (venceu ontem)",
        "resumo_do_dia": "Resumo: 2 compromissos, 3 pendências, 1 tarefa vencida.",
        "conversa_com": "Últimas 5 mensagens: combinaram revisar o contrato; ficou de enviar a planilha até quinta.",
        "buscar_mensagens": "2 mensagens encontradas sobre o assunto (12/10 e 28/10).",
        "emails_recentes": "2 e-mails do contato nos últimos 30 dias: proposta revisada; convite da reunião.",
        "buscar_memoria": "Fato: prefere reuniões de manhã. Fato: contrato renova em dezembro.",
        "buscar_cofre": "Nenhuma nota encontrada sobre o tema.",
        "pesquisar": "Pesquisa concluída: 12 fontes. Nota salva em research/tema-pesquisado/index.md.",
        "andamento_pesquisa": "Job em andamento: 3 de 5 subperguntas.",
        "listar_limpezas": "1 lote aguardando: [L7] 42 e-mails (promoção 30, transacional 12).",
    }
    if nome == "executar_comando":
        return _comando(str(args.get("comando", "")))
    if nome in PERIGOSAS:
        return f"[não executada] {nome} precisa da aprovação de Pedro (simulação)."
    return fixos.get(nome, "[simulado] nenhum resultado.")


def _executor():
    spec = importlib.util.spec_from_file_location("executor_main", RAIZ / "services" / "shell-executor" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def personalidade() -> str:
    texto = (RAIZ / "docs" / "alfred-system-prompt.md").read_text()
    return "\n".join(l for l in texto.splitlines() if l.strip() != "/no_think").strip()


def _chat(msgs, tools):
    corpo = {"model": sel.MODELO, "messages": msgs, "stream": False, "think": False,
             "options": {"num_ctx": sel.NUM_CTX, "temperature": 0}}
    if tools:
        corpo["tools"] = tools
    return sel._post(f"{sel.OLLAMA}/api/chat", corpo, timeout=600)


def rodar_braco(braco: str, pedido: dict, skill: dict, esquemas: list, executor) -> dict:
    sistema = f"[sessão {secrets.token_hex(4)}]\n\n{personalidade()}"
    tools = esquemas
    if braco != "zero":
        sistema += f"\n\n## Procedimento a seguir (skill {skill['nome']})\n\n{skill['corpo']}"
    if braco == "skill_ferramentas":
        nomes = set(_lista(skill["ferramentas"]))
        tools = [e for e in esquemas if e["function"]["name"] in nomes]
    msgs = [{"role": "system", "content": sistema}, {"role": "user", "content": pedido["pedido"]}]
    r = {"id": pedido["id"], "skill": skill["nome"], "braco": braco, "n_tools": len(tools), "voltas": 0,
         "chamadas": [], "comandos": [], "tokens_avaliados": 0, "tokens_gerados": 0, "prompt_volta1": 0,
         "ms": 0.0, "carga_ms": 0.0}
    t0 = time.perf_counter()
    for volta in range(MAX_VOLTAS + 1):
        resp = _chat(msgs, tools if volta < MAX_VOLTAS else None)
        r["voltas"] += 1
        r["tokens_avaliados"] += resp.get("prompt_eval_count") or 0
        r["tokens_gerados"] += resp.get("eval_count") or 0
        r["carga_ms"] += (resp.get("load_duration") or 0) / 1e6
        if volta == 0:
            r["prompt_volta1"] = resp.get("prompt_eval_count") or 0
        msg = resp.get("message") or {}
        chamadas = msg.get("tool_calls") or []
        if not chamadas:
            r["resposta_chars"] = len(msg.get("content") or "")
            break
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": chamadas})
        for c in chamadas:
            nome = c["function"]["name"]
            args = c["function"].get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            r["chamadas"].append(nome)
            if nome == "executar_comando":
                cmd = str(args.get("comando", ""))
                r["comandos"].append({"cmd": cmd, "permitido": executor.is_allowed(cmd)})
            msgs.append({"role": "tool", "content": simular(nome, args), "tool_name": nome})
    r["ms"] = round((time.perf_counter() - t0) * 1000)
    r["carga_ms"] = round(r["carga_ms"])
    r["cobertura"] = cobertura(skill, r)
    return r


# Passos que a skill exige com as saídas sintéticas acima (os condicionais, como o curl /health de um serviço
# que já aparece "failed", não contam). Cada padrão casa com o nome de uma ferramenta ou com um comando shell.
ESSENCIAIS = {
    "diagnosticar-servico": [r"^systemctl --user (is-active|status)", r"^journalctl --user"],
    "vram-ollama": [r"^nvidia-smi|^status_gpu$|^relatorio_sistema$", r"^ollama ps"],
    "liberar-vram": [r"^ollama ps", r"^curl .*keep_alive"],
    "espaco-em-disco": [r"^df |^status_disco$|^relatorio_sistema$", r"^docker system df", r"^du -sh"],
    "containers-docker": [r"^docker ps", r"^docker logs"],
    "resumo-da-manha": [r"^ver_agenda$", r"^listar_pendencias$", r"^tarefas_vencidas$"],
    "preparar-conversa": [r"^conversa_com$|^buscar_mensagens$", r"^emails_recentes$", r"^buscar_memoria$", r"^ver_agenda$"],
    "pesquisa-para-o-cofre": [r"^buscar_cofre$", r"^pesquisar$"],
    "triagem-de-pendencias": [r"^listar_pendencias$", r"^ver_pendencia$"],
    "limpeza-da-caixa": [r"^listar_limpezas$"],
}


def cobertura(skill: dict, r: dict) -> float:
    feitos = [c["cmd"].strip() for c in r["comandos"]] + r["chamadas"]
    alvo = ESSENCIAIS[skill["nome"]]
    return round(sum(1 for p in alvo if any(re.search(p, f) for f in feitos)) / len(alvo), 2)


def _lista(valor: str) -> list[str]:
    """"[a, b]" do frontmatter → ["a", "b"]."""
    return [x.strip() for x in valor.strip().strip("[]").split(",") if x.strip()]


def carregar() -> tuple[dict, list]:
    skills = {}
    for s in sel.carregar_skills(AQUI / "skills_proto" / "skills"):
        skills[s["nome"]] = s
    esquemas = json.loads((AQUI / "skills_proto" / "esquemas.json").read_text())
    return skills, esquemas


def calibrar() -> None:
    """O nonce no system invalida o cache das ferramentas também? (= as tools entram depois do system)"""
    _, esquemas = carregar()
    pers = personalidade()
    contagens = []
    for _ in range(3):
        msgs = [{"role": "system", "content": f"[sessão {secrets.token_hex(4)}]\n\n{pers}"},
                {"role": "user", "content": "oi"}]
        resp = sel._post(f"{sel.OLLAMA}/api/chat", {"model": sel.MODELO, "messages": msgs, "tools": esquemas,
                                                     "stream": False, "think": False,
                                                     "options": {"num_ctx": sel.NUM_CTX, "num_predict": 1}}, timeout=600)
        contagens.append((resp.get("prompt_eval_count"), round((resp.get("load_duration") or 0) / 1e6)))
    sem = sel._post(f"{sel.OLLAMA}/api/chat", {"model": sel.MODELO, "stream": False, "think": False,
                                               "messages": [{"role": "system", "content": f"[x{secrets.token_hex(4)}]\n\n{pers}"},
                                                            {"role": "user", "content": "oi"}],
                                               "options": {"num_ctx": sel.NUM_CTX, "num_predict": 1}}, timeout=600)
    print(json.dumps({"com_35_tools_(prompt_eval, carga_ms)": contagens,
                      "sem_tools_prompt_eval": sem.get("prompt_eval_count")}))


def resumo(arq: Path) -> None:
    linhas = [json.loads(l) for l in arq.read_text().splitlines() if l.strip()]
    print("| Braço | Pedidos | Ferramentas no prompt | Prompt da 1ª volta (tokens) | Tokens avaliados | Tokens gerados "
          "| Voltas | Chamadas | Cliques de aprovação | Tempo (s) | Tempo sem carga de modelo (s) | Cobertura dos passos "
          "| Comandos recusados pelo executor |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    base = {}
    for b in BRACOS:
        rs = [r for r in linhas if r["braco"] == b]
        if not rs:
            continue
        m = lambda k: statistics.mean(r[k] for r in rs)  # noqa: E731
        cmds = [c for r in rs for c in r["comandos"]]
        recus = sum(1 for c in cmds if not c["permitido"])
        base.setdefault("avaliados", m("tokens_avaliados"))
        base.setdefault("ms", m("ms"))
        liquido = statistics.mean(r["ms"] - r["carga_ms"] for r in rs)
        base.setdefault("liquido", liquido)
        print(f"| {b} | {len(rs)} | {m('n_tools'):.0f} | {m('prompt_volta1'):.0f} | {m('tokens_avaliados'):.0f} "
              f"({m('tokens_avaliados') / base['avaliados'] - 1:+.0%}) | {m('tokens_gerados'):.0f} | {m('voltas'):.1f} | "
              f"{statistics.mean(len(r['chamadas']) for r in rs):.1f} | "
              f"{statistics.mean(sum(1 for c in r['chamadas'] if c in PERIGOSAS) for r in rs):.1f} | {m('ms') / 1000:.1f} ({m('ms') / base['ms'] - 1:+.0%}) | "
              f"{liquido / 1000:.1f} ({liquido / base['liquido'] - 1:+.0%}) | "
              f"{m('cobertura'):.0%} | {recus}/{len(cmds)} |")
    cargas = [r["carga_ms"] for r in linhas]
    print(f"\nruído: {sum(1 for c in cargas if c > 1000)}/{len(cargas)} execuções pagaram carga de modelo > 1 s "
          f"(total {sum(cargas) / 1000:.0f} s)")
    print("\npor pedido (tokens avaliados zero → skill → skill_ferramentas; cobertura):")
    for i in sorted({r["id"] for r in linhas}):
        rs = {r["braco"]: r for r in linhas if r["id"] == i}
        print(f"  {i} {rs[BRACOS[0]]['skill'] if BRACOS[0] in rs else ''}: "
              + " → ".join(f"{rs[b]['tokens_avaliados']} ({rs[b]['cobertura']:.0%}, {len(rs[b]['chamadas'])} ch)"
                           for b in BRACOS if b in rs))
    recusados = sorted({c["cmd"] for r in linhas for c in r["comandos"] if not c["permitido"]})
    if recusados:
        print("\ncomandos recusados:", *recusados, sep="\n  ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("acao", choices=["calibrar", "rodar", "resumo"])
    ap.add_argument("arquivo", nargs="?", type=Path)
    ap.add_argument("--id", type=int)
    ap.add_argument("--saida", type=Path)
    ap.add_argument("--rotacao", type=int, help="qual braço vai primeiro (0, 1 ou 2); padrão: id %% 3")
    a = ap.parse_args()
    if a.acao == "calibrar":
        return calibrar()
    if a.acao == "resumo":
        return resumo(a.arquivo)
    skills, esquemas = carregar()
    pedido = next(p for p in json.loads((AQUI / "skills_proto" / "pedidos.json").read_text()) if p["id"] == a.id)
    executor = _executor()
    k = (a.id if a.rotacao is None else a.rotacao) % 3
    ordem = BRACOS[k:] + BRACOS[:k]     # alterna a ordem dos braços entre pedidos (o 1º paga a carga de modelo)
    # retomável: braço já gravado para este pedido não roda de novo (a máquina reiniciou no meio duas vezes)
    feitos = {(r["id"], r["braco"]) for r in map(json.loads, a.saida.read_text().splitlines())
              if r} if a.saida.exists() else set()
    with a.saida.open("a") as f:
        for b in ordem:
            if (a.id, b) in feitos:
                continue
            r = rodar_braco(b, pedido, skills[pedido["skill"]], esquemas, executor)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{a.id} {b}: {r['tokens_avaliados']} tok, {r['voltas']} voltas, {r['chamadas']}, {r['ms']} ms, "
                  f"cobertura {r['cobertura']:.0%}")


if __name__ == "__main__":
    main()
