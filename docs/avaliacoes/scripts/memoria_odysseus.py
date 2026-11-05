"""
Avaliação da memória — roda a lógica de memória do Odysseus (services/memory/memory_extractor.py e
src/memory.py de um clone, importados daqui; nada do código deles é copiado para o Alfred) contra o
mesmo Ollama local, para comparar com o Alfred nos mesmos dados.

Isolamento: rode com um venv próprio no scratchpad e `python -I`, ambiente limpo (sem .env, cofres ou dados
do Alfred). Só usa a biblioteca padrão. As dependências de rede do Odysseus são trocadas por stubs:
  src.llm_core.llm_call_async → POST /api/chat do Ollama (think=false, mesma num_ctx do Alfred)
  src.utility_effort / src.event_bus → no-op
  índice vetorial (ChromaDB no Odysseus atual) → stub em memória com bge-m3 no Ollama (CPU), mesmo
                                                   find_similar por cosseno e limiar 0,72 do extrator

Modos:
  extrair     roda extract_and_store() de verdade em cada trecho de conversa (store novo por trecho) e grava
              duas variantes: 'completo' (o que o Odysseus guardaria: filtro de evidência + candidatos por
              regex) e 'so_llm' (a lista que o LLM devolveu, antes do filtro de evidência)
  deduplicar  insere as entradas de memoria_dedup.json com a mesma sequência de checagens do
              extract_and_store (vetor ≥ --limiar, 0,72 no Odysseus → descarta; enriquecimento de nome; texto igual; Jaccard) e depois
              roda audit_memories() de verdade

Uso:
  python -I memoria_odysseus.py extrair    --odysseus <clone> --trechos memoria_trechos.json --modelo gemma4:12b \
         --rodadas 3 --saida ody_12b.json [--ollama http://localhost:11434]
  python -I memoria_odysseus.py deduplicar --odysseus <clone> --dados memoria_dedup.json --modelo gemma4:12b \
         --saida ody_dedup.json --pasta /tmp/x
"""

import argparse
import asyncio
import importlib.util
import json
import math
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
NUM_CTX = 8192
RESPOSTAS: list = []            # saída crua de cada chamada ao LLM (para a variante so_llm)


