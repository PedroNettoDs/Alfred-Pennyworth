"""
Avaliação E-mail — utilitários comuns aos scripts de medição (só leitura).

- Importa services/_shared/lib_mail.py sem carregar lib_alfred (troca o log por um mudo).
- Enumera as pastas mbox de cada conta do perfil do Thunderbird.
- Dá rótulos anônimos às pastas: nomes-padrão ficam, o resto vira "pasta-N".
- Nada aqui grava no perfil, no mbox nem no .msf; nenhum texto de e-mail sai dos scripts, só números.
"""

import re
import sys
import types
from email.parser import BytesHeaderParser
from email import policy
from pathlib import Path
from typing import Iterator, Optional

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ / "services"))

# lib_mail tenta "from _shared.lib_alfred import log"; um stub evita carregar o resto do Alfred.
_stub = types.ModuleType("_shared.lib_alfred")
_stub.log = lambda msg: None
sys.modules["_shared.lib_alfred"] = _stub

from _shared import lib_mail  # noqa: E402

ROTULO_CONTA = {
    "outlook.office365.com": "hotmail",
    "imap.gmail.com": "gmail",
}

PASTAS_PADRAO = {
    "INBOX", "Sent", "Archive", "Trash", "Junk", "Drafts",
    "[Gmail]/Lixeira", "[Gmail]/Todos os e-mails", "[Gmail]/Com estrela", "[Gmail]/Importante",
    "[Gmail]/Rascunhos", "[Gmail]/Enviados", "[Gmail]/Spam", "Importantes",
}


def perfil() -> Path:
    p = lib_mail.find_thunderbird_profile()
    if not p:
        sys.exit("perfil do Thunderbird não encontrado")
    return p


def contas(prof: Path) -> list[dict]:
    out = []
    for a in lib_mail.list_accounts(prof):
        a = dict(a)
        a["rotulo"] = ROTULO_CONTA.get(a["hostname"], "local" if a["type"] == "none" else a["id"])
        out.append(a)
    return out


def pastas_mbox(conta: dict) -> list[tuple[str, Path]]:
    """[(nome lógico "Pai/Filho", caminho do mbox)] de uma conta mbox."""
    base = conta.get("directory")
    if not base or not Path(base).is_dir() or conta.get("store") != "mbox":
        return []
    base = Path(base)
    out = []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.suffix in (".msf", ".dat", ".json", ".sqlite", ".sbd"):
            continue
        if f.name in ("msgFilterRules.dat", "popstate.dat") or f.name.startswith("."):
            continue
        rel = f.relative_to(base)
        nome = "/".join(p[:-4] if p.endswith(".sbd") else p for p in rel.parts)
        out.append((nome, f))
    return out


def rotulo_pasta(nome: str, vistos: dict) -> str:
    if nome in PASTAS_PADRAO:
        return nome
    if nome not in vistos:
        vistos[nome] = f"pasta-{len(vistos) + 1}"
    return vistos[nome]


_HDR = BytesHeaderParser(policy=policy.compat32)


def iter_mbox(path: Path) -> Iterator[tuple[bytes, bytes]]:
    """(linha separadora "From - …", bytes da mensagem) — mesmo separador de lib_mail._iter_mbox_raw."""
    data = path.read_bytes()
    sep: Optional[bytes] = None
    buf: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if lib_mail._MBOX_SEP.match(line):
            if buf or sep is not None:
                yield sep or b"", b"".join(buf)
            sep, buf = line, []
            continue
        buf.append(line)
    if buf or sep is not None:
        yield sep or b"", b"".join(buf)


def cabecalhos(raw: bytes):
    """Só os cabeçalhos (rápido); o corpo não é lido."""
    corte = raw.find(b"\n\n")
    return _HDR.parsebytes(raw if corte < 0 else raw[:corte + 2])


def message_id(h) -> str:
    return lib_mail._decode_hdr(h.get("Message-ID")).strip("<> ")


def status_mozilla(h) -> int:
    try:
        return int(str(h.get("X-Mozilla-Status", "0") or "0").strip(), 16)
    except ValueError:
        return 0


def e_fragmento(h) -> bool:
    """Mesmo critério de fetch_messages: sem From e sem Subject = falso separador."""
    return not (h.get("From") or "").strip() and not (h.get("Subject") or "").strip()


def linhas_indice(path: Path) -> Optional[dict]:
    """
    Lê o .msf com o leitor Mork do lib_mail e devolve {"ids": set, "linhas": int, "sem_mid": int}
    ("linhas" = linhas da tabela de mensagens, para contar Message-ID repetido no próprio índice).
    None se não há .msf legível.
    """
    msf = path.with_name(path.name + ".msf")
    if not msf.is_file():
        return None
    try:
        mk = lib_mail._Mork(msf.read_bytes().decode("latin-1"))
        ids = mk.message_ids()
    except Exception:
        return None
    if ids is None:
        return None
    tabs = [t for t in mk.tabelas.values() if t["tipo"] == mk.TIPO_MSGS]
    chaves = [c for t in tabs for c in t["linhas"]]
    sem_mid = sum(1 for c in chaves if not (mk.linhas.get(c) or {}).get("message-id"))
    return {"ids": ids, "linhas": len(chaves), "sem_mid": sem_mid, "mork": mk, "chaves": chaves}


_TS_SEP = re.compile(rb"^From (?:-|\S+) (\w{3} \w{3} +\d{1,2} \d\d:\d\d:\d\d \d{4})")
