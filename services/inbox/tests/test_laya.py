"""
Testes do cliente do Laya (services/_shared/lib_laya.py): formato da requisição, limiar de confiança,
LAYA_DESLIGADO e fallback (serviço fora do ar / erro HTTP → None, sem exceção).

    cd services/inbox
    venv/bin/python -m unittest tests.test_laya -v

Offline: httpx.Client mockado. A validação com o modelo real fica no about.md do services/laya.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from _shared import lib_laya  # noqa: E402


def _cliente(answers=None, erro=None):
    """httpx.Client falso: devolve `answers` no POST ou levanta `erro`."""
    resp = mock.Mock()
    resp.json.return_value = {"answers": answers or {}}
    resp.raise_for_status.return_value = None
    client = mock.MagicMock()
    client.__enter__.return_value = client
    if erro:
        client.post.side_effect = erro
    else:
        client.post.return_value = resp
    return client


class TestLaya(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"PORT_LAYA": "7999"}, clear=False)
        self.env.start()
        os.environ.pop("LAYA_DESLIGADO", None)
        os.environ.pop("LAYA_CONFIANCA_MINIMA", None)
        os.environ.pop("LAYA_URL", None)

    def tearDown(self):
        self.env.stop()

    def test_escolher_envia_choice_e_devolve_rotulo(self):
        c = _cliente({"q": {"choice": "trabalho", "confidence": 0.9}})
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            r = lib_laya.escolher("reunião de sprint", "Categoria?", {"trabalho": "job", "pessoal": "vida"})
        self.assertEqual(r, "trabalho")
        url, = c.post.call_args.args
        corpo = c.post.call_args.kwargs["json"]
        self.assertEqual(url, "http://127.0.0.1:7999/v1/systemone")
        self.assertEqual(corpo["state"], "reunião de sprint")
        self.assertEqual(corpo["questions"]["q"]["type"], "choice")
        self.assertEqual(corpo["questions"]["q"]["criteria"], {"trabalho": "job", "pessoal": "vida"})

    def test_confianca_baixa_devolve_none(self):
        c = _cliente({"q": {"choice": "trabalho", "confidence": 0.4}})
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertIsNone(lib_laya.escolher("x", "?", {"a": "", "b": ""}))

    def test_limiar_usa_answer_confidence(self):
        c = _cliente({"q": {"choice": "pessoal", "confidence": 0.5, "answer_confidence": 0.86}})
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertEqual(lib_laya.escolher("x", "?", {"pessoal": "", "trabalho": ""}), "pessoal")

    def test_limiar_configuravel(self):
        os.environ["LAYA_CONFIANCA_MINIMA"] = "0.3"
        c = _cliente({"q": {"score": 2.0, "confidence": 0.4}})
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertEqual(lib_laya.pontuar("x", "?", ["baixa", "média", "alta"]), 2.0)

    def test_sim_nao_devolve_probabilidade(self):
        c = _cliente({"q": {"noul": 0.87}})
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertAlmostEqual(lib_laya.sim_nao("amanhã 14h reunião", "É um convite?"), 0.87)
        self.assertEqual(c.post.call_args.kwargs["json"]["questions"]["q"]["type"], "noul")

    def test_fora_do_ar_devolve_none(self):
        c = _cliente(erro=lib_laya.httpx.ConnectError("recusado"))
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c), mock.patch.object(lib_laya, "log"):
            self.assertIsNone(lib_laya.sim_nao("x", "?"))
            self.assertIsNone(lib_laya.escolher("x", "?", {"a": ""}))
            self.assertIsNone(lib_laya.decidir("x", {}))

    def test_desligado_nao_chama_servico(self):
        os.environ["LAYA_DESLIGADO"] = "true"
        with mock.patch.object(lib_laya.httpx, "Client") as cls:
            self.assertIsNone(lib_laya.sim_nao("x", "?"))
        cls.assert_not_called()

    def test_disponivel(self):
        c = _cliente()
        c.get.return_value = mock.Mock(status_code=200)
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertTrue(lib_laya.disponivel())
        c.get.side_effect = lib_laya.httpx.ConnectError("x")
        with mock.patch.object(lib_laya.httpx, "Client", return_value=c):
            self.assertFalse(lib_laya.disponivel())


if __name__ == "__main__":
    unittest.main()
