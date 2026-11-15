"""
Testes do feed de avisos (lib_db.avisos) nos dois pontos por onde quase tudo passa: o que o Alfred manda ao
WhatsApp de Pedro (lib_whatsapp.enviar_texto) e a notificação simples do desktop (core._notify). O aplicativo do
celular lê esse feed.

    cd services/inbox
    venv/bin/python -m unittest tests.test_avisos -v

A pendência, a dose de remédio e o item da agenda são testados com o ambiente de cada um: tests.test_whatsapp
(TestAvisos), tests.test_remedios (TestAvisos) e tests.test_agenda (TestNotificacao).

Offline: banco temporário, envios simulados, nenhum notify-send de verdade.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

os.environ.setdefault("SHELL_EXECUTOR_TOKEN", "token-de-teste")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

import core  # noqa: E402
from _shared import lib_db, lib_whatsapp  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="alfred-avisos-test-"))
PEDRO = "5514999990000"
OUTRO = "5514988887777"


class TestGanchos(unittest.TestCase):
    def setUp(self):
        for f in TMP.glob("*"):
            f.unlink()
        for p in (mock.patch.dict(os.environ, {"ALFRED_DB": str(TMP / "alfred.db"), "EVOLUTION_URL": "",
                                               "WHATSAPP_NUMERO_PEDRO": PEDRO}),
                  mock.patch.object(lib_whatsapp, "ENVIADOS_FILE", TMP / "enviados.jsonl"),
                  mock.patch.object(lib_whatsapp, "IDS_ENVIADOS_FILE", TMP / "ids_enviados.json"),
                  mock.patch.object(core.shutil, "which", lambda *a, **k: None)):   # sem notify-send de verdade
            p.start()
            self.addCleanup(p.stop)

    def test_whatsapp_para_pedro_entra_no_feed(self):
        self.assertTrue(lib_whatsapp.enviar_texto(PEDRO, "💊 Hora do *Celecoxibe*. Já tomou?")["ok"])
        (a,) = lib_db.avisos()
        self.assertEqual((a["tipo"], a["titulo"], a["texto"], a["link"]),
                         ("whatsapp", "WhatsApp", "💊 Hora do *Celecoxibe*. Já tomou?", ""))
        lib_whatsapp.enviar_texto(OUTRO, "Resposta ao contato")             # só o que vai para Pedro
        lib_whatsapp.enviar_texto(PEDRO, "o token da semana", aviso=False)  # quem chama pediu para não copiar
        self.assertEqual(len(lib_db.avisos()), 1)

    def test_envio_real_so_entra_se_saiu(self):
        """A Evolution fora do ar e o envio repetido a cada volta do laço não podem encher o feed."""
        with mock.patch.object(lib_whatsapp, "simulado", lambda: False), \
                mock.patch.object(lib_whatsapp, "_post", return_value={"ok": False, "erro": "fora do ar"}):
            self.assertFalse(lib_whatsapp.enviar_texto(PEDRO, "Já tomou?")["ok"])
            self.assertEqual(lib_db.avisos(), [])
        with mock.patch.object(lib_whatsapp, "simulado", lambda: False), \
                mock.patch.object(lib_whatsapp, "_post", return_value={"ok": True, "resposta": {"key": {"id": "A1"}}}):
            self.assertTrue(lib_whatsapp.enviar_texto(PEDRO, "Já tomou?")["ok"])
        self.assertEqual([a["texto"] for a in lib_db.avisos()], ["Já tomou?"])

    def test_feed_fora_do_ar_nao_quebra_o_envio(self):
        with mock.patch.object(lib_db, "_conectar", lambda: None):
            self.assertTrue(lib_whatsapp.enviar_texto(PEDRO, "oi")["ok"])
            core._notify("Alfred — Jornal do dia", "12 notícias")
        self.assertEqual(lib_db.avisos(), [])

    def test_notify_entra_no_feed_com_o_link_do_painel(self):
        core._notify("Alfred — e-mail aguardando", "Proposta\nPronto para revisar\nhttp://127.0.0.1:7073/painel/redigir")
        core._notify("Alfred — Jornal do dia", "12 notícias de 4 newsletters")
        core._notify("Alfred — agenda", "Abra http://127.0.0.1:7073/painel/agenda/a1 no navegador.", aviso=False)
        avisos = lib_db.avisos()
        self.assertEqual([(a["tipo"], a["titulo"], a["link"]) for a in avisos],
                         [("aviso", "e-mail aguardando", "/painel/redigir"), ("aviso", "Jornal do dia", "")])
        self.assertIn("Pronto para revisar", avisos[0]["texto"])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
