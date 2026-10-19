"""
Alfred — leitura de e-mails.

Backends:
  - Thunderbird local (mbox "berkeleystore" ou maildir) — padrão. Lê direto os
    arquivos do perfil, sem rede e sem senha. Funciona com contas IMAP/OAuth2
    (Outlook, Gmail) porque usa as cópias offline que o Thunderbird já baixou.
  - IMAP direto (imaplib) — opcional, para contas com senha de aplicativo.
    Outlook/Hotmail exige OAuth2 e NÃO funciona por este caminho.

Uso:
    from _shared.lib_mail import find_thunderbird_profile, list_accounts, fetch_messages

    profile  = find_thunderbird_profile()
    accounts = list_accounts(profile)
    msgs     = fetch_messages(accounts, pastas=["INBOX"],
                              desde=datetime.now().astimezone() - timedelta(hours=36))

Cada mensagem é um dict normalizado (ver parse_message) — sem objetos email.*,
para ser fácil de serializar e de passar pro LLM.
"""

import configparser
import email
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone, date as _date
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Optional

try:
    from _shared.lib_alfred import log
except ImportError:  # execução direta pra debug
    from datetime import datetime as _dt
    def log(msg: str):  # type: ignore[misc]
        print(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════
#  1. Descoberta do perfil do Thunderbird
# ══════════════════════════════════════════════════════════════

CANDIDATOS_PROFILES_INI = [
    Path.home() / "snap/thunderbird/current/.config/thunderbird/profiles.ini",   # snap (Ubuntu)
    Path.home() / "snap/thunderbird/common/.thunderbird/profiles.ini",           # snap antigo
    Path.home() / ".thunderbird/profiles.ini",                                   # deb / tarball
    Path.home() / ".var/app/org.mozilla.Thunderbird/.thunderbird/profiles.ini",  # flatpak
    Path.home() / ".mozilla-thunderbird/profiles.ini",                           # legado
]


def _parse_profiles_ini(ini: Path) -> Optional[Path]:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(ini, encoding="utf-8")
    except Exception as e:
        log(f"[mail] profiles.ini ilegível ({ini}): {e}")
        return None

    default: Optional[Path] = None
    first: Optional[Path] = None
    for sec in cp.sections():
        if not sec.lower().startswith("profile"):
            continue
        path = cp[sec].get("Path", "").strip()
        if not path:
            continue
        rel = cp[sec].get("IsRelative", "1").strip() == "1"
        p = (ini.parent / path) if rel else Path(path)
        if first is None:
            first = p
        if cp[sec].get("Default", "0").strip() == "1":
            default = p

    p = default or first
    return p if p and p.is_dir() else None


def find_thunderbird_profile(explicit: Optional[str] = None) -> Optional[Path]:
    """
    Retorna o diretório do perfil do Thunderbird.
    Ordem: argumento → THUNDERBIRD_PROFILE (.env) → candidatos conhecidos.
    """
    env = (explicit or os.getenv("THUNDERBIRD_PROFILE", "")).strip()
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
        log(f"[mail] THUNDERBIRD_PROFILE aponta pra diretório inexistente: {p}")
        return None

    for ini in CANDIDATOS_PROFILES_INI:
        if ini.exists():
            prof = _parse_profiles_ini(ini)
            if prof:
                return prof
    return None


# ══════════════════════════════════════════════════════════════
#  2. prefs.js → contas
# ══════════════════════════════════════════════════════════════

_PREF_RE = re.compile(r'^\s*user_pref\("((?:[^"\\]|\\.)*)",\s*(.*?)\);\s*$')


def read_prefs(profile: Path) -> dict:
    """Parse simples do prefs.js (strings, ints, bools)."""
    prefs: dict = {}
    f = profile / "prefs.js"
    if not f.exists():
        return prefs
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _PREF_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith('"'):
            try:
                val = json.loads(raw)
            except Exception:
                val = raw.strip('"')
        elif raw in ("true", "false"):
            val = raw == "true"
        else:
            try:
                val = int(raw)
            except ValueError:
                val = raw
        prefs[key] = val
    return prefs


def list_accounts(profile: Path) -> list[dict]:
    """
    Lista contas de e-mail do perfil:
        [{"id": "server2", "type": "imap", "hostname": "outlook.office365.com",
          "name": "fulano@hotmail.com", "directory": Path(...), "store": "mbox"}]
    """
    prefs = read_prefs(profile)
    servers: dict[str, dict] = {}
    for key, val in prefs.items():
        m = re.match(r"mail\.server\.(server\d+)\.([\w-]+)$", key)
        if m:
            servers.setdefault(m.group(1), {})[m.group(2)] = val

    accounts: list[dict] = []
    for sid, s in sorted(servers.items()):
        tipo = str(s.get("type", ""))
        if tipo not in ("imap", "pop3", "none"):
            continue
        d = s.get("directory-rel") or s.get("directory") or ""
        d = str(d)
        if d.startswith("[ProfD]"):
            d = str(profile / d[len("[ProfD]"):])
        store = "maildir" if "maildirstore" in str(s.get("storeContractID", "")) else "mbox"
        accounts.append({
            "id":        sid,
            "type":      tipo,
            "hostname":  str(s.get("hostname", "")),
            "name":      str(s.get("name", "")),
            "directory": Path(d) if d else None,
            "store":     store,
        })

    if not accounts:  # fallback: varrer diretórios padrão
        for base, tipo in ((profile / "ImapMail", "imap"), (profile / "Mail", "none")):
            if base.is_dir():
                for sub in sorted(base.iterdir()):
                    if sub.is_dir():
                        accounts.append({
                            "id": sub.name, "type": tipo, "hostname": sub.name,
                            "name": sub.name, "directory": sub, "store": "mbox",
                        })
    return accounts


def resolve_folder(account: dict, folder: str = "INBOX") -> Optional[Path]:
    """
    Resolve "INBOX", "Inbox", "Pai/Filho" para o caminho real no disco.
    mbox:    <dir>/INBOX            subpasta: <dir>/Pai.sbd/Filho
    maildir: <dir>/INBOX/{cur,new}  subpasta: <dir>/Pai.sbd/Filho/{cur,new}
    """
    base = account.get("directory")
    if not base or not Path(base).is_dir():
        return None

    parts = [p for p in folder.replace("\\", "/").split("/") if p]
    p = Path(base)
    for i, part in enumerate(parts):
        p = p / (f"{part}.sbd" if i < len(parts) - 1 else part)

    def _match(cand: Path) -> bool:
        if account.get("store") == "maildir":
            return cand.is_dir() and (cand / "cur").is_dir()
        return cand.is_file() and cand.suffix != ".msf"

    if _match(p):
        return p
    # fallback case-insensitive (INBOX vs Inbox)
    if p.parent.is_dir():
        for cand in p.parent.iterdir():
            if cand.name.lower() == p.name.lower() and _match(cand):
                return cand
    return None


# ══════════════════════════════════════════════════════════════
#  3. Leitura bruta: mbox (Thunderbird) e maildir
# ══════════════════════════════════════════════════════════════

# Thunderbird: "From - Mon Oct  5 18:16:19 2026"; mbox clássico: "From user@host Mon Oct ..."
_MBOX_SEP = re.compile(rb"^From (?:-|\S+) \w{3} \w{3} +\d{1,2} \d\d:\d\d:\d\d \d{4}")


def _iter_mbox_raw(path: Path) -> Iterator[bytes]:
    """Divide o mbox pelas linhas separadoras. Lê tudo em memória (INBOX típica < 100 MB)."""
    data = path.read_bytes()
    buf: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if _MBOX_SEP.match(line):
            if buf:
                yield b"".join(buf)
            buf = []
            continue
        buf.append(line)
    if buf:
        yield b"".join(buf)


def _iter_maildir_raw(path: Path) -> Iterator[bytes]:
    for sub in ("cur", "new"):
        d = path / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file():
                try:
                    yield f.read_bytes()
                except Exception as e:
                    log(f"[mail] Erro lendo {f.name}: {e}")


def iter_folder_raw(path: Path, store: str = "mbox") -> Iterator[bytes]:
    if store == "maildir":
        yield from _iter_maildir_raw(path)
    else:
        yield from _iter_mbox_raw(path)


# ══════════════════════════════════════════════════════════════
#  4. Parsing de uma mensagem → dict normalizado
# ══════════════════════════════════════════════════════════════

class _HTMLText(HTMLParser):
    """HTML → texto corrido + lista de links (texto_âncora, url)."""
    _SKIP  = {"script", "style", "head", "title", "noscript", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "section", "article", "header", "footer", "ul", "ol",
              "blockquote", "hr", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: Optional[str] = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "td":
            self.parts.append(" ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href.strip()
            self._anchor = []
        if tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt and self._href is not None:
                self._anchor.append(alt)

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            text = " ".join(" ".join(self._anchor).split())
            if self._href.lower().startswith(("http://", "https://")):
                self.links.append((text, self._href))
            self._href = None
            self._anchor = []

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)

    def text(self) -> str:
        return _normalize_text("".join(self.parts))


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", text)   # zero-width / soft hyphen
    text = text.replace("\xa0", " ")
    lines = [" ".join(l.split()) for l in text.split("\n")]
    out: list[str] = []
    blank = 0
    for l in lines:
        if not l:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(l)
    return "\n".join(out).strip()


def html_to_text(src: str) -> tuple[str, list[tuple[str, str]]]:
    p = _HTMLText()
    try:
        p.feed(src)
        p.close()
    except Exception as e:
        log(f"[mail] HTML parser falhou: {e}")
        return _normalize_text(re.sub(r"<[^>]+>", " ", src)), []
    return p.text(), p.links


def _decode_hdr(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if "=?" in s:
        try:
            s = str(make_header(decode_header(s)))
        except Exception:
            pass
    return " ".join(s.split())


def _part_text(part) -> str:
    """Conteúdo textual de uma part, tolerante a charset desconhecido."""
    try:
        c = part.get_content()
        if isinstance(c, bytes):
            c = c.decode(part.get_content_charset() or "utf-8", errors="replace")
        return c
    except Exception:
        try:
            raw = part.get_payload(decode=True) or b""
            return raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            return ""


_ICS_LINE = re.compile(r"^(?P<key>[A-Z-]+)(?:;[^:]*)?:(?P<val>.*)$")


def _ics_dt_to_iso(val) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo:
            return val.astimezone().isoformat(timespec="minutes")
        return val.isoformat(timespec="minutes")
    if isinstance(val, _date):
        return val.isoformat()
    s = str(val).strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$", s)
    if not m:
        return s or None
    y, mo, d, hh, mm, ss, z = m.groups()
    if hh is None:
        return f"{y}-{mo}-{d}"
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
    if z:
        dt = dt.replace(tzinfo=timezone.utc).astimezone()
    return dt.isoformat(timespec="minutes")


def parse_ics(raw) -> list[dict]:
    """Extrai VEVENTs de um .ics (icalendar se disponível; fallback regex)."""
    if isinstance(raw, bytes):
        raw_s = raw.decode("utf-8", errors="replace")
    else:
        raw_s = str(raw)
    eventos: list[dict] = []
    try:
        from icalendar import Calendar  # type: ignore
        cal = Calendar.from_ical(raw_s)
        for comp in cal.walk("VEVENT"):
            def _g(k):
                v = comp.get(k)
                return str(v) if v is not None else ""
            def _d(k):
                try:
                    return _ics_dt_to_iso(comp.decoded(k))
                except Exception:
                    return _ics_dt_to_iso(_g(k))
            eventos.append({
                "summary":     _g("SUMMARY"),
                "dtstart":     _d("DTSTART"),
                "dtend":       _d("DTEND"),
                "location":    _g("LOCATION"),
                "url":         _g("URL"),
                "organizer":   _g("ORGANIZER").replace("mailto:", "").replace("MAILTO:", ""),
                "description": _g("DESCRIPTION")[:600],
            })
        return eventos
    except ImportError:
        pass
    except Exception as e:
        log(f"[mail] icalendar falhou ({e}) — usando fallback regex")

    # fallback: unfold + regex
    unfolded = re.sub(r"\r?\n[ \t]", "", raw_s)
    cur: Optional[dict] = None
    for line in unfolded.splitlines():
        if line.strip() == "BEGIN:VEVENT":
            cur = {"summary": "", "dtstart": None, "dtend": None, "location": "",
                   "url": "", "organizer": "", "description": ""}
            continue
        if line.strip() == "END:VEVENT" and cur is not None:
            eventos.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        m = _ICS_LINE.match(line.strip())
        if not m:
            continue
        k, v = m.group("key"), m.group("val").replace("\\n", "\n").replace("\\,", ",")
        if k == "SUMMARY":       cur["summary"] = v
        elif k == "DTSTART":     cur["dtstart"] = _ics_dt_to_iso(v)
        elif k == "DTEND":       cur["dtend"] = _ics_dt_to_iso(v)
        elif k == "LOCATION":    cur["location"] = v
        elif k == "URL":         cur["url"] = v
        elif k == "ORGANIZER":   cur["organizer"] = v.replace("mailto:", "").replace("MAILTO:", "")
        elif k == "DESCRIPTION": cur["description"] = v[:600]
    return eventos


def _msg_date(msg) -> Optional[datetime]:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(str(raw))
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone()


def parse_message(msg, folder: str = "", account: str = "", max_text: int = 12000) -> dict:
    """
    email.message.EmailMessage → dict normalizado:

        message_id, folder, account, date (datetime local | None), date_iso,
        from_name, from_addr, from_domain, to (list), subject,
        text (corpo em texto puro), links [(texto, url)], attachments [nomes],
        list_id, is_bulk (List-Id/List-Unsubscribe/Precedence),
        ics (lista de eventos extraídos de .ics), read, expunged
    """
    subject   = _decode_hdr(msg.get("Subject"))
    from_name, from_addr = parseaddr(_decode_hdr(msg.get("From")))
    from_addr = from_addr.lower()
    to_addrs  = [a.lower() for _, a in getaddresses([_decode_hdr(msg.get("To", ""))]) if a]

    mid = _decode_hdr(msg.get("Message-ID")).strip("<> ")
    if not mid:
        mid = "gen-" + hashlib.sha1(f"{from_addr}|{subject}|{msg.get('Date','')}".encode()).hexdigest()[:16]

    try:
        status = int(str(msg.get("X-Mozilla-Status", "0") or "0").strip(), 16)
    except ValueError:
        status = 0

    plain: Optional[str] = None
    html_src: Optional[str] = None
    attachments: list[str] = []
    ics_events: list[dict] = []

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        ct = part.get_content_type()
        fn = _decode_hdr(part.get_filename())
        disp = part.get_content_disposition()
        if ct == "text/calendar" or fn.lower().endswith(".ics"):
            try:
                ics_events.extend(parse_ics(part.get_payload(decode=True) or b""))
            except Exception as e:
                log(f"[mail] .ics ilegível em '{subject[:40]}': {e}")
            if fn:
                attachments.append(fn)
            continue
        if disp == "attachment" or (fn and not ct.startswith("text/")):
            attachments.append(fn or ct)
            continue
        if ct == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ct == "text/html" and html_src is None:
            html_src = _part_text(part)

    html_text, links = ("", [])
    if html_src:
        html_text, links = html_to_text(html_src)
    plain_norm = _normalize_text(plain) if plain else ""

    # newsletters costumam ter text/plain só com "ver no navegador" → prefere HTML se for bem maior
    if plain_norm and (not html_text or len(plain_norm) >= 200 or len(plain_norm) >= len(html_text) * 0.5):
        text = plain_norm
    else:
        text = html_text or plain_norm

    if len(text) > max_text:
        text = text[:max_text] + "\n[... truncado ...]"

    # dedup de links preservando ordem
    seen_urls: set[str] = set()
    uniq_links: list[tuple[str, str]] = []
    for t, u in links:
        if u in seen_urls:
            continue
        seen_urls.add(u)
        uniq_links.append((t[:120], u))

    date_local = _msg_date(msg)
    list_id = _decode_hdr(msg.get("List-Id"))
    is_bulk = bool(list_id or msg.get("List-Unsubscribe")
                   or str(msg.get("Precedence", "")).lower() in ("bulk", "list")
                   or msg.get("X-Mailchimp-ID") or msg.get("X-Campaign") or msg.get("X-SES-Outgoing"))

    return {
        "message_id":  mid,
        "folder":      folder,
        "account":     account,
        "date":        date_local,
        "date_iso":    date_local.isoformat(timespec="minutes") if date_local else "",
        "from_name":   from_name.strip() or from_addr,
        "from_addr":   from_addr,
        "from_domain": from_addr.split("@")[-1] if "@" in from_addr else "",
        "to":          to_addrs,
        "subject":     subject,
        "text":        text,
        "links":       uniq_links,
        "attachments": attachments,
        "list_id":     list_id,
        "is_bulk":     is_bulk,
        "ics":         ics_events,
        "read":        bool(status & 0x0001),
        "expunged":    bool(status & 0x0008),
    }


def _from_bytes(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


# ══════════════════════════════════════════════════════════════
#  5. API de alto nível — Thunderbird
# ══════════════════════════════════════════════════════════════

def fetch_messages(
    accounts: list[dict],
    pastas: Optional[list[str]] = None,
    desde: Optional[datetime] = None,
    conta: str = "",
    max_total: int = 500,
    incluir_lidos: bool = True,
) -> list[dict]:
    """
    Lê as pastas das contas e devolve mensagens normalizadas, mais recentes primeiro.

    :param accounts:      saída de list_accounts()
    :param pastas:        nomes de pasta (default ["INBOX"])
    :param desde:         datetime com tz; mensagens mais antigas são ignoradas
    :param conta:         filtra por hostname ou name da conta (substring, case-insensitive)
    :param max_total:     limite de segurança
    :param incluir_lidos: False → só mensagens não lidas
    """
    pastas = pastas or ["INBOX"]
    if desde is not None and desde.tzinfo is None:
        desde = desde.astimezone()

    out: list[dict] = []
    for acc in accounts:
        if acc.get("type") == "none" and not conta:
            continue  # "Pastas locais" só se pedido explicitamente
        if conta:
            alvo = conta.lower()
            if alvo not in acc.get("hostname", "").lower() and alvo not in acc.get("name", "").lower():
                continue
        for pasta in pastas:
            path = resolve_folder(acc, pasta)
            if not path:
                log(f"[mail] Pasta '{pasta}' não encontrada em {acc.get('hostname')} ({acc.get('directory')})")
                continue
            n_lidas = n_ok = 0
            for raw in iter_folder_raw(path, acc.get("store", "mbox")):
                n_lidas += 1
                if not raw.strip():
                    continue
                try:
                    m = parse_message(_from_bytes(raw), folder=pasta, account=acc.get("hostname", ""))
                except Exception as e:
                    log(f"[mail] Mensagem ilegível em {pasta}: {e}")
                    continue
                if m["expunged"]:
                    continue
                if not m["from_addr"] and not m["subject"]:
                    continue  # fragmento (falso separador) — ignora
                if desde is not None and m["date"] is not None and m["date"] < desde:
                    continue
                if not incluir_lidos and m["read"]:
                    continue
                out.append(m)
                n_ok += 1
            log(f"[mail] {acc.get('hostname')}/{pasta}: {n_ok} mensagens selecionadas de {n_lidas}")

    out.sort(key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:max_total]


# ══════════════════════════════════════════════════════════════
#  6. Backend IMAP direto (opcional)
# ══════════════════════════════════════════════════════════════

_MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_fetch(
    host: str,
    user: str,
    password: str,
    folder: str = "INBOX",
    desde: Optional[datetime] = None,
    port: int = 993,
    limit: int = 200,
) -> list[dict]:
    """
    Busca mensagens via IMAP (SSL). Usa BODY.PEEK pra não marcar como lida.
    Mesmo formato de saída de fetch_messages().
    """
    out: list[dict] = []
    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(user, password)
        M.select(folder, readonly=True)
        crit = "ALL"
        if desde is not None:
            crit = f'(SINCE {desde.day:02d}-{_MESES_IMAP[desde.month - 1]}-{desde.year})'
        typ, data = M.search(None, crit)
        ids = data[0].split()[-limit:] if typ == "OK" and data and data[0] else []
        for i in reversed(ids):
            typ, d = M.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            try:
                m = parse_message(_from_bytes(d[0][1]), folder=folder, account=host)
                if desde is not None and m["date"] is not None and m["date"] < desde:
                    continue
                out.append(m)
            except Exception as e:
                log(f"[imap] Mensagem ilegível: {e}")
        M.logout()
    except Exception as e:
        log(f"[imap] Erro em {host}: {e}")
    out.sort(key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


# ══════════════════════════════════════════════════════════════
#  CLI de debug:  python3 lib_mail.py [horas] [pasta]
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    horas = int(sys.argv[1]) if len(sys.argv) > 1 else 48
    pasta = sys.argv[2] if len(sys.argv) > 2 else "INBOX"
    prof = find_thunderbird_profile()
    print(f"perfil: {prof}")
    if not prof:
        sys.exit(1)
    accs = list_accounts(prof)
    for a in accs:
        print(f"  conta {a['id']}: type={a['type']} host={a['hostname']} store={a['store']} dir={a['directory']}")
    desde = datetime.now().astimezone() - timedelta(hours=horas)
    msgs = fetch_messages(accs, pastas=[pasta], desde=desde)
    print(f"\n{len(msgs)} mensagens nas últimas {horas}h em {pasta}:")
    for m in msgs[:40]:
        flags = ("ICS " if m["ics"] else "") + ("BULK " if m["is_bulk"] else "") + ("" if m["read"] else "NOVO ")
        print(f"  {m['date_iso']:<22} {m['from_domain']:<28} {m['subject'][:60]:<60} {flags}")
