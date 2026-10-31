#!/usr/bin/env python3
"""
Backup e restore do estado do Alfred num tar.gz datado.

    services/inbox/venv/bin/python services/scripts/backup.py criar [--destino DIR] [--manter N]
    services/inbox/venv/bin/python services/scripts/backup.py restaurar ARQUIVO [--dry-run] [--sim] [--vault DIR] [--raiz DIR]

Usa o Python de um venv do Alfred (lib_alfred importa httpx). O .env é lido em tempo de execução só para achar
o VAULT_ALFRED (e o ALFRED_BACKUP_DIR) — ele mesmo NUNCA entra no backup.

O que entra (caminhos dentro do .tar.gz):
    repo/logs/...                       estado dos serviços (seen, fila, categorias, vigia...)
    repo/services/_shared/perfis.yml
    vault/...                           o VAULT_ALFRED inteiro (pendências, agenda, memória, jornal, podcast)
    MANIFESTO.json                      data, caminhos de origem e a lista de bancos SQLite
Bancos SQLite (ex.: vault/.alfred/alfred.db) são achados pelo cabeçalho e copiados pela API de backup online do
SQLite (a mesma do `sqlite3 .backup`): cópia consistente mesmo com a API gravando. -wal/-shm não entram.

O que NÃO entra: .env (segredos), data/ (volumes do Docker, do root), symlinks, temporários (*.tmp) e travas (*.lock).

Destino: --destino, ALFRED_BACKUP_DIR ou ~/.local/share/alfred/backups (fora do /tmp, que some no reboot). O
arquivo é criado 0600 (tem e-mails e conversas) e só ganha o nome final depois de completo; ficam os --manter
mais novos (padrão 8 — o alfred-backup.timer é semanal).

Restore: escreve só o que está no backup (não apaga nada a mais), cada arquivo por troca atômica, e remove o
-wal/-shm velho ao lado de cada banco restaurado. Pede confirmação (--sim pula); --dry-run só mostra. Pare o
Alfred antes: services/scripts/alfred-switch.sh off.
"""

import argparse
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _shared.lib_alfred import PROJECT_ROOT, gravar_atomico, load_env, log  # noqa: E402

PREFIXO = "alfred-backup-"
MANTER = 8
ITENS_REPO = ("logs", "services/_shared/perfis.yml")             # relativos à raiz do repositório
IGNORAR_SUFIXOS = (".tmp", ".lock", ".parcial", "-wal", "-shm", "-journal")
CABECALHO_SQLITE = b"SQLite format 3\x00"


def destino_padrao() -> Path:
    if os.getenv("ALFRED_BACKUP_DIR", "").strip():
        return Path(os.environ["ALFRED_BACKUP_DIR"].strip()).expanduser()
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "alfred" / "backups"


def _eh_sqlite(f: Path) -> bool:
    try:
        with open(f, "rb") as fh:
            return fh.read(16) == CABECALHO_SQLITE
    except OSError:
        return False


def _arquivos(origem: Path, pular: Path):
    """Arquivos regulares de `origem` (arquivo ou pasta), em ordem, sem symlinks, temporários, travas e a pasta `pular`."""
    if origem.is_file():
        yield origem
        return
    for d, subdirs, nomes in os.walk(origem):
        subdirs[:] = sorted(s for s in subdirs if (Path(d) / s).resolve() != pular)   # destino dentro do cofre
        for n in sorted(nomes):
            f = Path(d) / n
            if not n.endswith(IGNORAR_SUFIXOS) and not f.is_symlink() and f.is_file():
                yield f


def _copiar_sqlite(f: Path, copia: Path) -> None:
    """Cópia consistente de um banco possivelmente aberto (API de backup online — o `.backup` do sqlite3)."""
    origem = sqlite3.connect(f, timeout=30)
    try:
        destino = sqlite3.connect(copia)
        try:
            origem.backup(destino)
        finally:
            destino.close()
    finally:
        origem.close()


