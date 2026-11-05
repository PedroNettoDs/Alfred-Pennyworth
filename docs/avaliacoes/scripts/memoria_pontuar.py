"""
Avaliação da memória — pontua saídas de extração (do Alfred ou do Odysseus) contra memoria_trechos.json.

Regras (iguais para os dois sistemas):
  - um fato extraído casa com um esperado se o texto normalizado (minúsculas, sem acento, pontuação vira espaço)
    tem pelo menos uma alternativa de CADA grupo de chaves, como início de palavra ('=x' = palavra inteira);
  - com sujeito estrito, o sujeito extraído também precisa bater (algum token em comum com um sujeito aceito);
  - um fato extraído pode cobrir mais de um esperado ("treina jiu-jitsu às terças e quintas" cobre dois);
  - extraído que não casa com esperado nem com aceitável é falso positivo; se casar com um proibido, conta
    também como erro grave (agenda do dia, senha, dedução, saída do assistente tomada como fato);
  - precisão = (casou esperado) / (casou esperado + falso positivo); aceitáveis ficam fora da conta;
  - revocação = esperados cobertos / esperados.

Uso:
    python memoria_pontuar.py memoria_trechos.json saida1.json [saida2.json ...] [--ignorar-sujeito] [--so conversa]
Cada saída: {"sistema", "modelo", "rodadas": [{"trechos": {id: {"fatos": [{"fato", "sujeito"}], "segundos"}}}]}
"""

import argparse
import json
import re
import statistics
import sys
import unicodedata
from pathlib import Path


def normalizar(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", s)          # "e-mail" → "e mail", "jiu-jitsu" → "jiu jitsu"
    return " " + re.sub(r"[^a-z0-9/:-]+", " ", s).strip() + " "


def _tem(texto_norm: str, alternativa: str) -> bool:
    if alternativa.startswith("="):
        return f" {normalizar(alternativa[1:]).strip()} " in texto_norm
    alvo = normalizar(alternativa).strip()
    return f" {alvo}" in texto_norm or (not alvo[:1].isalnum() and alvo in texto_norm)


def casa(texto: str, chaves: list) -> bool:
    t = normalizar(texto)
    return all(any(_tem(t, alt) for alt in grupo) for grupo in chaves)


def _tokens(nome: str) -> set:
    return set(normalizar(nome).split()) - {"seu", "dona", "sr", "sra", "dr"}


def sujeito_ok(extraido: str, aceitos: list) -> bool:
    e = _tokens(extraido)
    return any(e & _tokens(a) for a in aceitos)


def pontuar_trecho(trecho: dict, fatos: list, ignorar_sujeito: bool) -> dict:
    cobertos, vp, fp, graves, fora_sujeito = set(), 0, 0, 0, 0
    for f in fatos:
        texto = f.get("fato", "")
        # O sujeito entra no texto também: "Camila — é irmã de Pedro" casa com [camila][irma]
        texto_com_sujeito = f"{f.get('sujeito', '')} {texto}"
        bateu = False
        for i, esp in enumerate(trecho["esperados"]):
            if casa(texto_com_sujeito, esp["chaves"]):
                if ignorar_sujeito or sujeito_ok(f.get("sujeito", ""), esp["sujeitos"]):
                    cobertos.add(i)
                    bateu = True
                else:
                    fora_sujeito += 1
        if bateu:
            vp += 1
            continue
        if any(casa(texto_com_sujeito, a["chaves"]) for a in trecho.get("aceitaveis", [])):
            continue
        fp += 1
        if any(casa(texto_com_sujeito, p["chaves"]) for p in trecho.get("proibidos", [])):
            graves += 1
    return {"esperados": len(trecho["esperados"]), "cobertos": len(cobertos), "vp": vp, "fp": fp,
            "graves": graves, "extraidos": len(fatos), "sujeito_errado": fora_sujeito}


def pontuar(dataset: dict, saida: dict, ignorar_sujeito: bool = False, so: str = "") -> dict:
    trechos = [t for t in dataset["trechos"] if not so or (so == "conversa") == ("turnos" in t)]
    por_rodada = []
    for rod in saida["rodadas"]:
        soma = {"esperados": 0, "cobertos": 0, "vp": 0, "fp": 0, "graves": 0, "extraidos": 0, "sujeito_errado": 0,
                "fp_sem_fato": 0, "segundos": []}
        for t in trechos:
            r = rod["trechos"].get(t["id"])
            if r is None:
                continue
            p = pontuar_trecho(t, r.get("fatos", []), ignorar_sujeito)
            for k in ("esperados", "cobertos", "vp", "fp", "graves", "extraidos", "sujeito_errado"):
                soma[k] += p[k]
            if not t["esperados"]:
                soma["fp_sem_fato"] += p["fp"]
            if r.get("chamou_llm", True):
                soma["segundos"].append(r.get("segundos", 0.0))
        soma["precisao"] = soma["vp"] / (soma["vp"] + soma["fp"]) if soma["vp"] + soma["fp"] else 1.0
        soma["revocacao"] = soma["cobertos"] / soma["esperados"] if soma["esperados"] else 1.0
        soma["f1"] = (2 * soma["precisao"] * soma["revocacao"] / (soma["precisao"] + soma["revocacao"])
                      if soma["precisao"] + soma["revocacao"] else 0.0)
        s = soma.pop("segundos")
        soma["seg_mediana"] = statistics.median(s) if s else 0.0
        soma["seg_p90"] = sorted(s)[int(0.9 * (len(s) - 1))] if s else 0.0
        soma["chamadas_llm"] = len(s)
        por_rodada.append(soma)
    media = {k: statistics.mean(r[k] for r in por_rodada) for k in por_rodada[0]} if por_rodada else {}
    faixa = {k: (min(r[k] for r in por_rodada), max(r[k] for r in por_rodada))
             for k in ("precisao", "revocacao")} if por_rodada else {}
    return {"sistema": saida.get("sistema"), "modelo": saida.get("modelo"), "variante": saida.get("variante", ""),
            "trechos": len(trechos), "rodadas": len(por_rodada), "media": media, "faixa": faixa}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("saidas", nargs="+")
    ap.add_argument("--ignorar-sujeito", action="store_true")
    ap.add_argument("--so", choices=["", "conversa"], default="")
    a = ap.parse_args()
    dataset = json.loads(Path(a.dataset).read_text(encoding="utf-8"))
    print("sistema | modelo | variante | trechos | P | R | F1 | FP | graves | FP em trecho sem fato | "
          "sujeito errado | s/trecho (mediana) | p90")
    for arq in a.saidas:
        r = pontuar(dataset, json.loads(Path(arq).read_text(encoding="utf-8")), a.ignorar_sujeito, a.so)
        m, fx = r["media"], r["faixa"]
        print(f"{r['sistema']} | {r['modelo']} | {r['variante']} | {r['trechos']} | "
              f"{m['precisao']:.2f} ({fx['precisao'][0]:.2f}–{fx['precisao'][1]:.2f}) | "
              f"{m['revocacao']:.2f} ({fx['revocacao'][0]:.2f}–{fx['revocacao'][1]:.2f}) | {m['f1']:.2f} | "
              f"{m['fp']:.1f} | {m['graves']:.1f} | {m['fp_sem_fato']:.1f} | {m['sujeito_errado']:.1f} | "
              f"{m['seg_mediana']:.2f} | {m['seg_p90']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
