"""
Testes do token semanal do aplicativo do celular (app_token.py): faixa da tailnet, validade de 7 dias, arquivo
600, envio ao WhatsApp de Pedro uma vez só e fora do feed de avisos.

    cd services/inbox
    venv/bin/python -m unittest tests.test_app_token -v

Offline: arquivo temporário, envio e sessões do WhatsApp simulados por mock. O login e a sessão do aplicativo no
Painel estão em tests.test_painel_app.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

os.environ.setdefault("SHELL_EXECUTOR_TOKEN", "token-de-teste")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

import app_token  # noqa: E402
import core  # noqa: E402
import whatsapp  # noqa: E402
from _shared import lib_db, lib_whatsapp  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="alfred-app-token-test-"))
PEDRO = "5514999990000"
DIA = 86400


class Base(unittest.TestCase):
    HOSTS = "100.95.104.56, BatComputador ,"

    def setUp(self):
        for f in TMP.glob("*"):
            f.unlink()
        self.envios = mock.Mock(return_value={"ok": True})
        self.notificacoes = mock.Mock()
        for p in (mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": self.HOSTS, "PORT_INBOX": "7073",
                                               "WHATSAPP_NUMERO_PEDRO": PEDRO, "ALFRED_DB": str(TMP / "alfred.db")}),
                  mock.patch.object(app_token, "ARQUIVO", TMP / "app_token.json"),
                  mock.patch.object(lib_whatsapp, "simulado", lambda: False),
                  mock.patch.object(lib_whatsapp, "enviar_texto", self.envios),
                  mock.patch.object(whatsapp, "sessoes", lambda: [{"instancia": "alfred", "papel": "pessoal"},
                                                                  {"instancia": "alfred-assistente", "papel": "assistente"}]),
                  mock.patch.object(core, "_notify", self.notificacoes)):
            p.start()
            self.addCleanup(p.stop)


class TestToken(Base):
    def test_tailnet_e_hosts(self):
        for ip, esperado in (("100.117.66.32", True), ("100.64.0.1", True), ("100.127.255.254", True),
                             ("100.63.255.255", False), ("100.128.0.1", False), ("192.168.1.2", False),
                             ("127.0.0.1", False), ("", False), ("batcelular", False), ("fd7a:115c:a1e0::1", False)):
            self.assertEqual(app_token.da_tailnet(ip), esperado, ip)
        self.assertEqual(app_token.hosts(), ("100.95.104.56", "batcomputador"))
        self.assertEqual(app_token.url(), "http://100.95.104.56:7073")
        with mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": " "}):
            self.assertEqual((app_token.hosts(), app_token.url()), ((), ""))

    def test_renovar_gera_token_novo_e_arquivo_600(self):
        self.assertIsNone(app_token.atual())
        a = app_token.renovar()
        b = app_token.renovar()
        self.assertNotEqual(a["token"], b["token"])
        self.assertGreaterEqual(len(b["token"]), 32)
        self.assertEqual(b["expira_em"] - b["criado_em"], 7 * DIA)
        self.assertEqual(app_token.atual()["token"], b["token"])
        self.assertEqual(stat.S_IMODE(app_token.ARQUIVO.stat().st_mode), 0o600)
        self.assertEqual([f.name for f in TMP.glob("*")], ["app_token.json"])     # nenhum temporário para trás
        self.assertIsNone(app_token.conferir(a["token"]))                          # o anterior deixou de valer
        self.assertEqual(app_token.conferir(b["token"])["token"], b["token"])

    def test_conferir_token_errado_vazio_ou_vencido(self):
        d = app_token.renovar()
        for errado in ("", "errado", d["token"][:-1], d["token"] + "x", "çãõ\udcff"):
            self.assertIsNone(app_token.conferir(errado), errado)
        velho = app_token.renovar(time.time() - 7 * DIA - 1)
        self.assertIsNone(app_token.conferir(velho["token"]))
        self.assertIsNone(app_token.atual())
        app_token.ARQUIVO.write_text("{isto não é json", encoding="utf-8")        # arquivo estragado: sem token
        self.assertIsNone(app_token.atual())


class TestPassar(Base):
    def test_sem_hosts_nao_faz_nada(self):
        with mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": ""}):
            app_token.passar()
        self.assertFalse(app_token.ARQUIVO.exists())
        self.envios.assert_not_called()

    def test_envia_uma_vez_pelo_numero_do_alfred_e_fora_do_feed(self):
        t0 = time.time()
        app_token.passar(t0)
        token = app_token.atual()["token"]
        (aviso, kw1), (so_o_token, kw2) = [(c.args, c.kwargs) for c in self.envios.call_args_list]
        self.assertEqual((aviso[0], so_o_token), (PEDRO, (PEDRO, token)))
        self.assertIn("http://100.95.104.56:7073", aviso[1])
        self.assertNotIn(token, aviso[1])
        self.assertEqual((kw1, kw2), ({"inst": "alfred-assistente", "aviso": False},) * 2)
        self.assertEqual(lib_db.avisos(), [])                                     # o token nunca entra no feed
        (n,) = self.notificacoes.call_args_list                                   # o desktop avisa, sem o token
        self.assertNotIn(token, " ".join(n.args))
        self.assertEqual(n.kwargs, {"aviso": False})
        app_token.passar(t0 + 60)
        app_token.passar(t0 + DIA)
        self.assertEqual(self.envios.call_count, 2)                               # não repete
        self.assertEqual(app_token.atual()["enviado_em"], t0)

    def test_envio_que_falha_tenta_de_novo_depois(self):
        self.envios.return_value = {"ok": False, "erro": "fora do ar"}
        t0 = time.time()
        app_token.passar(t0)
        app_token.passar(t0 + 60)                                                 # cedo demais: não insiste a cada volta
        self.assertEqual(self.envios.call_count, 1)
        self.envios.return_value = {"ok": True}
        app_token.passar(t0 + app_token.NOVA_TENTATIVA_S + 1)
        self.assertEqual(self.envios.call_count, 3)
        self.assertTrue(app_token.atual()["enviado_em"])

    def test_sem_sessao_assistente_nao_envia(self):
        with mock.patch.object(whatsapp, "sessoes", lambda: [{"instancia": "alfred", "papel": "pessoal"}]):
            app_token.passar()
        self.envios.assert_not_called()
        self.assertTrue(app_token.atual())                                        # o token existe: está no Painel

    def test_renova_quando_vence_e_quando_pedro_gera_outro(self):
        t0 = time.time()
        app_token.passar(t0)
        a = app_token.atual()["token"]
        app_token.passar(t0 + 7 * DIA + 1)
        b = app_token.atual(t0 + 7 * DIA + 2)["token"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.envios.call_args.args, (PEDRO, b))
        c = app_token.renovar(t0 + 8 * DIA)["token"]                              # o botão do Painel
        app_token.passar(t0 + 8 * DIA + 60)
        self.assertEqual(self.envios.call_args.args, (PEDRO, c))
        self.assertEqual(self.envios.call_count, 6)

    def test_simulado_nao_envia_o_token(self):
        """O envio simulado grava o texto em logs/whatsapp_enviados.jsonl: o token não pode ir para lá."""
        with mock.patch.object(lib_whatsapp, "simulado", lambda: True):
            app_token.passar()
        self.envios.assert_not_called()
        self.assertTrue(app_token.atual())


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