def criar_backup(destino: Path, raiz: Path = PROJECT_ROOT, vault: Optional[Path] = None,
                 manter: int = MANTER) -> Path:
    """Gera <destino>/alfred-backup-AAAA-MM-DD_HHMMSS.tar.gz e apaga os mais antigos além de `manter` (0 = todos)."""
    destino, raiz = Path(destino).expanduser(), Path(raiz)
    destino.mkdir(mode=0o700, parents=True, exist_ok=True)
    origens = [("repo", raiz / item, raiz) for item in ITENS_REPO]
    if vault and Path(vault).is_dir():
        origens.append(("vault", Path(vault), Path(vault)))
    else:
        log(f"[backup] VAULT_ALFRED {'vazio' if not vault else f'não existe ({vault})'} — só logs/ e perfis.yml")
    agora = datetime.now()
    nome = f"{PREFIXO}{agora:%Y-%m-%d_%H%M%S}.tar.gz"
    final, parcial = destino / nome, destino / f".{nome}.parcial"
    manifesto = {"criado_em": agora.isoformat(timespec="seconds"), "raiz": str(raiz),
                 "vault": str(vault) if vault else None, "bancos": []}
    total = 0
    try:
        with tempfile.TemporaryDirectory(dir=destino, prefix=".copias-") as tmp, \
                open(parcial, "wb", opener=lambda p, fl: os.open(p, fl, 0o600)) as fh:
            with tarfile.open(fileobj=fh, mode="w:gz") as tar:
                for area, origem, base in origens:
                    for f in _arquivos(origem, destino.resolve()):
                        arcname = f"{area}/{f.relative_to(base).as_posix()}"
                        if _eh_sqlite(f):
                            copia = Path(tmp) / f"{len(manifesto['bancos'])}.db"
                            _copiar_sqlite(f, copia)
                            tar.add(copia, arcname=arcname)
                            manifesto["bancos"].append(arcname)
                        else:
                            tar.add(f, arcname=arcname)
                        total += 1
                dados = json.dumps(manifesto, ensure_ascii=False, indent=2).encode("utf-8")
                info = tarfile.TarInfo("MANIFESTO.json")
                info.size, info.mtime, info.mode = len(dados), int(agora.timestamp()), 0o600
                tar.addfile(info, BytesIO(dados))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(parcial, final)
    except BaseException:
        parcial.unlink(missing_ok=True)
        raise
    log(f"[backup] {final} — {total} arquivo(s), {len(manifesto['bancos'])} banco(s) SQLite, "
        f"{final.stat().st_size / 1e6:.1f} MB")
    if manter > 0:
        for velho in sorted(destino.glob(f"{PREFIXO}*.tar.gz"))[:-manter]:   # nome datado: ordem = cronologia
            velho.unlink()
            log(f"[backup] removido (passou de {manter}): {velho.name}")
    return final


def _plano(tar: tarfile.TarFile, raiz: Path, vault: Optional[Path]) -> list[tuple[tarfile.TarInfo, Path]]:
    """(membro, destino) de cada arquivo. Recusa o backup inteiro se algum membro sair de repo/ ou vault/."""
    destinos = {"repo": Path(raiz), "vault": Path(vault) if vault else None}
    plano = []
    for m in tar.getmembers():
        if m.name == "MANIFESTO.json":
            continue
        area, _, rel = m.name.partition("/")
        if area not in destinos or not rel or not m.isfile() or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError(f"membro inesperado no backup: {m.name!r} — não foi gerado pelo backup.py")
        if destinos[area] is None:
            raise ValueError("o backup tem o cofre (vault/) e não sei para onde vai: defina VAULT_ALFRED ou use --vault")
        plano.append((m, destinos[area] / rel))
    return plano


