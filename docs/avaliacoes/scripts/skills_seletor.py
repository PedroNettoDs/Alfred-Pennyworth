"""
Avaliação Skills — protótipo descartável de seletor: qual skill (ou nenhuma) atende cada pedido de teste.

Seletores:
    lexico  fração das palavras do pedido que aparecem em nome + descrição + gatilhos da skill (sem rede)
    embed   cosseno bge-m3 (Ollama /api/embeddings, num_gpu 0 = CPU, igual ao lib_alfred) entre pedido e skill
    laya    POST /v1/systemone do laya-serve (o mesmo payload de lib_laya.escolher), pergunta "choice"
    llm     gemma4:12b no /api/chat com saída JSON restrita ao enum dos nomes (num_ctx 16384, igual à Conversa)

Não importa nada do Alfred (não carrega .env): fala HTTP direto com o Laya e o Ollama em 127.0.0.1.

Uso (da raiz do repositório; laya e lexico não usam GPU, embed e llm vão dentro do flock):
    P=docs/avaliacoes/scripts/skills_proto
    python3 docs/avaliacoes/scripts/skills_seletor.py lexico --saida /tmp/sel_lexico.json
    python3 docs/avaliacoes/scripts/skills_seletor.py laya   --saida /tmp/sel_laya.json
    flock /tmp/alfred-bench-gpu.lock python3 docs/avaliacoes/scripts/skills_seletor.py embed --saida /tmp/sel_embed.json
    flock /tmp/alfred-bench-gpu.lock python3 docs/avaliacoes/scripts/skills_seletor.py llm   --saida /tmp/sel_llm.json
    python3 docs/avaliacoes/scripts/skills_seletor.py resumo /tmp/sel_*.json
"""

import argparse
import json
import math
import re
import statistics
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434"
LAYA = "http://127.0.0.1:7074"
MODELO = "gemma4:12b"
NUM_CTX = 16384
LIMIAR_LEXICO = 0.34       # definidos antes de rodar, não ajustados nos dados
LIMIAR_EMBED = 0.50
LIMIAR_LAYA = 0.70         # lib_laya.CONFIANCA_MINIMA
NENHUMA = "nenhuma"
DESC_NENHUMA = ("pedido simples que uma ferramenta sozinha ou a própria conversa resolve: conta, tradução, "
                "piada, lembrar um fato, adicionar uma tarefa, ler uma nota ou o jornal")
INSTRUCAO = "Qual procedimento do assistente Alfred atende este pedido de Pedro?"
STOP = set("a o as os de da do das dos e em no na nos nas um uma pra para por que me meu minha se com ta tá esta "
           "está ja já eu voce você isso esse essa aquele aquela ao the of".split())


def carregar_skills(pasta: Path) -> list[dict]:
    skills = []
    for arq in sorted(pasta.glob("*.md")):
        texto = arq.read_text()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", texto, re.S)
        meta = {}
        for linha in m.group(1).splitlines():
            if re.match(r"^\w+:", linha):
                chave, valor = linha.split(":", 1)
                meta[chave] = valor.strip()
        meta["gatilhos"] = json.loads(meta.get("gatilhos") or "[]")
        meta["corpo"] = m.group(2).strip()
        skills.append(meta)
    return skills


def _post(url: str, corpo: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _tokens(texto: str) -> set:
    t = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2 and w not in STOP}


def _texto_skill(s: dict) -> str:
    return f"{s['nome'].replace('-', ' ')}. {s['descricao']} Exemplos: {'; '.join(s['gatilhos'])}"


def sel_lexico(skills, pedidos):
    textos = {s["nome"]: _tokens(_texto_skill(s)) for s in skills}
    out = []
    for p in pedidos:
        t0 = time.perf_counter()
        q = _tokens(p["pedido"])
        notas = {n: (len(q & t) / len(q) if q else 0.0) for n, t in textos.items()}
        nome, nota = max(notas.items(), key=lambda x: x[1])
        out.append({"id": p["id"], "esperado": p["skill"], "top1": nome, "nota": round(nota, 3),
                    "escolhido": nome if nota >= LIMIAR_LEXICO else NENHUMA,
                    "ms": round((time.perf_counter() - t0) * 1000, 2)})
    return out


