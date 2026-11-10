"""
Testes da voz do Painel (painel_voz.py + voz.js): o microfone (POST /painel/api/voz), a fala do Alfred
(POST /painel/api/voz/falar + o .mp3) e os botões nas telas de conversa.

    cd services/inbox
    venv/bin/python -m unittest tests.test_voz -v

Offline: a transcrição (lib_audio.transcrever_audio) e a fala (lib_tts.tts_save) são simuladas; nada vai ao
Ollama nem ao edge-tts.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

from tests.test_painel import BASE, Base as BasePainel  # noqa: E402  (ambiente de teste do Painel + helpers)
import conversa  # noqa: E402
import painel_voz  # noqa: E402
from _shared import lib_audio, lib_tts  # noqa: E402

URL = "/painel/api/voz"
WEBM = "audio/webm;codecs=opus"


def _resultado(texto="", erro=None, cortado=False, ocupado=False) -> dict:
    return {"texto": texto, "metodo": "ollama" if texto else "", "duracao_s": 3.0, "segmentos": 1,
            "cortado": cortado, "erro": erro, "ocupado": ocupado}


class TestVoz(BasePainel):
    def falar(self, cli, audio=b"OpusHead-fake", csrf=None, origem=BASE, **headers):
        h = {"Content-Type": WEBM, **headers}
        if csrf is not False:
            h["X-CSRF-Token"] = csrf if csrf is not None else self.csrf(cli)
        if origem:
            h["Origin"] = origem
        return self.guardar(cli.post(URL, content=audio, headers=h))

    def test_transcreve_e_devolve_o_texto(self):
        cli = self.entrar()
        with mock.patch.object(lib_audio, "transcrever_audio", return_value=_resultado("quais pendências tenho?")) as t:
            r = self.falar(cli)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "texto": "quais pendências tenho?", "cortado": False}))
        t.assert_called_once_with(b"OpusHead-fake", WEBM, painel_voz.MAX_SEGUNDOS,
                                  tokens_por_segundo=painel_voz.TOKENS_POR_SEGUNDO,
                                  num_ctx=conversa._cfg()["num_ctx"],            # o mesmo do chat
                                  silencio_final=painel_voz.SILENCIO_FINAL,
                                  motor="whisper", dica=painel_voz.TERMOS)       # Whisper primeiro, com os nomes

    def test_exige_sessao_csrf_e_origem(self):
        cli = self.entrar()
        token = self.csrf(cli)
        with mock.patch.object(lib_audio, "transcrever_audio", return_value=_resultado("oi")) as t:
            self.assertEqual(self.falar(cli, csrf=False).status_code, 403)
            self.assertEqual(self.falar(cli, csrf=token, origem="http://evil.example").status_code, 403)
            self.assertEqual(self.falar(self.cliente(), csrf=token).status_code, 401)
            self.assertEqual(self.falar(self.cliente(ip="172.17.0.3"), csrf=token).status_code, 403)
            t.assert_not_called()

    def test_vazio_grande_demais_e_falhas(self):
        cli = self.entrar()
        token = self.csrf(cli)
        with mock.patch.object(lib_audio, "transcrever_audio") as t:
            self.assertEqual(self.falar(cli, audio=b"", csrf=token).status_code, 400)
            with mock.patch.object(painel_voz, "MAX_BYTES", 8):
                self.assertEqual(self.falar(cli, audio=b"123456789", csrf=token).status_code, 413)
            t.assert_not_called()

            t.return_value = _resultado()                                  # silêncio: nada transcrito, sem erro
            r = self.falar(cli, csrf=token)
            self.assertEqual(r.status_code, 422); self.assertIn("Não entendi", r.json()["erro"])
            t.return_value = _resultado(erro="nenhum transcritor respondeu")
            r = self.falar(cli, csrf=token)
            self.assertEqual(r.status_code, 503); self.assertIn("Ollama", r.json()["erro"])
            t.return_value = _resultado(erro="o Ollama não respondeu em 30 s", ocupado=True)   # placa com outro modelo
            r = self.falar(cli, csrf=token)
            self.assertEqual(r.status_code, 503)
            self.assertIn("ocupada com outra tarefa", r.json()["erro"]); self.assertIn("30 s", r.json()["erro"])

    def test_botao_e_script_nas_telas_de_conversa(self):
        cli = self.entrar()
        for url in ("/painel/conversa", "/painel/escrita"):
            html = self.guardar(cli.get(url)).text
            self.assertIn('<button id="microfone"', html, url)
            self.assertIn('<button id="audio-alfred"', html, url)
            self.assertIn('<script src="/painel/static/voz.js" defer></script>', html, url)
        js = self.guardar(cli.get("/painel/static/voz.js"))
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertIn('"/painel/api/voz"', js.text)
        self.assertIn('"/painel/api/voz/falar"', js.text)
        self.assertNotIn("innerHTML", js.text)                              # texto transcrito nunca vira HTML
        self.assertIn("requestSubmit", js.text)                             # falou, enviou
        for nome in ("conversa.js", "escrita.js"):                          # quem avisa o voz.js de cada pedaço e do fim
            fonte = self.guardar(cli.get("/painel/static/" + nome)).text
            self.assertIn('new CustomEvent("alfred:texto"', fonte, nome)
            self.assertIn('new CustomEvent("alfred:resposta"', fonte, nome)


class TestWhisper(unittest.TestCase):
    """lib_audio com motor="whisper": o faster-whisper vai primeiro e o gemma4 só entra se ele não existir."""

    def rodar(self, whisper, **kw):
        with mock.patch.object(lib_audio, "para_wav", return_value=True), \
                mock.patch.object(lib_audio, "_segmentar", lambda wav, pasta: [wav]), \
                mock.patch.object(lib_audio, "duracao_segundos", return_value=4.0), \
                mock.patch.object(lib_audio, "_transcrever_whisper", return_value=whisper) as w, \
                mock.patch.object(lib_audio, "_transcrever_ollama", return_value="do gemma") as ollama:
            return lib_audio.transcrever_audio(b"audio", "audio/webm", 120, **kw), w, ollama

    def test_whisper_responde_e_o_ollama_nem_e_chamado(self):
        r, w, ollama = self.rodar("Alfred, quais pendências?", motor="whisper", dica="Alfred")
        self.assertEqual((r["texto"], r["metodo"], r["erro"]), ("Alfred, quais pendências?", "whisper", None))
        self.assertEqual(w.call_args.args[1], "Alfred")
        ollama.assert_not_called()

    def test_sem_fala_nao_cai_na_fila_do_ollama(self):
        r, _w, ollama = self.rodar("", motor="whisper")
        self.assertEqual((r["texto"], r["metodo"], r["erro"], r["ocupado"]), ("", "whisper", None, False))
        ollama.assert_not_called()

    def test_sem_o_pacote_o_gemma4_assume(self):
        r, w, ollama = self.rodar(None, motor="whisper")
        self.assertEqual((r["texto"], r["metodo"]), ("do gemma", "ollama"))
        self.assertEqual(w.call_count, 1)                                           # não tenta o Whisper de novo no fim

    def test_o_padrao_continua_sendo_o_gemma4(self):
        r, w, _ollama = self.rodar("do whisper")                                    # WhatsApp: nada muda
        self.assertEqual(r["metodo"], "ollama")
        w.assert_not_called()

    def test_le_o_wav_direto_e_passa_a_dica(self):
        import wave
        import numpy as np
        pasta = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        with wave.open(str(pasta / "a.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(np.array([0, 16384, -16384], np.int16).tobytes())
        modelo = mock.Mock()
        modelo.transcribe.return_value = ([mock.Mock(text=" Alfred, "), mock.Mock(text="bom dia. ")], None)
        with mock.patch.object(lib_audio, "_modelo_whisper", return_value=modelo):
            self.assertEqual(lib_audio._transcrever_whisper(pasta / "a.wav", "Alfred"), "Alfred, bom dia.")
        audio = modelo.transcribe.call_args.args[0]
        self.assertEqual([round(float(x), 2) for x in audio], [0.0, 0.5, -0.5])
        self.assertEqual(modelo.transcribe.call_args.kwargs["initial_prompt"], "Alfred")
        self.assertEqual(modelo.transcribe.call_args.kwargs["language"], "pt")
        with mock.patch.object(lib_audio, "_modelo_whisper", return_value=None):    # pacote não instalado
            self.assertIsNone(lib_audio._transcrever_whisper(pasta / "a.wav"))
        modelo.transcribe.side_effect = RuntimeError("quebrou")
        with mock.patch.object(lib_audio, "_modelo_whisper", return_value=modelo):
            self.assertIsNone(lib_audio._transcrever_whisper(pasta / "a.wav"))


class TestTranscricaoRapida(unittest.TestCase):
    """lib_audio com tokens_por_segundo: teto proporcional ao áudio e uma segunda tentativa quando volta vazio."""

    def rodar(self, respostas, **kw):
        with mock.patch.object(lib_audio, "para_wav", return_value=True), \
                mock.patch.object(lib_audio, "_segmentar", lambda wav, pasta: [wav]), \
                mock.patch.object(lib_audio, "duracao_segundos", return_value=7.5), \
                mock.patch.object(lib_audio, "_transcrever_whisper", return_value=""), \
                mock.patch.object(lib_audio, "_transcrever_ollama", side_effect=respostas) as ollama:
            return lib_audio.transcrever_audio(b"audio", "audio/webm", 120, **kw), ollama

    def test_vazio_e_tentado_de_novo_com_teto_curto(self):
        r, ollama = self.rodar(["", "quais pendências tenho?"], tokens_por_segundo=12, num_ctx=16384)
        self.assertEqual((r["texto"], r["metodo"]), ("quais pendências tenho?", "ollama"))
        self.assertEqual([c.args[2:] for c in ollama.call_args_list],
                         [(130, 16384, lib_audio.ESPERA_INTERATIVA_S)] * 2)            # 40 + 12 × 7,5 s; espera curta

    def test_silencio_no_fim_vai_para_o_ffmpeg(self):
        chamadas = []
        with mock.patch.object(lib_audio, "ffmpeg_disponivel", return_value=True), \
                mock.patch.object(lib_audio.subprocess, "run", lambda cmd, **kw: chamadas.append(cmd) or mock.Mock(returncode=1, stderr="")):
            lib_audio.para_wav(Path("a.webm"), Path("a.wav"), silencio_final=0.8)
            lib_audio.para_wav(Path("a.webm"), Path("a.wav"))
        self.assertIn("apad=pad_dur=0.8", chamadas[0])
        self.assertNotIn("-af", chamadas[1])                                        # o WhatsApp continua como era

    def test_sem_o_parametro_fica_como_sempre(self):
        r, ollama = self.rodar([""])
        self.assertEqual(r["texto"], "")
        self.assertEqual(len(ollama.call_args_list), 1)                            # sem segunda tentativa
        self.assertEqual(ollama.call_args.args[1:], (None, 700, None, lib_audio.ESPERA_S))   # tudo como sempre
        self.assertFalse(r["ocupado"])

    def test_ollama_ocupado_desiste_no_prazo_sem_segunda_tentativa(self):
        relogio = iter([100.0, 100.0 + lib_audio.ESPERA_INTERATIVA_S])            # a chamada levou o prazo inteiro
        with mock.patch.object(lib_audio.time, "monotonic", lambda: next(relogio)):
            r, ollama = self.rodar([""], tokens_por_segundo=12)
        self.assertEqual((r["texto"], r["ocupado"]), ("", True))
        self.assertIn("não respondeu", r["erro"])
        self.assertEqual(len(ollama.call_args_list), 1)


def _fluxo_falso(pedacos, quebra=False):
    """lib_tts.fluxo_tts de mentira: entrega os pedaços e, se pedido, quebra no meio."""
    chamadas = []

    async def fluxo(texto, voz):
        chamadas.append((texto, voz))
        for p in pedacos:
            yield p
        if quebra:
            raise RuntimeError("edge-tts caiu")

    fluxo.chamadas = chamadas
    return fluxo


class TestFalar(BasePainel):
    def setUp(self):
        super().setUp()
        painel_voz._FALAS.clear()

    def test_registra_a_frase_na_hora_e_o_audio_sai_em_fluxo(self):
        cli = self.entrar()
        fluxo = _fluxo_falso([b"ID3", b"-um", b"-dois"])
        with mock.patch.object(lib_tts, "fluxo_tts", fluxo):
            r = self.acao(cli, "/painel/api/voz/falar", {"texto": "## Pendências\n\nHá **duas** [aqui](http://x.y/z)."})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(fluxo.chamadas, [])                              # pedir a fala não espera o áudio
            url = r.json()["url"]
            self.assertRegex(url, r"^/painel/api/voz/fala/[0-9a-f]{16}\.mp3$")
            audio = self.guardar(cli.get(url))
        self.assertEqual((audio.status_code, audio.headers["content-type"]), (200, "audio/mpeg"))
        self.assertEqual(audio.content, b"ID3-um-dois")
        self.assertEqual(fluxo.chamadas, [("Pendências Há duas aqui.", painel_voz.VOZ)])   # markdown fora, voz das pendências
        self.assertEqual(self.guardar(self.cliente().get(url)).status_code, 401)
        for ruim in ("/painel/api/voz/fala/0000000000000000.mp3", "/painel/api/voz/fala/..%2F..%2Fetc%2Fpasswd.mp3"):
            self.assertEqual(self.guardar(cli.get(ruim)).status_code, 404, ruim)

    def test_exige_csrf_e_recusa_o_que_nao_da_para_falar(self):
        cli = self.entrar()
        token = self.csrf(cli)
        self.assertEqual(self.acao(cli, "/painel/api/voz/falar", {"texto": "oi"}, csrf=False).status_code, 403)
        self.assertEqual(self.acao(cli, "/painel/api/voz/falar", {"texto": " ** "}, csrf=token).status_code, 400)
        self.assertEqual(painel_voz._FALAS, {})

    def test_edge_tts_caindo_no_meio_so_encurta_o_audio(self):
        cli = self.entrar()
        with mock.patch.object(lib_tts, "fluxo_tts", _fluxo_falso([b"ID3-comeco"], quebra=True)):
            url = self.acao(cli, "/painel/api/voz/falar", {"texto": "oi"}).json()["url"]
            audio = self.guardar(cli.get(url))
        self.assertEqual((audio.status_code, audio.content), (200, b"ID3-comeco"))

    def test_frase_longa_e_cortada_e_so_as_ultimas_ficam_na_memoria(self):
        longo = "Primeira frase do relatório. " * 80
        dito = painel_voz.texto_para_fala(longo)
        self.assertLessEqual(len(dito), painel_voz.FALA_MAX + len(painel_voz.RESTO))
        self.assertTrue(dito.endswith("relatório." + painel_voz.RESTO), dito[-60:])
        self.assertEqual(painel_voz.texto_para_fala("Curta."), "Curta.")
        cli = self.entrar()
        token = self.csrf(cli)
        with mock.patch.object(painel_voz, "FALAS_GUARDADAS", 3), mock.patch.object(lib_tts, "fluxo_tts", _fluxo_falso([b"ID3"])):
            urls = [self.acao(cli, "/painel/api/voz/falar", {"texto": f"fala {n}"}, csrf=token).json()["url"] for n in range(5)]
            self.assertEqual(list(painel_voz._FALAS.values()), ["fala 2", "fala 3", "fala 4"])
            self.assertEqual(self.guardar(cli.get(urls[0])).status_code, 404)        # saiu da memória
            self.assertEqual(self.guardar(cli.get(urls[-1])).status_code, 200)


if __name__ == "__main__":
    unittest.main()
