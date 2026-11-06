"""
Avaliação E-mail — frescor: quanto tempo entre o e-mail chegar ao servidor e o Alfred vê-lo (só leitura).

Para cada mensagem da INBOX (primeira cópia de cada Message-ID no mbox, que só cresce por append):
  t_srv   data do Received mais alto (o último servidor, o da caixa, carimbou a chegada)
  t_tb    data da linha separadora "From - …" do mbox (instante em que o Thunderbird gravou a cópia offline).
          O Thunderbird snap grava essa hora em UTC (sem fuso no sandbox): uma mensagem que chegou às 04:47
          (-03) aparece como "07:48". Lida como hora local, todo A sai 3 h maior; ver --tz-separador.
  t_alf   início do 1º ciclo efetivo do alfred-inbox (linha "[mail] … selecionadas" no journal) depois de t_tb
  A = t_tb − t_srv   atraso do Thunderbird (aberto + IDLE: segundos; fechado: até ele abrir)
  B = t_alf − t_tb   espera pelo próximo ciclo (timer 07:30/18:30 + máquina ligada + modo jogo)
  perdido            Date < t_alf − janela_horas: o ciclo nunca o seleciona (janela de 36 h no Date)
Valida o modelo com logs/inbox_seen.json (Message-ID → minuto em que o ciclo o processou).
Também lê do journal do alfred-vigia ("Thunderbird aberto: sim|não", a cada 10 min).

Uso:  python3 docs/avaliacoes/scripts/email_frescor.py [--conta hotmail] [--desde 2026-10-20] [--janela 36]
"""

import argparse
import bisect
import json
import re
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import email_comum as c



