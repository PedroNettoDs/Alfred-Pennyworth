"""
Testes de robustez: logs sem segredos (lib_alfred.sanitizar), gravação atômica (lib_alfred.gravar_atomico, que
core.gravar_atomico reaproveita) e backup → restore (services/scripts/backup.py).

    cd services/inbox
    venv/bin/python -m unittest tests.test_robustez -v

Tudo offline e em pastas temporárias: nada toca o .env, o vault ou os logs reais.
"""

import errno
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
SERVICES = INBOX.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(SERVICES))

import core  # noqa: E402
from _shared import lib_alfred  # noqa: E402
from _shared.lib_alfred import gravar_atomico, sanitizar  # noqa: E402


def _carregar_backup():
    spec = importlib.util.spec_from_file_location("backup_alfred", SERVICES / "scripts" / "backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup = _carregar_backup()


# ══════════════════════════════════════════════════════════════
#  sanitizar / log
# ══════════════════════════════════════════════════════════════

class TestSanitizar(unittest.TestCase):

    def test_bearer(self):
        self.assertEqual(sanitizar("Authorization: Bearer eyJhbGc.iOiJ-IUzI1_Ni=="), "Authorization: Bearer ***")
        self.assertEqual(sanitizar("{'Authorization': 'Bearer abc123'}"), "{'Authorization': 'Bearer ***'}")

    def test_chaves(self):
        self.assertEqual(sanitizar("WEBUI_API_TOKEN=abc123 carregado"), "WEBUI_API_TOKEN=*** carregado")
        self.assertEqual(sanitizar("EVOLUTION_API_KEY: k-999"), "EVOLUTION_API_KEY: ***")
        self.assertEqual(sanitizar("GET http://evo:8080/chat?apikey=XYZ789&n=1 → 401"),
                         "GET http://evo:8080/chat?apikey=***&n=1 → 401")
        self.assertEqual(sanitizar('{"apikey": "segredo", "numero": 2}'), '{"apikey": "***", "numero": 2}')
        self.assertEqual(sanitizar("{'access_token': 'tok', 'x': 1}"), "{'access_token': '***', 'x': 1}")

    def test_telefones(self):
        self.assertEqual(sanitizar("(simulado) texto → 5511987651234: oi"), "(simulado) texto → 55119****1234: oi")
        self.assertEqual(sanitizar("jid 5511987651234@s.whatsapp.net"), "jid 55119****1234@s.whatsapp.net")
        self.assertEqual(sanitizar("fixo 551187651234"), "fixo 55118***1234")
        self.assertEqual(sanitizar("ligue +55 (11) 98765-1234"), "ligue +55119****1234")
        self.assertEqual(sanitizar("ou +5511987651234."), "ou +55119****1234.")

    def test_emails(self):
        self.assertEqual(sanitizar("de pedro@gmail.com"), "de p***@gmail.com")
        self.assertEqual(sanitizar("Fulano <fulano.tal+news@empresa.com.br>"), "Fulano <f***@empresa.com.br>")
        # JID de grupo e de contato não é e-mail
        self.assertEqual(sanitizar("grupo 120363012345678901@g.us"), "grupo 120363012345678901@g.us")

    def test_resto_intacto(self):
        for t in ("[classif   1/ 30] promocao     0.95 gmail.com Oferta 50% off",
                  "yaml.dump(..., sort_keys=False) às 13:17:00, ts=1730390000000",
                  "[jornal] Markdown salvo: 2026-10-31_jornal.md",
                  "pendência a1b2 → vou (via whatsapp)"):
            self.assertEqual(sanitizar(t), t)

    def test_log_passa_pelo_sanitizar(self):
        out = io.StringIO()
        with redirect_stdout(out):
            lib_alfred.log("enviando para 5511987651234 com Bearer abc e cc pedro@gmail.com")
        linha = out.getvalue()
        self.assertIn("55119****1234", linha)
        self.assertIn("Bearer ***", linha)
        self.assertIn("p***@gmail.com", linha)
        self.assertNotIn("987651234", linha)
        self.assertNotIn("abc", linha)


# ══════════════════════════════════════════════════════════════
#  gravar_atomico
# ══════════════════════════════════════════════════════════════

class _MeioEscrito:
    """Arquivo que grava metade do conteúdo e falha com disco cheio."""

    def __init__(self, fh):
        self.fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()

    def write(self, dados):
        self.fh.write(dados[: len(dados) // 2])
        self.fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device")


class TestGravarAtomico(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-robustez-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.f = self.tmp / "estado.json"
        self.f.write_text('{"versao": 1}', encoding="utf-8")

    def _temporarios(self):
        return [p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")]

    def test_core_reaproveita_lib_alfred(self):
        self.assertIs(core.gravar_atomico, lib_alfred.gravar_atomico)

    def test_grava_e_mantem_permissoes(self):
        os.chmod(self.f, 0o640)
        gravar_atomico(self.f, '{"versao": 2, "nome": "João"}')
        self.assertEqual(json.loads(self.f.read_text(encoding="utf-8")), {"versao": 2, "nome": "João"})
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o640)
        gravar_atomico(self.tmp / "sub" / "bin.dat", b"\x00\x01\xff")   # bytes e pasta nova
        self.assertEqual((self.tmp / "sub" / "bin.dat").read_bytes(), b"\x00\x01\xff")
        self.assertEqual(self._temporarios(), [])

    def test_falha_no_meio_da_escrita_preserva_o_original(self):
        fdopen_real = os.fdopen
        with mock.patch.object(lib_alfred.os, "fdopen", side_effect=lambda fd, modo: _MeioEscrito(fdopen_real(fd, modo))):
            with self.assertRaises(OSError):
                gravar_atomico(self.f, json.dumps({"versao": 2, "lista": list(range(500))}))
        self.assertEqual(self.f.read_text(encoding="utf-8"), '{"versao": 1}')
        self.assertEqual(self._temporarios(), [])

    def test_falha_na_troca_preserva_o_original(self):
        with mock.patch.object(lib_alfred.os, "replace", side_effect=OSError(errno.EIO, "erro de E/S")):
            with self.assertRaises(OSError):
                gravar_atomico(self.f, '{"versao": 2}')
        self.assertEqual(self.f.read_text(encoding="utf-8"), '{"versao": 1}')
        self.assertEqual(self._temporarios(), [])

    def test_save_seen_e_atomico(self):
        seen = self.tmp / "inbox_seen.json"
        core.save_seen({"a@b": "2026-10-31T10:00:00"}, seen)
        with mock.patch.object(lib_alfred.os, "replace", side_effect=OSError(errno.EIO, "erro de E/S")):
            with self.assertRaises(OSError):
                core.save_seen({"c@d": "2026-10-31T11:00:00"}, seen)
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8")), {"a@b": "2026-10-31T10:00:00"})


# ══════════════════════════════════════════════════════════════
#  backup → restore
# ══════════════════════════════════════════════════════════════

class TestBackupRestore(unittest.TestCase):

    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.raiz, self.vault, self.destino = self.tmp / "repo", self.tmp / "vault", self.tmp / "backups"
        arquivos = {
            self.raiz / "logs" / "inbox_seen.json": '{"x@y": "2026-10-31T10:00:00"}',
            self.raiz / "services" / "_shared" / "perfis.yml": "perfis:\n  pedro_dev: {}\n",
            self.raiz / ".env": "WEBUI_API_TOKEN=nao-pode-ir-pro-backup\n",
            self.raiz / "data" / "chromadb" / "chroma.bin": "volume do docker",
            self.raiz / "services" / "inbox" / "core.py": "# código: fora do backup\n",
            self.vault / "pendencias" / "pendencias.yml": "pendencias: []\n",
            self.vault / "pendencias" / ".pendencias.yml.lock": "",
            self.vault / "agenda" / "agenda.yml": "itens: []\n",
            self.vault / "memoria" / "pedro.md": "# Memória\n- gosta de café\n",
            self.vault / "jornal" / ".2026-10-31.json.ab12cd34.tmp": "{meio escrito",
        }
        for f, txt in arquivos.items():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(txt, encoding="utf-8")
        # banco em WAL com as linhas ainda só no -wal e a conexão aberta: o backup precisa pegá-las
        db = self.vault / ".alfred" / "alfred.db"
        db.parent.mkdir(parents=True)
        self.con = sqlite3.connect(db)
        self.addCleanup(self.con.close)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA wal_autocheckpoint=0")
        self.con.execute("CREATE TABLE notas (texto TEXT)")
        self.con.executemany("INSERT INTO notas VALUES (?)", [("um",), ("dois",), ("três",)])
        self.con.commit()
        self.assertTrue(Path(f"{db}-wal").stat().st_size > 0)

    def _criar(self, **kw):
        return backup.criar_backup(self.destino, raiz=self.raiz, vault=self.vault, **kw)

    def test_conteudo_do_backup(self):
        arq = self._criar()
        self.assertRegex(arq.name, r"^alfred-backup-\d{4}-\d{2}-\d{2}_\d{6}\.tar\.gz$")
        self.assertEqual(arq.stat().st_mode & 0o777, 0o600)
        with tarfile.open(arq) as tar:
            nomes = set(tar.getnames())
            manifesto = json.load(tar.extractfile("MANIFESTO.json"))
            copia = self.tmp / "copia.db"
            copia.write_bytes(tar.extractfile("vault/.alfred/alfred.db").read())
        self.assertEqual(nomes, {
            "MANIFESTO.json", "repo/logs/inbox_seen.json", "repo/services/_shared/perfis.yml",
            "vault/pendencias/pendencias.yml", "vault/agenda/agenda.yml", "vault/memoria/pedro.md",
            "vault/.alfred/alfred.db",
        })   # sem .env, data/, código, .lock, .tmp, -wal/-shm
        self.assertEqual(manifesto["bancos"], ["vault/.alfred/alfred.db"])
        with sqlite3.connect(copia) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM notas").fetchone()[0], 3)
        self.assertEqual([p.name for p in self.destino.iterdir()], [arq.name])   # sem .parcial nem cópias

    def test_mantem_so_os_mais_novos(self):
        self.destino.mkdir()
        for d in ("01", "02", "03"):
            (self.destino / f"alfred-backup-2020-01-{d}_000000.tar.gz").write_bytes(b"velho")
        arq = self._criar(manter=2)
        self.assertEqual(sorted(p.name for p in self.destino.glob("alfred-backup-*")),
                         ["alfred-backup-2020-01-03_000000.tar.gz", arq.name])

    def test_restore_dry_run_e_completo(self):
        arq = self._criar()
        raiz2, vault2 = self.tmp / "repo2", self.tmp / "vault2"

        res = backup.restaurar(arq, raiz=raiz2, vault=vault2, dry_run=True)
        self.assertFalse(res["restaurado"])
        self.assertEqual(len(res["novos"]), 6)
        self.assertFalse(raiz2.exists() or vault2.exists())

        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="não"):
            self.assertFalse(backup.restaurar(arq, raiz=raiz2, vault=vault2)["restaurado"])
        self.assertFalse(raiz2.exists() or vault2.exists())

        (vault2 / "pendencias").mkdir(parents=True)
        (vault2 / "pendencias" / "pendencias.yml").write_text("pendencias: [estragado]\n", encoding="utf-8")
        (vault2 / "pendencias" / "extra.md").write_text("fica", encoding="utf-8")
        (vault2 / ".alfred").mkdir()
        (vault2 / ".alfred" / "alfred.db-wal").write_bytes(b"wal velho de outro banco")
        res = backup.restaurar(arq, raiz=raiz2, vault=vault2, sim=True)
        self.assertTrue(res["restaurado"])
        self.assertEqual(res["sobrescritos"], [vault2 / "pendencias" / "pendencias.yml"])
        for rel in ("logs/inbox_seen.json", "services/_shared/perfis.yml"):
            self.assertEqual((raiz2 / rel).read_text(encoding="utf-8"), (self.raiz / rel).read_text(encoding="utf-8"))
        for rel in ("pendencias/pendencias.yml", "agenda/agenda.yml", "memoria/pedro.md"):
            self.assertEqual((vault2 / rel).read_text(encoding="utf-8"), (self.vault / rel).read_text(encoding="utf-8"))
        self.assertEqual((vault2 / "pendencias" / "extra.md").read_text(encoding="utf-8"), "fica")   # não apaga a mais
        self.assertFalse((vault2 / ".alfred" / "alfred.db-wal").exists())
        self.assertFalse((raiz2 / ".env").exists() or (raiz2 / "data").exists())
        with sqlite3.connect(vault2 / ".alfred" / "alfred.db") as c:
            self.assertEqual(sorted(r[0] for r in c.execute("SELECT texto FROM notas")), ["dois", "três", "um"])

    def test_restore_recusa_caminho_fora_do_destino(self):
        arq = self.tmp / "malicioso.tar.gz"
        with tarfile.open(arq, "w:gz") as tar:
            for nome, dados in (("MANIFESTO.json", b'{"criado_em": "x", "vault": null, "bancos": []}'),
                                ("repo/../../fora.txt", b"escapou")):
                info = tarfile.TarInfo(nome)
                info.size = len(dados)
                tar.addfile(info, io.BytesIO(dados))
        with self.assertRaises(ValueError):
            backup.restaurar(arq, raiz=self.tmp / "repo2", vault=self.tmp / "vault2", sim=True)
        self.assertFalse((self.tmp / "fora.txt").exists())


if __name__ == "__main__":
    unittest.main()
