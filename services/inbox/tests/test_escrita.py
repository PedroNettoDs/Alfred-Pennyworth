"""
Testes da janela de escrita: ponte com o WordCraft (lib_wordcraft), bloco do documento na conversa e rotas (escrita.py).

    cd services/inbox
    venv/bin/python -m unittest tests.test_escrita -v

Offline: um servidor falso faz o papel da porta de controle do WordCraft; Ollama, memória e RAG mockados.
"""

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TOKEN = "token-de-teste"
BASE = "http://127.0.0.1:7073"
TMP = Path(tempfile.mkdtemp(prefix="alfred-escrita-test-"))
os.environ.update({"SHELL_EXECUTOR_TOKEN": TOKEN, "VAULT_ALFRED": str(TMP / "vault")})

import conversa   # noqa: E402
import core       # noqa: E402
import escrita    # noqa: E402
import main       # noqa: E402
from _shared import lib_ferramentas as lf  # noqa: E402
from _shared import lib_wordcraft as wc    # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CHAVE = "chave-de-teste"
TEXTO = "Primeiro parágrafo do TG.\nSegundo parágrafo citando BitTorrent.\nTerceiro citando BitTorrent."


class WordCraftFalso:
    """Porta de controle: uma linha JSON por conexão. Guarda os comandos na ordem em que chegaram."""

    def __init__(self, pasta: Path):
        self.selecao, self.texto, self.rastreando, self.autor = "", TEXTO, False, "Pedro"
        self.comandos: list[tuple] = []
        self.chaves: list[str] = []
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.porta = self.srv.getsockname()[1]
        (pasta / f"control-key.local-{self.porta}").write_text(CHAVE + "\n", encoding="utf-8")
        threading.Thread(target=self._servir, daemon=True).start()

    def _servir(self):
        while True:
            try:
                con, _ = self.srv.accept()
            except OSError:
                return
            with con, con.makefile("r", encoding="utf-8") as f:
                pedido = json.loads(f.readline())
                self.chaves.append(pedido.get("key"))
                con.sendall((json.dumps(self._responder(pedido), ensure_ascii=False) + "\n").encode("utf-8"))

    def _responder(self, pedido: dict) -> dict:
        metodo, params = pedido["method"], pedido.get("params") or {}
        if metodo == "ui.inspect":
            return {"ok": True, "result": {"ui": {"author": self.autor}}}
        if metodo == "document.inspect":
            return {"ok": True, "result": {"trackChanges": self.rastreando}}
        comando, args = params["command"], params.get("params") or {}
        self.comandos.append((comando, args))
        if comando == "view.switchWindows":
            resultado = {"documents": [{"id": 1, "active": False, "title": "outro", "path": "/o.docx", "dirty": False},
                                       {"id": 2, "active": True, "title": "TG", "path": "/tg.docx", "dirty": True}]}
        elif comando == "document.selection":
            resultado = {"text": self.selecao}
        elif comando == "document.text":
            resultado = {"text": self.texto}
        else:
            resultado = {}
        return {"ok": True, "result": resultado}

    def fechar(self):
        self.srv.close()

    def nomes(self) -> list[str]:
        leitura = ("view.switchWindows", "document.selection", "document.text")
        return [c for c, _ in self.comandos if c not in leitura]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-escrita-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wc = WordCraftFalso(self.tmp)
        self.addCleanup(self.wc.fechar)
        p = mock.patch.dict(os.environ, {"WORDCRAFT_PROFILE_DIR": str(self.tmp),
                                         "WORDCRAFT_CONTROL_PORT": str(self.wc.porta)})
        p.start()
        self.addCleanup(p.stop)


