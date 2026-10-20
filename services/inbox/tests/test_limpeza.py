"""
Testes da limpeza pós-podcast (limpeza.py + endpoints /limpeza/* + executor da extensão).

    cd services/inbox
    venv/bin/python -m unittest tests.test_limpeza -v

Ambiente 100% sintético: perfil do Thunderbird falso (prefs.js + mbox) num diretório temporário,
vault, manifestos e registro de categorias temporários. Nenhuma caixa real é lida ou alterada.
O executor da extensão (thunderbird-ext/executor.js) roda no node com um `messenger` simulado.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TMP = Path(tempfile.mkdtemp(prefix="alfred-limpeza-test-"))
PERFIL = TMP / "tb-profile"
ENV = {
    "VAULT_ALFRED": str(TMP / "vault"),
    "VAULT_NOTICIAS": str(TMP / "noticias"),
    "THUNDERBIRD_PROFILE": str(PERFIL),
    "SHELL_EXECUTOR_TOKEN": "token-de-teste",
    "INBOX_DEFAULT": "pedro_hotmail",
}
os.environ.update(ENV)   # outros módulos de teste também mexem no ambiente → reaplicado em cada setUp

import core  # noqa: E402
import limpeza  # noqa: E402

AGORA = datetime.now().astimezone()


def _data(dias: int) -> datetime:
    return AGORA - timedelta(days=dias)


# (message_id, remetente, assunto, dias atrás, categoria registrada ou None)
EMAILS = [
    ("promo-velha@loja.com",   "ofertas@loja.com",        "50% OFF só hoje",          60, "promocao"),
    ("promo-nova@loja.com",    "ofertas@loja.com",        "Frete grátis",              3, "promocao"),
    ("trans-velha@banco.com",  "naoresponda@banco.com",   "Seu extrato chegou",       45, "transacional"),
    ("contato-velho@x.com",    "recrutadora@empresa.com", "Vaga de engenheiro",       90, "contato"),
    ("evento-velho@x.com",     "meetup@comunidade.org",   "Convite: meetup",          90, "evento"),
    ("pessoal@x.com",          "mae@gmail.com",           "Almoço domingo",           90, "pessoal"),
    ("news-podcast@tldr.tech", "dan@tldr.tech",           "TLDR 2026-10-12",           7, "noticia"),
    ("contato-no-manif@x.com", "chefe@empresa.com",       "Reunião",                   7, "contato"),
    ("ignorado-sem-reg@kabum", "promo@promo.kabum.com.br","Kabum ofertas",            40, None),
    ("sem-reg@desconhecido",   "alguem@desconhecido.com", "Olá",                      80, None),
    ("pendente@x.com",         "ofertas@loja.com",        "Promo com pendência",      60, "promocao"),
]


def _mbox(emails) -> bytes:
    partes = []
    for mid, rem, assunto, dias, _cat in emails:
        d = _data(dias)
        partes.append(
            f"From - {d:%a %b %d %H:%M:%S %Y}\n"
            f"X-Mozilla-Status: 0001\n"
            f"Message-ID: <{mid}>\nFrom: {rem}\nTo: pedro@hotmail.com\nSubject: {assunto}\n"
            f"Date: {d:%a, %d %b %Y %H:%M:%S %z}\nContent-Type: text/plain; charset=utf-8\n\ncorpo de {assunto}\n\n"
        )
    return "".join(partes).encode("utf-8")


def montar_ambiente():
    if TMP.exists():
        for sub in ("vault", "noticias", "tb-profile"):
            shutil.rmtree(TMP / sub, ignore_errors=True)
    conta = PERFIL / "ImapMail" / "outlook.office365.com"
    conta.mkdir(parents=True)
    (PERFIL / "prefs.js").write_text(
        'user_pref("mail.server.server1.type", "imap");\n'
        'user_pref("mail.server.server1.hostname", "outlook.office365.com");\n'
        'user_pref("mail.server.server1.name", "pedro@hotmail.com");\n'
        f'user_pref("mail.server.server1.directory", "{conta}");\n', encoding="utf-8")
    (conta / "INBOX").write_bytes(_mbox(EMAILS))
    (TMP / "vault" / "pendencias").mkdir(parents=True)

    man = TMP / "noticias" / "podcast" / "manifestos"
    man.mkdir(parents=True)
    (man / "2026-W42.json").write_text(json.dumps({
        "semana": "2026-W42", "episodio_md": "podcast/2026-W42.md", "episodio_mp3": "podcast/2026-W42.mp3",
        "gerado_em": AGORA.isoformat(),
        "emails": [{"message_id": "news-podcast@tldr.tech", "from": "dan@tldr.tech"},
                   {"message_id": "contato-no-manif@x.com", "from": "chefe@empresa.com"}],
    }), encoding="utf-8")
    # manifesto sem episódio gerado não autoriza nada
    (man / "2026-W43.json").write_text(json.dumps({
        "semana": "2026-W43", "emails": [{"message_id": "sem-reg@desconhecido"}]}), encoding="utf-8")

    reg = {mid: {"categoria": cat, "registrado_em": AGORA.isoformat()} for mid, _r, _a, _d, cat in EMAILS if cat}
    limpeza.REGISTRO_FILE.write_text(json.dumps(reg), encoding="utf-8")

    core._save_pend({"pendencias": [
        {"id": "p-aguardando", "tipo": "evento", "status": "aguardando", "titulo": "x",
         "email": {"message_id": "pendente@x.com"}},
    ]})


class BaseLimpeza(unittest.TestCase):
    def setUp(self):
        mock.patch.dict(os.environ, ENV).start()
        self.reg_patch = mock.patch.object(limpeza, "REGISTRO_FILE", TMP / "inbox_categorias.json")
        self.reg_patch.start()
        self.notify = mock.patch.object(core, "_notify").start()
        montar_ambiente()

    def tearDown(self):
        mock.patch.stopall()


class TestCandidatos(BaseLimpeza):
    def test_dry_run_seleciona_so_o_permitido_com_motivo(self):
        c = limpeza.candidatos(incluir_mantidos=True)
        mids = {i["message_id"]: i["motivo"] for i in c["candidatos"]}
        self.assertEqual(set(mids), {"promo-velha@loja.com", "trans-velha@banco.com",
                                     "news-podcast@tldr.tech", "ignorado-sem-reg@kabum"})
        self.assertIn("2026-W42", mids["news-podcast@tldr.tech"])
        self.assertTrue(mids["promo-velha@loja.com"].startswith("promocao há"))
        self.assertTrue(mids["ignorado-sem-reg@kabum"].startswith("ignorado há"))
        self.assertTrue(c["dry_run"])
        self.assertFalse(limpeza.lotes_file().exists(), "dry-run não pode gravar nada")

    def test_protecoes_vencem_o_manifesto(self):
        c = limpeza.candidatos(incluir_mantidos=True)
        mids = {i["message_id"] for i in c["candidatos"]}
        for protegido in ("contato-velho@x.com", "evento-velho@x.com", "pessoal@x.com",
                          "contato-no-manif@x.com", "pendente@x.com", "promo-nova@loja.com", "sem-reg@desconhecido"):
            self.assertNotIn(protegido, mids)
        self.assertIn("ligado a pendência aguardando", c["mantidos"])

    def test_dias_minimos_configuravel(self):
        _n, cfg, lc = limpeza.config()
        lc = {**lc, "dias_minimos": 2}
        with mock.patch.object(limpeza, "config", return_value=("pedro_hotmail", cfg, lc)):
            mids = {i["message_id"] for i in limpeza.candidatos()["candidatos"]}
        self.assertIn("promo-nova@loja.com", mids)

    def test_categorias_proibidas_na_config_sao_descartadas(self):
        nome, cfg = core.carregar_cfg()
        cfg = {**cfg, "limpeza": {"categorias": ["promocao", "contato", "pessoal"]}}
        with mock.patch.object(core, "carregar_cfg", return_value=(nome, cfg)):
            _n, _c, lc = limpeza.config()
        self.assertEqual(lc["categorias"], ["promocao"])

    def test_registrar_classificacoes(self):
        limpeza.registrar_classificacoes([{"message_id": "novo@x", "categoria": "promocao", "from": "a@b"}])
        self.assertEqual(limpeza.carregar_registro()["novo@x"]["categoria"], "promocao")


class TestFluxoAprovacao(BaseLimpeza):
    def test_ponta_a_ponta_via_api(self):
        from fastapi.testclient import TestClient
        import main
        main.TOKEN = "token-de-teste"
        cli = TestClient(main.app)
        h = {"Authorization": "Bearer token-de-teste"}

        self.assertEqual(cli.get("/limpeza/candidatos").status_code, 401)
        self.assertEqual(cli.get("/limpeza/candidatos", headers=h).json()["total"], 4)

        lote = cli.post("/limpeza/propor?notificar=false", headers=h).json()["lote"]
        self.assertEqual(lote["status"], "aguardando")
        self.assertTrue((core.vault() / lote["arquivos"]["md"]).is_file())
        # sem aprovação a extensão não recebe nada
        self.assertEqual(cli.get("/limpeza/fila", headers=h).json()["lotes"], [])
        # novos candidatos não duplicam itens de lote aberto
        self.assertEqual(cli.get("/limpeza/candidatos", headers=h).json()["total"], 0)

        # entre a proposta e a aprovação, um item ganhou pendência aguardando → sai na revalidação
        data = core._load_pend()
        data["pendencias"].append({"id": "p2", "tipo": "contato", "status": "aguardando", "titulo": "y",
                                   "email": {"message_id": "trans-velha@banco.com"}})
        core._save_pend(data)

        r = cli.post(f"/limpeza/{lote['id']}/aprovar", headers=h, json={"nota": "ok"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 3)
        self.assertEqual(r.json()["removidos_na_aprovacao"][0]["message_id"], "trans-velha@banco.com")
        self.assertEqual(cli.post(f"/limpeza/{lote['id']}/aprovar", headers=h, json={}).status_code, 400)

        fila = cli.get("/limpeza/fila", headers=h).json()["lotes"]
        self.assertEqual(len(fila), 1)
        self.assertEqual(fila[0]["acao"], "mover_para_lixeira")
        self.assertEqual(cli.get("/limpeza/fila", headers=h).json()["lotes"], [], "não entrega o lote duas vezes")

        itens = [{"message_id": i["message_id"], "status": "movido"} for i in fila[0]["itens"]]
        itens[0]["status"] = "recusado"
        itens.append({"message_id": "contato-velho@x.com", "status": "movido"})  # fora do lote → ignorado
        res = cli.post(f"/limpeza/{lote['id']}/resultado", headers=h, json={"itens": itens}).json()
        self.assertEqual(res["status"], "parcial")
        self.assertEqual(res["contagem"], {"recusado": 1, "movido": 2})
        self.assertEqual(cli.get(f"/limpeza/{lote['id']}", headers=h).json()["status"], "parcial")

    def test_recusar_e_lote_inexistente(self):
        lote = limpeza.propor(notificar=False)
        limpeza.recusar(lote["id"])
        with self.assertRaises(ValueError):
            limpeza.aprovar(lote["id"])
        with self.assertRaises(KeyError):
            limpeza.aprovar("limpeza-nao-existe")
        self.assertEqual(limpeza.fila_execucao(), [])

    def test_lote_expirado_nao_aprova(self):
        lote = limpeza.propor(notificar=False)
        data = limpeza._load_lotes()
        data["lotes"][0]["expira_em"] = (AGORA - timedelta(hours=1)).isoformat()
        limpeza._save_lotes(data)
        with self.assertRaises(ValueError):
            limpeza.aprovar(lote["id"])
        self.assertEqual(limpeza.obter_lote(lote["id"])["status"], "expirado")

    def test_simulado_pela_extensao(self):
        lote = limpeza.propor(notificar=False)
        limpeza.aprovar(lote["id"])
        fila = limpeza.fila_execucao()
        res = limpeza.registrar_resultado(lote["id"], [{"message_id": i["message_id"], "status": "simulado"}
                                                        for i in fila[0]["itens"]])
        self.assertEqual(res["status"], "simulado")

    def test_cli(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("inbox_assistant", INBOX / "inbox-assistant.py")
        ia = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ia)
        with mock.patch.object(sys, "argv", ["x", "limpeza", "propor", "--sem-notificar"]):
            self.assertEqual(ia.main(), 0)
        lid = limpeza.listar_lotes("aguardando")[0]["id"]
        with mock.patch.object(sys, "argv", ["x", "limpeza", "aprovar", lid]):
            self.assertEqual(ia.main(), 0)
        self.assertEqual(limpeza.obter_lote(lid)["status"], "aprovado")


@unittest.skipUnless(shutil.which("node"), "node não instalado")
class TestExecutorExtensao(unittest.TestCase):
    def test_executor_js(self):
        r = subprocess.run(["node", "--test", str(AQUI / "test_executor.mjs")], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
