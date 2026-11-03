#!/usr/bin/env python3
"""
Avaliação de Robustez — o que ainda vaza nos logs depois de lib_alfred.sanitizar.

Lê CÓPIAS de logs (texto) e conta, por linha, ocorrências de telefone, e-mail e segredo em três estágios:
  bruto        a linha como está no arquivo
  sanitizar    depois de lib_alfred.sanitizar (o que vazaria mesmo se a linha tivesse passado por log())
  +url         sanitizar e depois corte de userinfo/query/fragmento de toda URL (a ideia do log_safety do
               Odysseus, reimplementada aqui com urllib — nenhum código dele é usado)
Os detectores são próprios e mais largos que os do sanitizar, de propósito. Só números saem: nenhum valor
encontrado é impresso (para segredo, sai só o NOME da chave, ex. EVOLUTION_API_KEY).

Para o journal (journalctl -o short-iso), separa linhas antes e depois de --corte (quando o sanitizar entrou em
produção) e classifica a origem da linha que vazou: log() do Alfred, acesso do uvicorn, traceback ou outra.

    journalctl --user -u alfred-inbox-api.service ... --since 2026-10-01 -o short-iso > /pasta/privada/journal.txt
    cp logs/*.json logs/*.jsonl /pasta/privada/
    services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_logs.py --corte 2026-10-31T15:08 /pasta/privada/*
    services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_logs.py --sintetico   # cobertura por tipo
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services"))
from _shared.lib_alfred import sanitizar  # noqa: E402

DETECTORES = {
    "telefone +55/55": re.compile(r"(?<![\d*])\+?55[\s.()-]*[1-9]\d[\s.()-]*9?\d{4}[\s.-]?\d{4}(?!\d)"),
    "telefone sem 55 (DDD+9)": re.compile(r"(?<![\d*+])\(?[1-9][1-9]\)?[\s.-]?9\d{4}[\s.-]?\d{4}(?!\d)"),
    "WhatsApp @lid": re.compile(r"\b\d{10,16}@lid\b"),
    "e-mail": re.compile(r"\b[A-Za-z0-9._%+-]+@(?!s\.whatsapp\.net\b|g\.us\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    "bearer": re.compile(r"(?i)\bbearer\s+(?!\*\*\*)[A-Za-z0-9\-._~+/]{8,}=*"),
    "chave=valor": re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|SENHA))['\"]?\s*[=:]\s*['\"]?(?!\*\*\*)[^\s'\"&,;})\]]{6,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "URL com usuário:senha": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    "query sensível (?token=...)": re.compile(r"(?i)[?&][a-z_]*(?:key|token|secret|sig|signature|auth|password|session|code)[a-z_]*=(?!\*\*\*)[^&\s\"']+"),
    "URL com query (qualquer)": re.compile(r"https?://[^\s\"'<>]*\?[^\s\"'<>]+"),
    "CPF": re.compile(r"(?<![\d.])\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"),
    "convite de grupo WhatsApp": re.compile(r"chat\.whatsapp\.com/[A-Za-z0-9]{10,}"),
    "IPv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
}
# "chave=valor" cujo valor é código (linha de fonte num traceback: KEY = os.getenv(...)) não é segredo
RE_CODIGO = re.compile(r"[A-Za-z_][\w.]*(\(.*)?")
MASCARADOS = {
    "telefone mascarado": re.compile(r"55\d{3}\*{2,}\d{4}"),
    "e-mail mascarado (domínio visível)": re.compile(r"[A-Za-z0-9._%+-]\*\*\*@[A-Za-z0-9-]+\."),
}
# (categoria, linha de log sintética, trecho que não pode sobrar) — dados inventados
SINTETICOS = [
    ("telefone +55 formatado", "ligue +55 (11) 98765-4321", "98765-4321"),
    ("telefone 55 cru / JID", "jid 5511987654321@s.whatsapp.net", "5511987654321"),
    ("telefone sem 55 formatado", "contato (11) 98765-4321", "98765-4321"),
    ("telefone sem 55 cru", "numero 11987654321", "11987654321"),
    ("WhatsApp @lid", "de 123456789012345@lid", "123456789012345"),
    ("e-mail", "de fulano.tal@exemplo.com.br", "fulano.tal"),
    ("CPF", "boleto CPF 123.456.789-09", "123.456.789-09"),
    ("Bearer", "Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("Basic auth", "Authorization: Basic dXNlcjpzZW5oYQ==", "dXNlcjpzZW5oYQ=="),
    ("NOME_KEY=valor", "EVOLUTION_API_KEY=k9x8c7v6", "k9x8c7v6"),
    ("password=valor", "password=hunter22", "hunter22"),
    ("x-api-key: valor", "headers x-api-key: k9x8c7v6", "k9x8c7v6"),
    ("JWT solto", "sessao eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJlXzEyMw", "eyJzdWIiOiIxMjMifQ"),
    ("URL usuário:senha", "GET http://pedro:s3nh4@nas.local:5000/api", "s3nh4"),
    ("URL usuário:senha@IP", "GET http://pedro:s3nh4@192.168.0.5:5000/api", "s3nh4"),
    ("URL ?token=", "GET https://api.exemplo.com/v1?token=t0k3n123", "t0k3n123"),
    ("URL ?code= (OAuth)", "callback https://h.exemplo/cb?code=4/0AbCdEf123&state=x", "4/0AbCdEf123"),
    ("URL ?sig= (assinada)", "baixando https://cdn.exemplo/a.mp3?sig=ab12cd34ef", "ab12cd34ef"),
    ("URL rastreio ?igsh=", "link https://www.instagram.com/reel/XYZ/?igsh=aWdzaDEyMzQ1", "aWdzaDEyMzQ1"),
    ("convite de grupo WhatsApp", "link https://chat.whatsapp.com/AbCdEfGhIjKlMnOp", "AbCdEfGhIjKlMnOp"),
]


def sintetico() -> dict:
    """Para cada caso: o trecho sensível some com sanitizar? e com sanitizar + corte de URL?"""
    linhas = []
    for cat, linha, segredo in SINTETICOS:
        san = sanitizar(linha)
        linhas.append({"caso": cat, "sanitizar": segredo not in san, "+url": segredo not in cortar_urls(san)})
    return {"casos": linhas, "cobertos_sanitizar": sum(c["sanitizar"] for c in linhas),
            "cobertos_+url": sum(c["+url"] for c in linhas), "total": len(linhas)}


RE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
RE_JOURNAL = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*\s+\S+\s+([^:\[]+)(?:\[\d+\])?:\s?(.*)$")


def cortar_urls(texto: str) -> str:
    """Toda URL vira esquema://host[:porta]/caminho (sem usuário:senha, query e fragmento)."""
    def um(m):
        try:
            p = urlsplit(m.group())
            host = p.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if p.port:
                host = f"{host}:{p.port}"
            return urlunsplit((p.scheme, host, p.path, "", ""))
        except ValueError:
            return "<url>"
    return RE_URL.sub(um, texto)


