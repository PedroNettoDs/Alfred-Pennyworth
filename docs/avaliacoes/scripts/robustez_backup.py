#!/usr/bin/env python3
"""
Avaliação de Robustez — backup.py: tempo, tamanho e restore completo numa pasta temporária.

  1. criar_backup() do estado real (logs/, perfis.yml, VAULT_ALFRED) para uma pasta temporária;
  2. percorre o tar.gz inteiro lendo cada membro (o equivalente ao `verify` do odysseus-backup);
  3. restaurar() para outra pasta temporária (raiz e cofre falsos) e confere cada arquivo: sha256 igual ao do
     tar, mtime igual, e cada banco SQLite com `PRAGMA integrity_check` = ok e as mesmas contagens de linhas
     da origem (as tabelas que mudaram no meio são contadas à parte);
  4. restaura de novo por cima (idempotência) e mede;
  5. apaga tudo (o backup tem e-mails e conversas). Só números saem.

Não chama main() do backup.py: os caminhos vão explícitos e o .env não é lido.
    services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_backup.py \\
        --raiz /caminho/do/Alfred-Pennyworth --vault /caminho/do/VAULT_ALFRED
"""

import argparse
import hashlib
import importlib.util
import io
import json
import resource
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

RAIZ_SCRIPT = Path(__file__).resolve().parents[3]


def _carregar_backup():
    spec = importlib.util.spec_from_file_location("backup_alfred", RAIZ_SCRIPT / "services" / "scripts" / "backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _medir(fn, *a, **k):
    t0, c0 = time.perf_counter(), _cpu()
    with redirect_stdout(io.StringIO()):   # o log() do backup cita caminhos; aqui só números
        res = fn(*a, **k)
    return res, round(time.perf_counter() - t0, 3), round(_cpu() - c0, 3)


def _contagens(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        tabelas = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tabelas}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raiz", type=Path, required=True)
    ap.add_argument("--vault", type=Path, required=True)
    ap.add_argument("--dir", type=Path, default=Path.home() / ".cache", help="onde criar a pasta temporária (ext4)")
    a = ap.parse_args()
    bk = _carregar_backup()
    base = Path(tempfile.mkdtemp(prefix="alfred-bench-backup-", dir=a.dir))
    base.chmod(0o700)
    out = {}
    try:
        tar_path, out["criar_s"], out["criar_cpu_s"] = _medir(bk.criar_backup, base / "bk", raiz=a.raiz,
                                                              vault=a.vault, manter=0)
        out["tar_mb"] = round(tar_path.stat().st_size / 1e6, 2)
        out["tar_modo"] = oct(tar_path.stat().st_mode & 0o777)

        t0 = time.perf_counter()
        hashes, por_area, bruto = {}, {}, 0
        with tarfile.open(tar_path, "r:gz") as tar:
            manifesto = json.load(tar.extractfile("MANIFESTO.json"))
            for m in tar.getmembers():
                if m.name == "MANIFESTO.json":
                    continue
                dados = tar.extractfile(m).read()
                hashes[m.name] = (hashlib.sha256(dados).hexdigest(), int(m.mtime))
                bruto += len(dados)
                area = m.name.split("/")[0]
                por_area[area] = por_area.get(area, 0) + 1
        out["verificar_s"] = round(time.perf_counter() - t0, 3)
        out["membros"] = len(hashes)
        out["membros_por_area"] = por_area
        out["bancos_sqlite"] = len(manifesto["bancos"])
        out["descomprimido_mb"] = round(bruto / 1e6, 2)
        out["compressao"] = round(out["tar_mb"] / max(out["descomprimido_mb"], 1e-9), 3)

        raiz_r, vault_r = base / "r" / "raiz", base / "r" / "vault"
        res, out["restaurar_s"], out["restaurar_cpu_s"] = _medir(bk.restaurar, tar_path, raiz=raiz_r, vault=vault_r,
                                                                 sim=True)
        out["restaurados_novos"] = len(res["novos"])
        destinos = {"repo": raiz_r, "vault": vault_r}
        origens = {"repo": a.raiz, "vault": a.vault}
        ok_hash = ok_mtime = faltando = 0
        bancos = {"integridade_ok": 0, "tabelas": 0, "tabelas_iguais_a_origem": 0, "tabelas_mudaram_depois": 0}
        for nome, (h, mtime) in hashes.items():
            area, _, rel = nome.partition("/")
            f = destinos[area] / rel
            if not f.is_file():
                faltando += 1
                continue
            ok_hash += hashlib.sha256(f.read_bytes()).hexdigest() == h
            ok_mtime += int(f.stat().st_mtime) == mtime
            if nome in manifesto["bancos"]:
                con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
                bancos["integridade_ok"] += con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                con.close()
                restaurado, origem = _contagens(f), _contagens(origens[area] / rel)
                bancos["tabelas"] += len(restaurado)
                iguais = sum(1 for t, n in restaurado.items() if origem.get(t) == n)
                bancos["tabelas_iguais_a_origem"] += iguais
                bancos["tabelas_mudaram_depois"] += len(restaurado) - iguais
        out["conferencia"] = {"sha256_igual_ao_tar": ok_hash, "mtime_igual": ok_mtime, "faltando": faltando,
                              "bancos": bancos}

        res2, out["restaurar_de_novo_s"], _ = _medir(bk.restaurar, tar_path, raiz=raiz_r, vault=vault_r, sim=True)
        out["restaurar_de_novo_sobrescritos"] = len(res2["sobrescritos"])
        out["pico_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    out["pasta_temporaria_apagada"] = not base.exists()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