def _embed(texto: str) -> list[float]:
    return _post(f"{OLLAMA}/api/embeddings", {"model": "bge-m3", "prompt": texto, "options": {"num_gpu": 0}})["embedding"]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def sel_embed(skills, pedidos):
    t0 = time.perf_counter()
    vetores = {s["nome"]: _embed(_texto_skill(s)) for s in skills}
    indexar_ms = (time.perf_counter() - t0) * 1000
    out = []
    for p in pedidos:
        t0 = time.perf_counter()
        v = _embed(p["pedido"])
        notas = {n: _cos(v, w) for n, w in vetores.items()}
        nome, nota = max(notas.items(), key=lambda x: x[1])
        out.append({"id": p["id"], "esperado": p["skill"], "top1": nome, "nota": round(nota, 3),
                    "escolhido": nome if nota >= LIMIAR_EMBED else NENHUMA,
                    "ms": round((time.perf_counter() - t0) * 1000, 1)})
    out[0]["indexar_ms"] = round(indexar_ms, 1)
    return out


def sel_laya(skills, pedidos):
    opcoes = {s["nome"]: s["descricao"] for s in skills} | {NENHUMA: DESC_NENHUMA}
    out = []
    for p in pedidos:
        t0 = time.perf_counter()
        r = _post(f"{LAYA}/v1/systemone", {"state": p["pedido"], "questions": {
            "q": {"type": "choice", "instructions": INSTRUCAO, "criteria": opcoes}}}, timeout=30)
        ms = (time.perf_counter() - t0) * 1000
        q = (r.get("answers") or {}).get("q") or {}
        conf = q.get("answer_confidence", q.get("confidence", 0.0))
        out.append({"id": p["id"], "esperado": p["skill"], "top1": q.get("choice"), "nota": round(conf, 3),
                    "escolhido": q.get("choice") if conf >= LIMIAR_LAYA else None,   # None = cai para o LLM
                    "ms": round(ms, 1)})
    return out


def prompt_llm(skills) -> str:
    linhas = [f"- {s['nome']}: {s['descricao']}" for s in skills] + [f"- {NENHUMA}: {DESC_NENHUMA}"]
    return ("Você escolhe o procedimento (skill) do assistente Alfred que atende o pedido de Pedro. "
            "Responda só o JSON {\"skill\": \"<nome>\"}.\nSkills:\n" + "\n".join(linhas))


def sel_llm(skills, pedidos):
    nomes = [s["nome"] for s in skills] + [NENHUMA]
    formato = {"type": "object", "properties": {"skill": {"type": "string", "enum": nomes}}, "required": ["skill"]}
    sistema = prompt_llm(skills)
    out = []
    for p in pedidos:
        t0 = time.perf_counter()
        r = _post(f"{OLLAMA}/api/chat", {"model": MODELO, "stream": False, "think": False, "format": formato,
                                         "options": {"num_ctx": NUM_CTX, "temperature": 0},
                                         "messages": [{"role": "system", "content": sistema},
                                                      {"role": "user", "content": p["pedido"]}]})
        ms = (time.perf_counter() - t0) * 1000
        try:
            nome = json.loads(r["message"]["content"]).get("skill")
        except (json.JSONDecodeError, AttributeError):
            nome = None
        out.append({"id": p["id"], "esperado": p["skill"], "top1": nome, "escolhido": nome, "ms": round(ms, 1),
                    "tokens_entrada": r.get("prompt_eval_count"), "tokens_saida": r.get("eval_count"),
                    "carga_ms": round((r.get("load_duration") or 0) / 1e6, 1)})
    return out


def _acerto(res, chave="escolhido"):
    return sum(1 for r in res if r[chave] == r["esperado"])