class TestPonte(Base):
    def test_manda_a_chave_do_arquivo(self):
        wc.estado()
        self.assertEqual(set(self.wc.chaves), {CHAVE})

    def test_fechado_vira_erro_legivel(self):
        (self.tmp / f"control-key.local-{self.wc.porta}").unlink()
        with self.assertRaises(wc.ErroWordCraft):
            wc.estado()

    def test_estado_pega_o_documento_ativo(self):
        self.wc.selecao = "Segundo parágrafo"
        self.assertEqual(wc.estado(), {"titulo": "TG", "caminho": "/tg.docx", "alterado": True,
                                       "selecao": "Segundo parágrafo"})

    def test_partes_cabem_e_nao_perdem_texto(self):
        texto = "\n".join(["a" * 6000, "b" * 6000, "c" * 25000, "d"])
        partes = wc._partes(texto)
        self.assertTrue(all(len(p) <= wc.PARTE_MAX for p in partes))
        self.assertEqual("".join(partes).replace("\n", ""), texto.replace("\n", ""))

    def test_ler_parte_inexistente(self):
        self.assertIn("1 parte", wc.documento_ler(9))

    def test_substituir_usa_a_selecao_e_restaura_o_estado(self):
        self.wc.selecao = "Primeiro  parágrafo\ndo TG."
        wc.documento_substituir("Primeiro parágrafo do TG.", "Texto novo.")
        self.assertEqual(self.wc.nomes(), ["file.setAuthor", "review.trackChanges", "text.insert",
                                           "review.trackChanges", "file.setAuthor"])
        args = [a for c, a in self.wc.comandos if c in ("file.setAuthor", "review.trackChanges")]
        self.assertEqual(args, [{"name": "Alfred"}, {"value": True}, {"value": False}, {"name": "Pedro"}])

    def test_substituir_procura_trecho_unico(self):
        wc.documento_substituir("Primeiro parágrafo", "Novo")
        self.assertIn(("select.text", {"text": "Primeiro parágrafo"}), self.wc.comandos)

    def test_trecho_repetido_ou_ausente_nao_edita_e_restaura(self):
        for trecho in ("BitTorrent", "não existe no texto"):
            self.wc.comandos.clear()
            with self.assertRaises(wc.ErroWordCraft):
                wc.documento_substituir(trecho, "Novo")
            self.assertNotIn("text.insert", self.wc.nomes())
            self.assertEqual(self.wc.nomes()[-1], "file.setAuthor")

    def test_paragrafos_novos(self):
        self.wc.selecao = "Primeiro parágrafo do TG."
        wc.documento_substituir("Primeiro parágrafo do TG.", "um\ndois")
        self.assertIn(("text.insert", {"text": "um\ndois", "paragraphs": True}), self.wc.comandos)

    def test_comentar_nao_insere_texto(self):
        wc.documento_comentar("Primeiro parágrafo", "Falta citar a fonte.")
        self.assertIn(("review.newComment", {"text": "Falta citar a fonte."}), self.wc.comandos)
        self.assertNotIn("text.insert", self.wc.nomes())

    def test_so_substituir_pede_aprovacao(self):
        self.assertTrue(lf.perigosa("documento_substituir"))
        self.assertFalse(lf.perigosa("documento_comentar"))
        self.assertFalse(lf.perigosa("documento_ler"))


def _resposta(payload):
    return [{"message": {"content": "Resposta **ok**."}, "done": True, "prompt_eval_count": 1, "eval_count": 1}]