def _post(caminho: str, corpo: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(f"{OLLAMA}{caminho}", data=json.dumps(corpo).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


async def _llm_call_async(endpoint_url, model, messages, temperature=0.1, max_tokens=None, headers=None,
                          reasoning_effort=None, timeout=300, **_):
    corpo = {"model": model, "messages": messages, "stream": False, "think": False,
             "options": {"temperature": temperature, "num_ctx": NUM_CTX,
                         **({"num_predict": max_tokens} if max_tokens else {})}}
    resp = await asyncio.to_thread(_post, "/api/chat", corpo, timeout or 300)
    texto = resp.get("message", {}).get("content", "")
    RESPOSTAS.append(texto)
    return texto


def _embed(texto: str) -> list:
    v = _post("/api/embeddings", {"model": "bge-m3", "prompt": texto, "options": {"num_gpu": 0}}, 60)["embedding"]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class VetorEmMemoria:
    """Mesma interface usada pelo extrator (healthy, find_similar, add, remove, rebuild), com bge-m3."""

    def __init__(self):
        self.vetores: dict = {}
        self.healthy = True

    def find_similar(self, text, threshold=0.92):
        if not self.vetores:
            return None
        q = _embed(text)
        mid, sim = max(((m, sum(a * b for a, b in zip(q, v))) for m, v in self.vetores.items()), key=lambda x: x[1])
        return mid if sim >= threshold else None

    def add(self, memory_id, text):
        if memory_id not in self.vetores:
            self.vetores[memory_id] = _embed(text)

    def remove(self, memory_id):
        self.vetores.pop(memory_id, None)

    def rebuild(self, memories):
        self.vetores = {m["id"]: _embed(m["text"]) for m in memories if isinstance(m, dict) and m.get("text")}


class Sessao:
    def __init__(self, turnos, nome):
        self.mensagens = [{"role": r, "content": c} for r, c in turnos]
        self.session_id = nome
        self.owner = None

    def get_context_messages(self):
        return list(self.mensagens)


def carregar_odysseus(clone: Path):
    """Importa src.memory e o extrator do clone, com stubs no lugar do cliente LLM e do barramento de eventos."""
    sys.path.insert(0, str(clone))
    llm_core = types.ModuleType("src.llm_core")
    llm_core.llm_call_async = _llm_call_async
    effort = types.ModuleType("src.utility_effort")
    effort.effort_for_call = lambda *a, **k: None
    eventos = types.ModuleType("src.event_bus")
    eventos.fire_event = lambda *a, **k: None
    sys.modules.update({"src.llm_core": llm_core, "src.utility_effort": effort, "src.event_bus": eventos})
    from src.memory import MemoryManager
    spec = importlib.util.spec_from_file_location("ody_memory_extractor",
                                                  clone / "services" / "memory" / "memory_extractor.py")
    mx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mx)
    return MemoryManager, mx


def modo_extrair(a, MemoryManager, mx) -> None:
    trechos = [t for t in json.loads(Path(a.trechos).read_text(encoding="utf-8"))["trechos"] if "turnos" in t]
    saidas = {v: {"sistema": "odysseus", "modelo": a.modelo, "variante": v, "rodadas": []}
              for v in ("completo", "so_llm")}
    t0 = time.perf_counter()
    asyncio.run(_llm_call_async(OLLAMA, a.modelo, [{"role": "user", "content": "Responda []"}]))
    for s in saidas.values():
        s["aquecimento_s"] = round(time.perf_counter() - t0, 2)
    for n in range(a.rodadas):
        rods = {v: {"inicio": time.time(), "trechos": {}} for v in saidas}
        for t in trechos:
            with tempfile.TemporaryDirectory() as pasta:
                mm = MemoryManager(pasta)
                RESPOSTAS.clear()
                mx._extractions_since_audit = -10**9     # auditoria fica para o modo deduplicar
                t0 = time.perf_counter()
                asyncio.run(mx.extract_and_store(Sessao(t["turnos"], t["id"]), mm, VetorEmMemoria(), OLLAMA, a.modelo))
                dt = time.perf_counter() - t0
                guardados = mm.load_all()
            brutos = mx._parse_extraction_json(RESPOSTAS[0]) if RESPOSTAS else []
            rods["completo"]["trechos"][t["id"]] = {
                "fatos": [{"fato": f["text"], "sujeito": "Pedro"} for f in guardados], "segundos": round(dt, 3)}
            rods["so_llm"]["trechos"][t["id"]] = {
                "fatos": [{"fato": str(f.get("text", "")), "sujeito": "Pedro"} for f in brutos if isinstance(f, dict)],
                "segundos": round(dt, 3), "cru": RESPOSTAS[0][:600] if RESPOSTAS else ""}
            print(f"[{n + 1}] {t['id']} {dt:5.2f}s llm={len(brutos)} guardados={len(guardados)}", flush=True)
        for v, rod in rods.items():
            rod["fim"] = time.time()
            saidas[v]["rodadas"].append(rod)
    base = Path(a.saida)
    for v, s in saidas.items():
        base.with_name(f"{base.stem}_{v}{base.suffix}").write_text(json.dumps(s, ensure_ascii=False, indent=1),
                                                                   encoding="utf-8")


def modo_deduplicar(a, MemoryManager, mx) -> None:
    entradas = json.loads(Path(a.dados).read_text(encoding="utf-8"))["entradas"]
    pasta = Path(a.pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "memory.json").unlink(missing_ok=True)
    (pasta / "memory_tidy_state.json").unlink(missing_ok=True)
    mm, vec = MemoryManager(str(pasta)), VetorEmMemoria()
    existentes, criado_por, acoes = [], {}, {}
    asyncio.run(_llm_call_async(OLLAMA, a.modelo, [{"role": "user", "content": "Responda []"}]))

    t0 = time.perf_counter()
    for e in entradas:
        texto = f"{e['sujeito']} {e['fato']}"
        # Mesma ordem de checagens do extract_and_store (owner=None, sem legado)
        mid = vec.find_similar(texto, threshold=a.limiar)
        if mid and any(x.get("id") == mid for x in existentes):
            acoes[e["id"]] = "descartado_vetor"
            continue
        alvo = mx._name_enrichment_target(texto, [x for x in existentes if x.get("owner") is None])
        if alvo is not None:
            alvo["text"] = texto
            vec.add(alvo["id"], texto)
            acoes[e["id"]] = "enriqueceu_nome"
            continue
        if mm.find_duplicates(texto, existentes):
            acoes[e["id"]] = "descartado_texto_igual"
            continue
        if mx._is_text_duplicate(texto, existentes):
            acoes[e["id"]] = "descartado_jaccard"
            continue
        item = mm.add_entry(texto, source="auto", category="fact")
        existentes.append(item)
        vec.add(item["id"], texto)
        criado_por[item["id"]] = e["id"]
        acoes[e["id"]] = "novo"
    mm.save(existentes)
    seg_entrada = time.perf_counter() - t0

    def fase(segundos, extra):
        return {"vivos": {x["id"]: {"texto": x["text"], "criado_por": criado_por.get(x["id"], "?")}
                          for x in mm.load_all()}, "segundos": round(segundos, 2), **extra}

    saida = {"sistema": "odysseus", "modelo": f"{a.modelo} limiar={a.limiar}", "fases": {"entrada": fase(seg_entrada, {"chamadas_llm": 0,
                                                                                                "acoes": acoes})}}
    RESPOSTAS.clear()
    t0 = time.perf_counter()
    res = asyncio.run(mx.audit_memories(mm, vec, OLLAMA, a.modelo))
    saida["fases"]["auditoria"] = fase(time.perf_counter() - t0, {"resultado": res, "chamadas_llm": len(RESPOSTAS)})
    Path(a.saida).write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: {"vivos": len(v["vivos"]), "segundos": v["segundos"]} for k, v in saida["fases"].items()}))


def main() -> int:
    global OLLAMA
    ap = argparse.ArgumentParser()
    ap.add_argument("modo", choices=["extrair", "deduplicar"])
    ap.add_argument("--odysseus", required=True)
    ap.add_argument("--modelo", required=True)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--trechos")
    ap.add_argument("--dados")
    ap.add_argument("--pasta")
    ap.add_argument("--rodadas", type=int, default=3)
    ap.add_argument("--limiar", type=float, default=0.72, help="deduplicar: cosseno do find_similar (0,72 no extrator)")
    ap.add_argument("--ollama", default=OLLAMA)
    a = ap.parse_args()
    OLLAMA = a.ollama.rstrip("/")
    MemoryManager, mx = carregar_odysseus(Path(a.odysseus))
    (modo_extrair if a.modo == "extrair" else modo_deduplicar)(a, MemoryManager, mx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