def _p(valores, q):
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def resumo(arquivos: list[Path]) -> None:
    dados = {json.loads(a.read_text())["seletor"]: json.loads(a.read_text()) for a in arquivos}
    print("| Seletor | Acerto (34) | Acerto nas 26 com skill | Falsos positivos (8 sem skill) | p50 ms | p95 ms |")
    print("|---|---|---|---|---|---|")
    for nome, d in dados.items():
        res = d["resultados"]
        com = [r for r in res if r["esperado"] != NENHUMA]
        sem = [r for r in res if r["esperado"] == NENHUMA]
        fp = sum(1 for r in sem if r["escolhido"] not in (NENHUMA, None))
        ms = [r["ms"] for r in res]
        extra = ""
        if nome == "laya":
            decididos = [r for r in res if r["escolhido"] is not None]
            extra = (f" (decidiu {len(decididos)}/34 acima de {LIMIAR_LAYA}; acertou {_acerto(decididos)} deles; "
                     f"top-1 sem limiar {_acerto(res, 'top1')}/34)")
        if nome in ("lexico", "embed"):
            extra = f" (top-1 sem limiar nas 26: {_acerto(com, 'top1')}/26)"
        print(f"| {nome}{extra} | {_acerto(res)}/34 | {_acerto(com)}/26 | {fp}/8 | {_p(ms, .5):.0f} | {_p(ms, .95):.0f} |")
    if "laya" in dados and "llm" in dados:
        llm = {r["id"]: r for r in dados["llm"]["resultados"]}
        casc = []
        for r in dados["laya"]["resultados"]:
            usa_llm = r["escolhido"] is None
            casc.append({"esperado": r["esperado"], "escolhido": llm[r["id"]]["escolhido"] if usa_llm else r["escolhido"],
                         "ms": r["ms"] + (llm[r["id"]]["ms"] if usa_llm else 0), "llm": usa_llm})
        ms = [c["ms"] for c in casc]
        print(f"| cascata laya→llm (llm em {sum(c['llm'] for c in casc)}/34) | {_acerto(casc)}/34 | "
              f"{_acerto([c for c in casc if c['esperado'] != NENHUMA])}/26 | "
              f"{sum(1 for c in casc if c['esperado'] == NENHUMA and c['escolhido'] != NENHUMA)}/8 | "
              f"{_p(ms, .5):.0f} | {_p(ms, .95):.0f} |")
    if "llm" in dados:
        res = dados["llm"]["resultados"]
        print(f"\nllm: tokens de entrada médios {statistics.mean(r['tokens_entrada'] or 0 for r in res):.0f}, "
              f"saída {statistics.mean(r['tokens_saida'] or 0 for r in res):.0f}, "
              f"carga de modelo em {sum(1 for r in res if r['carga_ms'] > 1000)}/34 chamadas")
    if "embed" in dados:
        res = dados["embed"]["resultados"]
        print("\nembed, varredura do limiar (acerto 34 / falsos positivos 8):",
              ", ".join(f"{t:.2f}: {sum(1 for r in res if (r['top1'] if r['nota'] >= t else NENHUMA) == r['esperado'])}"
                        f"/{sum(1 for r in res if r['esperado'] == NENHUMA and r['nota'] >= t)}"
                        for t in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65)))
    for nome, d in dados.items():
        erros = [f"{r['id']}:{r['esperado']}→{r['escolhido']}" for r in d["resultados"] if r["escolhido"] != r["esperado"]]
        print(f"\nerros {nome}: {', '.join(erros)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("seletor", choices=["lexico", "embed", "laya", "llm", "resumo"])
    ap.add_argument("arquivos", nargs="*", type=Path)
    ap.add_argument("--skills", type=Path, default=AQUI / "skills_proto" / "skills")
    ap.add_argument("--pedidos", type=Path, default=AQUI / "skills_proto" / "pedidos.json")
    ap.add_argument("--saida", type=Path)
    a = ap.parse_args()
    if a.seletor == "resumo":
        return resumo(a.arquivos)
    skills = carregar_skills(a.skills)
    pedidos = json.loads(a.pedidos.read_text())
    res = {"lexico": sel_lexico, "embed": sel_embed, "laya": sel_laya, "llm": sel_llm}[a.seletor](skills, pedidos)
    saida = {"seletor": a.seletor, "quando": time.strftime("%Y-%m-%dT%H:%M:%S"), "resultados": res}
    if a.seletor == "llm":
        saida["prompt_chars"] = len(prompt_llm(skills))
    (a.saida.write_text(json.dumps(saida, ensure_ascii=False, indent=1)) if a.saida
     else json.dump(saida, sys.stdout, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