class BaseConversa(Base):
    def setUp(self):
        super().setUp()
        self.payloads: list[dict] = []

        def ollama(payload):
            self.payloads.append(payload)
            yield from _resposta(payload)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        env = {"VAULT_ALFRED": str(self.vault), "VAULT_PEDRO": "", "VAULT_NOTICIAS": "", "EVOLUTION_URL": "",
               "ALFRED_DB": str(self.tmp / "alfred.db"), "MEMORIA_DB": str(self.tmp / "memoria.db"),
               "SHELL_EXECUTOR_TOKEN": TOKEN}
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(conversa, "_stream_ollama", ollama),
                  mock.patch.object(conversa, "_cfg", lambda: {"modelo": "m", "modelo_ferramentas": "", "num_ctx": 16384}),
                  mock.patch.object(conversa.lib_memoria, "contexto", lambda *a, **k: ""),
                  mock.patch.object(conversa.lib_rag, "contexto", lambda *a, **k: ""),
                  mock.patch.object(escrita.lib_rag, "buscar", lambda *a, **k: []),
                  mock.patch.object(conversa, "ollama_generate", lambda *a, **k: ""),
                  mock.patch.object(core, "_notify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def ate_o_fim(self, turno) -> dict:
        while True:
            ev = turno.fila.get(timeout=10)
            if ev["tipo"] in ("fim", "erro"):
                return ev


class TestConversa(BaseConversa):
    def nomes_ferramentas(self) -> set:
        return {f["function"]["name"] for f in self.payloads[-1]["tools"]}

    def test_sem_documento_as_ferramentas_ficam_de_fora(self):
        self.ate_o_fim(conversa.enviar(conversa.nova_conversa()["id"], "oi"))
        self.assertFalse(self.nomes_ferramentas() & set(wc.FERRAMENTAS))
        self.assertEqual(self.payloads[-1]["options"]["num_ctx"], 16384)

    def test_com_documento_entra_o_bloco_e_as_ferramentas(self):
        self.ate_o_fim(conversa.enviar(conversa.nova_conversa()["id"], "oi", contexto="## BLOCO DO DOC", num_ctx=32768))
        self.assertTrue(set(wc.FERRAMENTAS) <= self.nomes_ferramentas())
        self.assertIn("## BLOCO DO DOC", self.payloads[-1]["messages"][-1]["content"])
        self.assertEqual(self.payloads[-1]["options"]["num_ctx"], 32768)

    def test_bloco_nao_fica_no_historico(self):
        cid = conversa.nova_conversa()["id"]
        self.ate_o_fim(conversa.enviar(cid, "primeira", contexto="## BLOCO DO DOC"))
        self.ate_o_fim(conversa.enviar(cid, "segunda"))
        self.assertEqual(self.payloads[-1]["messages"][-1]["content"], "segunda")
        self.assertNotIn("BLOCO DO DOC", json.dumps(self.payloads[-1]["messages"]))


class TestRotas(BaseConversa):
    def setUp(self):
        super().setUp()
        self.cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))

    def entrar(self) -> str:
        r = self.cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return re.search(r'<meta name="csrf-token" content="([0-9a-f]+)">', self.cli.get("/painel/escrita").text).group(1)

    def enviar(self, csrf: str, **corpo):
        return self.cli.post("/painel/api/escrita/mensagens", json=corpo, headers={"X-CSRF-Token": csrf, "Origin": BASE})

    def test_sem_sessao(self):
        self.assertEqual(self.cli.get("/painel/escrita", follow_redirects=False).status_code, 303)
        self.assertEqual(self.cli.get("/painel/api/escrita/estado").status_code, 401)

    def test_fora_de_localhost(self):
        cli = TestClient(main.app, base_url=BASE, client=("172.17.0.5", 50000))
        self.assertEqual(cli.get("/painel/api/escrita/estado").status_code, 403)

    def test_post_sem_csrf(self):
        self.entrar()
        self.assertEqual(self.enviar("", texto="oi").status_code, 403)

    def test_estado_com_selecao(self):
        self.entrar()
        self.wc.selecao = "x" * 500
        d = self.cli.get("/painel/api/escrita/estado").json()
        self.assertTrue(d["conectado"])
        self.assertEqual(d["caracteres"], 500)
        self.assertLessEqual(len(d["previa"]), escrita.PREVIA_CHARS)

    def test_estado_com_wordcraft_fechado(self):
        self.entrar()
        (self.tmp / f"control-key.local-{self.wc.porta}").unlink()
        self.assertFalse(self.cli.get("/painel/api/escrita/estado").json()["conectado"])

    def test_mensagem_com_selecao_e_stream(self):
        csrf = self.entrar()
        self.wc.selecao = "Segundo parágrafo citando BitTorrent."
        r = self.enviar(csrf, texto="Está certo?", alcance="selecao")
        self.assertEqual(r.status_code, 200, r.text)
        corpo = self.cli.get(f"/painel/api/escrita/turnos/{r.json()['turno']}/eventos").text
        fim = [json.loads(l[6:]) for l in corpo.splitlines() if l.startswith("data: ")][-1]
        self.assertEqual(fim["tipo"], "fim")
        self.assertIn("<strong>ok</strong>", fim["html"])
        pedido = self.payloads[-1]["messages"][-1]["content"]
        self.assertIn("Trecho que Pedro selecionou", pedido)
        self.assertIn("Segundo parágrafo citando BitTorrent.", pedido)
        self.assertIn("NBR 10520", pedido)
        self.assertTrue(pedido.endswith("Está certo?"))

    def test_documento_inteiro_grande_usa_contexto_maior(self):
        csrf = self.entrar()
        self.wc.texto = "palavra " * 2000
        r = self.enviar(csrf, texto="Avalie tudo.", alcance="documento")
        self.cli.get(f"/painel/api/escrita/turnos/{r.json()['turno']}/eventos")
        self.assertEqual(self.payloads[-1]["options"]["num_ctx"], escrita.NUM_CTX_GRANDE)
        self.assertIn("Documento inteiro", self.payloads[-1]["messages"][-1]["content"])

    def test_texto_acima_do_limite(self):
        csrf = self.entrar()
        self.wc.texto = "x" * (escrita.TEXTO_MAX + 1)
        self.assertEqual(self.enviar(csrf, texto="Avalie.", alcance="documento").status_code, 413)

    def test_wordcraft_fechado_na_mensagem(self):
        csrf = self.entrar()
        (self.tmp / f"control-key.local-{self.wc.porta}").unlink()
        self.assertEqual(self.enviar(csrf, texto="oi").status_code, 503)

    def test_decisao_invalida_ou_vencida(self):
        csrf = self.entrar()
        h = {"X-CSRF-Token": csrf, "Origin": BASE}
        self.assertEqual(self.cli.post("/painel/api/escrita/aprovacoes/x/talvez", headers=h).status_code, 400)
        self.assertEqual(self.cli.post("/painel/api/escrita/aprovacoes/x/executar", headers=h).status_code, 409)

    def test_estaticos_registrados(self):
        self.assertEqual(self.cli.get("/painel/static/escrita.js").status_code, 200)
        self.assertEqual(self.cli.get("/painel/static/escrita.css").status_code, 200)


if __name__ == "__main__":
    unittest.main()
