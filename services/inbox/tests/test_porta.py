"""
Testes da porta da GPU (services/_shared/lib_porta.py): o que ela mostra ao Laya, quando espera, e as
travas que valem mesmo com o Laya errando ou fora do ar.

    cd services/inbox
    venv/bin/python -m unittest tests.test_porta -v

Offline: Laya, /health e /api/ps mockados. A medição do checkpoint treinado fica no about.md do
services/laya.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from _shared import lib_laya, lib_porta  # noqa: E402

GEMMA = "gemma4:12b"


class TestPorta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ALFRED_PORTA_DIR": self.tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LAYA_DESLIGADO", None)
        carregado = mock.patch.object(lib_porta, "_modelo_carregado", return_value=True)
        carregado.start()
        self.addCleanup(carregado.stop)
        dormir = mock.patch.object(lib_porta.time, "sleep")
        self.dormir = dormir.start()
        self.addCleanup(dormir.stop)

    def _laya(self, *respostas):
        """lib_laya.decidir falso: uma resposta por pergunta ("open", "wait" ou None = sem resposta)."""
        saidas = [None if r is None else {"porta": {"choice": r}} for r in respostas]
        p = mock.patch.object(lib_laya, "decidir", side_effect=saidas)
        self.addCleanup(p.stop)
        return p.start()

    def _dono_falou_ha(self, segundos: float):
        arq = Path(self.tmp.name) / "dono"
        arq.touch()
        quando = time.time() - segundos
        os.utime(arq, (quando, quando))

    def test_placa_livre_e_dono_longe_entra_sem_perguntar(self):
        laya = self._laya()
        with lib_porta.passagem(GEMMA):
            pass
        laya.assert_not_called()

    def test_pergunta_leva_origem_ocupante_dono_e_modelo(self):
        self._dono_falou_ha(5)
        laya = self._laya("open", "open")
        with lib_porta.origem("live voice conversation with the owner"):
            with lib_porta.passagem(GEMMA):
                with lib_porta.origem("scheduled e-mail reading and classification"):
                    with lib_porta.passagem(GEMMA, 16384):
                        pass
        estado, pergunta = laya.call_args.args
        self.assertEqual(estado, {
            "asking_for_gpu": "scheduled e-mail reading and classification",
            "gpu_running_now": "live voice conversation with the owner",
            "owner": lib_porta.DONO_SEGUNDOS,
            "model": "already loaded",
        })
        self.assertEqual(pergunta, lib_porta.PERGUNTA)
        self.assertEqual(laya.call_args.kwargs["modelo"], "porta")

    def test_sem_origem_pede_como_rotina_sem_nome(self):
        self._dono_falou_ha(5)
        laya = self._laya("open")
        with lib_porta.passagem(GEMMA):
            pass
        self.assertEqual(laya.call_args.args[0]["asking_for_gpu"], lib_porta.ORIGEM_PADRAO)

    def test_wait_espera_e_pergunta_de_novo(self):
        self._dono_falou_ha(5)
        laya = self._laya("wait", "wait", "open")
        with lib_porta.passagem(GEMMA):
            pass
        self.assertEqual(laya.call_count, 3)
        self.assertEqual(self.dormir.call_count, 2)

    def test_teto_de_espera_abre_com_o_laya_dizendo_wait(self):
        self._dono_falou_ha(5)
        mock.patch.object(lib_laya, "decidir", return_value={"porta": {"choice": "wait"}}).start()
        self.addCleanup(mock.patch.stopall)
        relogio = iter(range(0, 10_000, 100))   # cada olhada no relógio anda 100 s
        with mock.patch.object(lib_porta.time, "monotonic", side_effect=lambda: next(relogio)):
            with lib_porta.passagem(GEMMA):
                entrou = True
        self.assertTrue(entrou)
        self.assertLessEqual(self.dormir.call_count, lib_porta.TETO_S // 100 + 1)

    def test_laya_sem_resposta_abre(self):
        self._dono_falou_ha(5)
        self._laya(None)
        with lib_porta.passagem(GEMMA):
            pass
        self.dormir.assert_not_called()

    def test_erro_na_porta_nao_derruba_a_chamada(self):
        with mock.patch.object(lib_porta, "_esperar", side_effect=RuntimeError("quebrou")):
            with lib_porta.passagem(GEMMA):
                entrou = True
        self.assertTrue(entrou)

    def test_marca_rodando_so_enquanto_dura(self):
        with lib_porta.origem("deep research job with a large model"):
            with lib_porta.passagem(GEMMA):
                self.assertEqual(lib_porta._rodando(), "deep research job with a large model")
        self.assertEqual(lib_porta._rodando(), lib_porta.PLACA_LIVRE)

    def test_marca_de_processo_morto_e_apagada(self):
        pasta = Path(self.tmp.name) / "rodando"
        pasta.mkdir()
        sobra = pasta / "999999999-abcd.json"
        sobra.write_text('{"origem": "x", "inicio": 1}')
        self.assertEqual(lib_porta._rodando(), lib_porta.PLACA_LIVRE)
        self.assertFalse(sobra.exists())

    def test_dono_por_tempo(self):
        self.assertEqual(lib_porta._dono(), lib_porta.DONO_LONGE)
        for ha, esperado in ((5, lib_porta.DONO_SEGUNDOS), (300, lib_porta.DONO_MINUTOS), (3600, lib_porta.DONO_LONGE)):
            self._dono_falou_ha(ha)
            self.assertEqual(lib_porta._dono(), esperado)

    def test_origem_do_dono_marca_que_ele_falou(self):
        @lib_porta.origem("chat turn typed by the owner in the panel", dono=True)
        def turno():
            return lib_porta._ORIGEM.get()

        self.assertEqual(turno(), ("chat turn typed by the owner in the panel", True))
        self.assertEqual(lib_porta._dono(), lib_porta.DONO_SEGUNDOS)
        self.assertEqual(lib_porta._ORIGEM.get(), (lib_porta.ORIGEM_PADRAO, False))


class TestSinais(unittest.TestCase):
    """O que a porta lê do Ollama e do laya-serve."""

    def _get(self, corpo=None, erro=None):
        resp = mock.Mock()
        resp.json.return_value = corpo
        return mock.patch("httpx.get", side_effect=erro, return_value=resp)

    def test_modelo_carregado_confere_nome_e_num_ctx(self):
        ps = {"models": [{"name": "gemma4:12b", "context_length": 16384}, {"name": "bge-m3:latest"}]}
        with self._get(ps):
            self.assertTrue(lib_porta._modelo_carregado("gemma4:12b", 16384))
            self.assertTrue(lib_porta._modelo_carregado("gemma4:12b", None))
            self.assertTrue(lib_porta._modelo_carregado("bge-m3", None))
            self.assertFalse(lib_porta._modelo_carregado("gemma4:12b", 8192))
            self.assertFalse(lib_porta._modelo_carregado("qwen3.6:35b-a3b", None))

    def test_ollama_sem_resposta_conta_como_carregado(self):
        with self._get(erro=OSError("fora do ar")):
            self.assertTrue(lib_porta._modelo_carregado("gemma4:12b", 16384))

    def test_resposta_de_outro_checkpoint_e_descartada(self):
        """Sem o "porta" registrado o laya-serve responde com o modelo genérico, que erra esta pergunta."""
        for respondeu, esperado in (("porta", "wait"), ("multilingual", None), (None, None)):
            resp = mock.Mock()
            resp.json.return_value = {"answers": {"porta": {"choice": "wait"}}, "routing": {"model": respondeu}}
            cliente = mock.MagicMock()
            cliente.__enter__.return_value = cliente
            cliente.post.return_value = resp
            with mock.patch.object(lib_laya.httpx, "Client", return_value=cliente), \
                    mock.patch.dict(os.environ, {"LAYA_DESLIGADO": ""}):
                self.assertEqual(lib_porta.decidir("x", lib_porta.PLACA_LIVRE, lib_porta.DONO_SEGUNDOS, True), esperado)
            self.assertEqual(cliente.post.call_args.kwargs["json"]["model"], "porta")


if __name__ == "__main__":
    unittest.main()