def _eh_codigo(m: re.Match) -> bool:
    valor = m.group().split(m.group(1), 1)[1].lstrip("'\" =:")
    return bool(RE_CODIGO.fullmatch(valor)) and not re.search(r"\d{4,}", valor)


def _quantos(det: str, rx: re.Pattern, texto: str) -> int:
    if det == "chave=valor":
        return sum(1 for m in rx.finditer(texto) if not _eh_codigo(m))
    return len(rx.findall(texto))


def origem(msg: str) -> str:
    if re.match(r"\[\d{2}:\d{2}:\d{2}\] ", msg):
        return "log() do Alfred"
    if re.search(r'\d+\.\d+\.\d+\.\d+:\d+ - "', msg):
        return "acesso uvicorn"
    if msg.startswith(("Traceback", "  File ", "    ")) or re.match(r"\w+(Error|Exception)\b", msg):
        return "traceback"
    return "outra (print/biblioteca/systemd)"


def contar(linhas, por_origem: bool = False) -> dict:
    est = {"bruto": Counter(), "sanitizar": Counter(), "+url": Counter()}
    linhas_com = {"bruto": 0, "sanitizar": 0, "+url": 0}
    origens, chaves, mascarados = Counter(), Counter(), Counter()
    n = 0
    for msg in linhas:
        n += 1
        estagios = {"bruto": msg}
        estagios["sanitizar"] = sanitizar(msg)
        estagios["+url"] = cortar_urls(estagios["sanitizar"])
        for nome, texto in estagios.items():
            achou = False
            for det, rx in DETECTORES.items():
                k = _quantos(det, rx, texto)
                if k:
                    est[nome][det] += k
                    if det != "IPv4":
                        achou = True
            if achou:
                linhas_com[nome] += 1
        for det, rx in MASCARADOS.items():
            mascarados[det] += len(rx.findall(msg))
        for m in DETECTORES["chave=valor"].finditer(estagios["sanitizar"]):
            if not _eh_codigo(m):
                chaves[m.group(1).upper()] += 1
        if por_origem and any(_quantos(d, rx, msg) for d, rx in DETECTORES.items() if d != "IPv4"):
            origens[origem(msg)] += 1
    res = {"linhas": n, "linhas_com_vazamento": linhas_com,
           "ocorrencias": {k: dict(sorted(v.items())) for k, v in est.items()},
           "ja_mascarados_no_arquivo": dict(mascarados),
           "nomes_de_chave_que_sobram_apos_sanitizar": dict(chaves.most_common())}
    if por_origem:
        res["origem_das_linhas_brutas_com_vazamento"] = dict(origens.most_common())
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivos", nargs="*", type=Path)
    ap.add_argument("--sintetico", action="store_true", help="só os casos sintéticos (sem arquivo)")
    ap.add_argument("--corte", default="2026-10-31T13:37", help="início do sanitizar em produção (journal)")
    a = ap.parse_args()
    if a.sintetico:
        print(json.dumps(sintetico(), ensure_ascii=False, indent=1))
        return 0
    saida = {}
    for arq in a.arquivos:
        texto = arq.read_text(encoding="utf-8", errors="replace").splitlines()
        if arq.name.startswith("journal"):
            antes, depois = [], []
            for linha in texto:
                m = RE_JOURNAL.match(linha)
                if not m:
                    continue
                (antes if m.group(1) < a.corte else depois).append(m.group(3))
            saida[arq.name] = {f"antes de {a.corte}": contar(antes, True), f"depois de {a.corte}": contar(depois, True)}
        else:
            saida[arq.name] = contar(texto)
    print(json.dumps(saida, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