def _confirmar() -> bool:
    if not sys.stdin.isatty():
        log("[restore] sem terminal para confirmar — use --sim (ou --dry-run para só ver)")
        return False
    return input("Restaurar e sobrescrever os arquivos acima? Digite 'sim': ").strip().lower() == "sim"


def restaurar(arquivo: Path, raiz: Path = PROJECT_ROOT, vault: Optional[Path] = None,
              dry_run: bool = False, sim: bool = False) -> dict:
    """
    Restaura `arquivo` em `raiz` (repo/) e `vault` (vault/; padrão: o caminho de origem gravado no manifesto).
    Retorna {"restaurado": bool, "novos": [Path], "sobrescritos": [Path]}.
    """
    with tarfile.open(arquivo, "r:gz") as tar:
        manifesto = json.load(tar.extractfile("MANIFESTO.json"))
        vault = vault or manifesto.get("vault")
        plano = _plano(tar, raiz, vault)
        novos = [d for _, d in plano if not d.exists()]
        sobrescritos = [d for _, d in plano if d.exists()]
        res = {"restaurado": False, "novos": novos, "sobrescritos": sobrescritos}
        log(f"[restore] {Path(arquivo).name} (de {manifesto['criado_em']}): {len(plano)} arquivo(s) — "
            f"{len(novos)} novo(s), {len(sobrescritos)} sobrescrito(s)")
        log(f"[restore]   repo/  → {raiz}")
        log(f"[restore]   vault/ → {vault}")
        if dry_run:
            for _, d in plano:
                log(f"[restore]   {'sobrescreve' if d.exists() else 'novo':<11} {d}")
            return res
        log("[restore] Pare o Alfred antes (services/scripts/alfred-switch.sh off): a API grava nesses arquivos.")
        if not sim and not _confirmar():
            log("[restore] cancelado — nada foi alterado")
            return res
        for m, d in plano:
            if m.name in manifesto["bancos"]:   # -wal/-shm velhos seriam aplicados por cima do banco restaurado
                for sufixo in ("-wal", "-shm", "-journal"):
                    Path(f"{d}{sufixo}").unlink(missing_ok=True)
            gravar_atomico(d, tar.extractfile(m).read())
            os.utime(d, (m.mtime, m.mtime))
    res["restaurado"] = True
    log(f"[restore] concluído: {len(plano)} arquivo(s) restaurado(s)")
    return res


def main(argv: Optional[list[str]] = None) -> int:
    load_env(override=False)
    p = argparse.ArgumentParser(description="Backup e restore do estado do Alfred (tar.gz datado).")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar", help="gera o tar.gz datado")
    c.add_argument("--destino", type=Path, help="pasta dos backups (padrão: ALFRED_BACKUP_DIR ou ~/.local/share/alfred/backups)")
    c.add_argument("--manter", type=int, default=MANTER, help=f"quantos backups guardar (padrão {MANTER}; 0 = todos)")
    r = sub.add_parser("restaurar", help="restaura um tar.gz (pede confirmação)")
    r.add_argument("arquivo", type=Path)
    r.add_argument("--dry-run", action="store_true", help="só mostra o que seria escrito")
    r.add_argument("--sim", action="store_true", help="não pergunta (para scripts)")
    r.add_argument("--raiz", type=Path, default=PROJECT_ROOT, help="destino de repo/ (padrão: este repositório)")
    r.add_argument("--vault", type=Path, help="destino de vault/ (padrão: VAULT_ALFRED; sem ele, o caminho de origem)")
    a = p.parse_args(argv)
    vault_env = os.getenv("VAULT_ALFRED", "").strip()
    if a.cmd == "criar":
        criar_backup(a.destino or destino_padrao(), vault=Path(vault_env) if vault_env else None, manter=a.manter)
        return 0
    res = restaurar(a.arquivo, raiz=a.raiz, vault=a.vault or (Path(vault_env) if vault_env else None),
                    dry_run=a.dry_run, sim=a.sim)
    return 0 if res["restaurado"] or a.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