def _seen_padrao() -> Path:
    """logs/inbox_seen.json do checkout; numa worktree, o do checkout principal (onde os serviços rodam)."""
    p = c.RAIZ / "logs" / "inbox_seen.json"
    if p.is_file():
        return p
    comum = subprocess.run(["git", "-C", str(c.RAIZ), "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True).stdout.strip()
    return (c.RAIZ / comum).resolve().parent / "logs" / "inbox_seen.json"


def _journal(unidade: str) -> list[str]:
    p = subprocess.run(["journalctl", "--user", "-u", unidade, "-o", "short-iso", "--no-pager"],
                       capture_output=True, text=True)
    return p.stdout.splitlines()


def ciclos_efetivos() -> tuple[list[datetime], int]:
    """Instantes em que o ciclo leu o mbox (linha '[mail] … selecionadas') e total de disparos do timer."""
    efetivos, disparos = [], 0
    for ln in _journal("alfred-inbox.service"):
        if "Starting alfred-inbox" in ln:
            disparos += 1
        elif "mensagens selecionadas" in ln:
            efetivos.append(datetime.fromisoformat(ln.split()[0]))
    return sorted(efetivos), disparos


def thunderbird_aberto() -> dict:
    """Amostras do vigia (a cada 10 min com a sessão de Pedro ativa): proxy de 'máquina ligada'."""
    sim = nao = 0
    primeiro = ultimo = None
    amostras = []
    for ln in _journal("alfred-vigia.service"):
        m = re.search(r"Thunderbird aberto: (sim|não)", ln)
        if m:
            t = datetime.fromisoformat(ln.split()[0])
            primeiro = primeiro or t
            ultimo = t
            amostras.append(t)
            sim += m.group(1) == "sim"
            nao += m.group(1) == "não"
    horas = (ultimo - primeiro).total_seconds() / 3600 if primeiro else 0
    return {"amostras": sim + nao, "aberto": sim, "fechado": nao,
            "periodo_h": round(horas, 1),
            # vigia a cada 10 min: amostras × 10 min ≈ horas com a máquina ligada e logada
            "horas_maquina_ligada_aprox": round((sim + nao) / 6, 1),
            "_amostras": amostras}


def espera_ligada(amostras: list, t: datetime) -> float:
    """Segundos de t até a máquina estar ligada (0 se havia amostra do vigia até 10 min depois de t)."""
    i = bisect.bisect_left(amostras, t - timedelta(minutes=10))
    if i >= len(amostras):
        return float("nan")
    return max(0.0, (amostras[i] - t).total_seconds() - 600)


def _dt(v):
    try:
        d = parsedate_to_datetime(v)
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        return None
    return d.astimezone()


def _t_srv(h):
    rec = h.get_all("Received") or []
    for r in rec:      # o mais alto (o último a carimbar); se ilegível, desce
        if ";" in r:
            d = _dt(" ".join(r.rsplit(";", 1)[1].split()))
            if d:
                return d
    return None


def _t_tb(sep: bytes, tz):
    m = c._TS_SEP.match(sep)
    if not m:
        return None
    return datetime.strptime(" ".join(m.group(1).decode().split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=tz)


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    k = (len(v) - 1) * p
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


def resumo(v, unidade=3600.0):
    if not v:
        return {"n": 0}
    v = [x / unidade for x in v]
    return {"n": len(v), "p50": round(pct(v, .5), 2), "p90": round(pct(v, .9), 2),
            "p99": round(pct(v, .99), 2), "max": round(max(v), 2), "media": round(statistics.mean(v), 2),
            "pct_ate_15min": round(100 * sum(1 for x in v if x * unidade <= 900) / len(v), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conta", default="hotmail")
    ap.add_argument("--desde", default="2026-10-20", help="só mensagens gravadas no mbox a partir desta data")
    ap.add_argument("--janela", type=int, default=36, help="janela_horas do perfil (perfis.yml)")
    ap.add_argument("--tz-separador", choices=["utc", "local"], default="utc",
                    help="fuso da linha 'From - …' do mbox (snap: utc)")
    ap.add_argument("--seen", type=Path, default=None, help="inbox_seen.json (padrão: logs/ do checkout principal)")
    args = ap.parse_args()

    tz = datetime.now().astimezone().tzinfo
    desde = datetime.fromisoformat(args.desde).replace(tzinfo=tz)
    tz_sep = timezone.utc if args.tz_separador == "utc" else tz
    prof = c.perfil()
    conta = next(a for a in c.contas(prof) if a["rotulo"] == args.conta)
    path = c.lib_mail.resolve_folder(conta, "INBOX")

    msgs = {}        # mid → (t_srv, t_tb, t_date)
    copias = {}      # mid → [t_tb, …]
    sem_srv = 0
    for sep, raw in c.iter_mbox(path):
        if not raw.strip():
            continue
        h = c.cabecalhos(raw)
        if c.e_fragmento(h) or c.status_mozilla(h) & 0x0008:
            continue
        mid = c.message_id(h)
        t_tb = _t_tb(sep, tz_sep)
        copias.setdefault(mid, []).append(t_tb)
        if mid in msgs:
            continue
        t_srv = _t_srv(h)
        sem_srv += t_srv is None
        msgs[mid] = (t_srv, t_tb, _dt(str(h.get("Date") or "")))

    efetivos, disparos = ciclos_efetivos()
    seen = {}
    arq_seen = args.seen or _seen_padrao()
    if arq_seen.is_file():
        for k, v in json.loads(arq_seen.read_text(encoding="utf-8")).items():
            try:
                seen[k] = datetime.fromisoformat(v).replace(tzinfo=tz)
            except (TypeError, ValueError):
                pass

    tb = thunderbird_aberto()
    amostras = tb.pop("_amostras")
    A, B, T = [], [], []
    perdidos = perdidos_tb = pendentes = fora = 0
    valid = []
    # cenários sobre as mesmas mensagens (ver relatório): segundos desde a chegada ao servidor
    cen_ligada, cen_tb_aberto_timer = [], []
    perdidos_tb_aberto_timer = 0
    for mid, (t_srv, t_tb, t_date) in msgs.items():
        if t_tb is None or t_tb < desde or t_srv is None:
            fora += 1
            continue
        a = (t_tb - t_srv).total_seconds()
        A.append(a)
        # IMAP IDLE no Alfred, ou Thunderbird aberto com a sessão + ciclo disparado pela mudança do mbox:
        # o e-mail é visto assim que a máquina está ligada
        on = espera_ligada(amostras, t_srv)
        if on == on:
            cen_ligada.append(on)
            # Thunderbird sempre aberto com a sessão, timer de hoje: próximo ciclo efetivo depois disso
            j = bisect.bisect_left(efetivos, t_srv + timedelta(seconds=on))
            if j < len(efetivos):
                c_ = efetivos[j]
                if t_date is not None and t_date < c_ - timedelta(hours=args.janela):
                    perdidos_tb_aberto_timer += 1
                else:
                    cen_tb_aberto_timer.append((c_ - t_srv).total_seconds())
        i = bisect.bisect_left(efetivos, t_tb)
        if i >= len(efetivos):
            pendentes += 1
            continue
        t_alf = efetivos[i]
        if t_date is not None and t_date < t_alf - timedelta(hours=args.janela):
            perdidos += 1
            perdidos_tb += a > args.janela * 3600
            continue
        B.append((t_alf - t_tb).total_seconds())
        T.append((t_alf - t_srv).total_seconds())
        if mid in seen:
            valid.append((seen[mid] - t_alf).total_seconds() / 60)

    # cópias extras: quanto depois da 1ª a 2ª foi gravada, e se vieram em rajada (mesmo minuto)
    seg_copias = [(v[1] - v[0]).total_seconds() for v in copias.values()
                  if len(v) > 1 and v[0] and v[1]]
    rajadas = {}
    for v in copias.values():
        if len(v) > 1 and v[1]:
            k = v[1].strftime("%Y-%m-%d %H:%M")
            rajadas[k] = rajadas.get(k, 0) + 1

    out = {
        "conta": args.conta, "desde": args.desde, "janela_h": args.janela,
        "mensagens_inbox": len(msgs), "sem_received": sem_srv, "fora_do_periodo": fora,
        "ciclos": {"disparos_do_timer": disparos, "efetivos": len(efetivos),
                   "primeiro": efetivos[0].isoformat() if efetivos else None,
                   "ultimo": efetivos[-1].isoformat() if efetivos else None},
        "A_thunderbird_h": resumo(A),
        "A_pct_ate_5min": round(100 * sum(1 for x in A if x <= 300) / len(A), 1) if A else None,
        "A_pct_ate_1h": round(100 * sum(1 for x in A if x <= 3600) / len(A), 1) if A else None,
        "A_negativos": sum(1 for x in A if x < -120),
        "B_espera_ciclo_h": resumo(B),
        "total_h": resumo(T),
        "perdidos_pela_janela": perdidos,
        "perdidos_so_pelo_thunderbird_fechado": perdidos_tb,
        "pendentes_sem_ciclo_ainda": pendentes,
        "validacao_inbox_seen_min": {"n": len(valid),
                                     "p50": round(pct(valid, .5), 1) if valid else None,
                                     "abs_max": round(max(abs(x) for x in valid), 1) if valid else None,
                                     "ate_5min": sum(1 for x in valid if abs(x) <= 5)},
        "copias_extras": {"n": len(seg_copias), "atraso_2a_copia_h": resumo(seg_copias),
                          "minutos_distintos": len(rajadas),
                          "maior_rajada": max(rajadas.values()) if rajadas else 0},
        "thunderbird_vigia": tb,
        "cenarios_total_h": {
            "hoje": {**resumo(T), "perdidos": perdidos},
            "vigiar_mbox_tb_como_hoje": {**resumo([x for x in A]), "perdidos": 0},
            "tb_aberto_com_a_sessao_timer_de_hoje": {**resumo(cen_tb_aberto_timer),
                                                     "perdidos": perdidos_tb_aberto_timer},
            "imap_idle_ou_tb_aberto_mais_vigiar_mbox": {**resumo(cen_ligada), "perdidos": 0},
        },
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
