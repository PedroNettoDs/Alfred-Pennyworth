"""
Testes da aba Conversa do Painel (painel_conversa.py + conversa.html / conversa.css / conversa.js): navegação e
seletor de modo, primeira mensagem criando a conversa, SSE com texto, ferramenta e cartão de aprovação (executar e
negar pelas rotas), parar, uma pergunta por vez, página recarregada com turno em andamento, busca, apagar e o
markdown seguro com cerca de código e lista numerada.

    cd services/inbox
    venv/bin/python -m unittest tests.test_painel_conversa -v

Offline: vault temporário com conversas.db de verdade; o Ollama é um roteiro (conversa._stream_ollama trocado por um
gerador); memória, RAG e resumo mockados; ferramentas temporárias no registro do lib_ferramentas.
"""

import copy
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TOKEN = "token-de-teste"
BASE = "http://127.0.0.1:7073"
TMP = Path(tempfile.mkdtemp(prefix="alfred-painel-conversa-test-"))
os.environ.update({"SHELL_EXECUTOR_TOKEN": TOKEN, "VAULT_ALFRED": str(TMP / "vault")})

import conversa          # noqa: E402
import core              # noqa: E402
import main              # noqa: E402
import painel            # noqa: E402
import painel_conversa   # noqa: E402
from _shared import lib_ferramentas as lf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ══════════════════════════════════════════════════════════════
#  Ollama de mentira (mesmo formato do roteiro de test_conversa)
# ══════════════════════════════════════════════════════════════

def texto(*partes, entrada=100, saida=10) -> list[dict]:
    return ([{"message": {"role": "assistant", "content": p}, "done": False} for p in partes]
            + [{"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": entrada,
                "eval_count": saida}])


def chamada(nome: str, args=None) -> list[dict]:
    return [{"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"id": "call_1", "function": {"index": 0, "name": nome,
                                                                      "arguments": args or {}}}]},
             "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 100, "eval_count": 5}]


def lento(n: int = 40, passo: float = 0.05):
    """Resposta que chega devagar (≈ n × passo segundos): dá tempo de parar, repetir a pergunta ou recarregar."""
    def gerar():
        for i in range(n):
            time.sleep(passo)
            yield {"message": {"role": "assistant", "content": f"p{i} "}, "done": False}
        yield {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 10, "eval_count": n}
    return gerar


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-painel-conversa-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.roteiro: list = []
        self.payloads: list[dict] = []

        def ollama(payload):
            self.payloads.append(copy.deepcopy(payload))
            passo = self.roteiro.pop(0) if self.roteiro else texto("(fim do roteiro)")
            yield from passo() if callable(passo) else passo

        env = {"VAULT_ALFRED": str(self.vault), "VAULT_PEDRO": "", "VAULT_NOTICIAS": "", "EVOLUTION_URL": "",
               "ALFRED_DB": str(self.tmp / "alfred.db"), "MEMORIA_DB": str(self.tmp / "memoria.db"),
               "OLLAMA_BASE_URL": "http://127.0.0.1:9", "SHELL_EXECUTOR_TOKEN": TOKEN}
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(conversa, "_stream_ollama", ollama),
                  mock.patch.object(conversa, "_cfg", lambda: {"modelo": "m", "modelo_ferramentas": "", "num_ctx": 16384}),
                  mock.patch.object(conversa.lib_memoria, "contexto", lambda *a, **k: ""),
                  mock.patch.object(conversa.lib_rag, "contexto", lambda *a, **k: ""),
                  mock.patch.object(conversa, "ollama_generate", lambda *a, **k: ""),
                  mock.patch.object(core, "_notify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        conversa._WA_CACHE.clear()
        self.addCleanup(self.esperar_livre)
        self.cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))

    def esperar_livre(self, timeout: float = 10):
        """O worker é um só para todos os testes: nenhum turno pode sobrar para o próximo."""
        fim = time.monotonic() + timeout
        while conversa.ocupado():
            if time.monotonic() > fim:
                for ap in list(conversa._APROVACOES):
                    conversa.negar(ap)
                self.fail("turno da conversa não terminou")
            time.sleep(0.01)

    def registrar(self, nome: str, funcao, perigosa: bool = False):
        lf.ferramenta(nome, "ferramenta de teste", perigosa=perigosa)(funcao)
        self.addCleanup(lf._REGISTRO.pop, nome, None)

    # ── HTTP ──

    def entrar(self) -> str:
        r = self.cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.csrf = re.search(r'<meta name="csrf-token" content="([0-9a-f]+)">',
                              self.cli.get("/painel/conversa").text).group(1)
        return self.csrf

    def post(self, url: str, corpo=None, csrf=None):
        return self.cli.post(url, json=corpo or {}, headers={"X-CSRF-Token": self.csrf if csrf is None else csrf,
                                                             "Origin": BASE})

    def enviar(self, texto_: str, cid: str = None):
        url = f"/painel/api/conversas/{cid}/mensagens" if cid else "/painel/api/conversas"
        r = self.post(url, {"texto": texto_})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def eventos(self, turno_id: str) -> list[dict]:
        """O SSE inteiro (o TestClient só devolve depois do "fim" / "erro")."""
        r = self.cli.get(f"/painel/api/turnos/{turno_id}/eventos")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        return [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]

    def esperar_aprovacao(self, timeout: float = 5) -> str:
        fim = time.monotonic() + timeout
        while not conversa._APROVACOES:
            self.assertLess(time.monotonic(), fim, "o cartão de aprovação não apareceu")
            time.sleep(0.01)
        return next(iter(conversa._APROVACOES))

    def sql(self, consulta: str, *params) -> list[dict]:
        with closing(sqlite3.connect(self.vault / "conversas.db")) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(consulta, params)]


# ══════════════════════════════════════════════════════════════
#  Tela
# ══════════════════════════════════════════════════════════════

class TestTela(Base):
    def test_sem_sessao_e_fora_de_localhost(self):
        self.assertEqual(self.cli.get("/painel/conversa", follow_redirects=False).status_code, 303)
        self.assertEqual(self.cli.get("/painel/api/turnos/x/eventos").status_code, 401)
        self.assertEqual(self.cli.post("/painel/api/conversas", json={"texto": "oi"}).status_code, 401)
        fora = TestClient(main.app, base_url=BASE, client=("172.17.0.5", 50000))
        self.assertEqual(fora.get("/painel/conversa").status_code, 403)

    def test_aba_na_navegacao_seletor_de_modo_e_seguranca(self):
        self.entrar()
        r = self.cli.get("/painel/conversa")
        html = r.text
        self.assertIn('href="/painel/conversa" aria-current="page"', html)
        # entre Início e Pendências
        self.assertLess(html.index('nav__rotulo">Início<'), html.index('nav__rotulo">Conversa<'))
        self.assertLess(html.index('nav__rotulo">Conversa<'), html.index('nav__rotulo">Pendências<'))
        self.assertIn('class="modo" type="button" aria-pressed="true">Conversa</button>', html)
        self.assertRegex(html, r'<button class="modo" type="button" disabled[^>]*>Code <span class="modo__em-breve">em breve</span>')
        self.assertIn('class="conteudo conteudo--cheio"', html)
        self.assertIn("Nenhuma conversa ainda.", html)
        self.assertIn("Às ordens, senhor.", html)
        self.assertIn('id="dialogo-apagar"', html)
        self.assertRegex(html, r'<form id="apagar" data-acao="" [^>]*hidden>')   # sem conversa aberta, nada a apagar
        # mesma segurança das outras telas: CSP sem inline, dois scripts (painel.js + conversa.js), nada de on*/style=
        self.assertEqual(r.headers["content-security-policy"], painel.CSP)
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertEqual(re.findall(r'<script src="([^"]+)" defer>', html),
                         ["/painel/static/painel.js", "/painel/static/conversa.js"])
        self.assertEqual(html.count("<script"), 2)
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertNotRegex(html, r"\sstyle\s*=|<style")
        self.assertNotIn(TOKEN, html)

    def test_estaticos(self):
        self.entrar()
        for nome, tipo in (("conversa.css", "text/css"), ("conversa.js", "text/javascript")):
            r = self.cli.get(f"/painel/static/{nome}")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith(tipo))
        self.assertIn("EventSource", self.cli.get("/painel/static/conversa.js").text)

    def test_conversa_inexistente(self):
        self.entrar()
        r = self.cli.get("/painel/conversa/nao-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Não encontrei essa conversa, senhor.", r.text)
        self.assertRegex(r.text, r'<textarea[^>]*\sdisabled>')


# ══════════════════════════════════════════════════════════════
#  Fluxo: mensagem → SSE → página
# ══════════════════════════════════════════════════════════════

class TestFluxo(Base):
    def test_primeira_mensagem_cria_a_conversa_e_responde_em_stream(self):
        self.entrar()
        self.roteiro = [texto("Boa ", "noite, **senhor**.")]
        d = self.enviar("oi Alfred, tudo bem?")
        cid = d["conversa"]
        self.assertTrue(d["nova"])
        self.assertEqual(d["url"], f"/painel/conversa/{cid}")
        self.assertTrue(d["titulo"])
        evs = self.eventos(d["turno"])
        self.assertEqual([e["tipo"] for e in evs], ["texto", "texto", "fim"])
        self.assertEqual("".join(e["texto"] for e in evs[:2]), "Boa noite, **senhor**.")
        self.assertEqual(evs[-1]["texto"], "Boa noite, **senhor**.")
        self.assertIn("<strong>senhor</strong>", evs[-1]["html"])
        self.assertFalse(evs[-1]["parado"])

        html = self.cli.get(d["url"]).text
        self.assertIn('<p class="pergunta__texto">oi Alfred, tudo bem?</p>', html)
        self.assertIn("Boa noite, <strong>senhor</strong>.", html)
        self.assertIn(f'<li class="conversas__item" aria-current="page">\n        <a class="conversas__link" href="{d["url"]}">', html)
        self.assertIn(f"<title>{d['titulo']} · Alfred</title>", html)
        self.assertIn(f'data-acao="/painel/api/conversas/{cid}/apagar"', html)
        self.assertNotIn("Às ordens, senhor.", html)
        self.assertIn(d["url"], self.cli.get("/painel/conversa").text)   # na lista, sem conversa aberta
        self.assertEqual([m["papel"] for m in conversa.mensagens(cid)], ["user", "assistant"])

        # a segunda mensagem continua a mesma conversa, com o histórico no contexto
        self.roteiro = [texto("Pois não.")]
        d2 = self.enviar("e as tarefas?", cid)
        self.assertEqual((d2["conversa"], d2["nova"]), (cid, False))
        self.assertEqual(self.eventos(d2["turno"])[-1]["texto"], "Pois não.")
        msgs = self.payloads[-1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[-1]["content"], "e as tarefas?")
        self.assertEqual(self.payloads[-1]["model"], "m")

    def test_validacoes(self):
        self.entrar()
        self.assertEqual(self.post("/painel/api/conversas", {"texto": "oi"}, csrf="").status_code, 403)
        r = self.post("/painel/api/conversas", {"texto": "   "})
        self.assertEqual((r.status_code, r.json()["campo"]), (400, "texto"))
        self.assertEqual(self.post("/painel/api/conversas", {"texto": "x" * (painel_conversa.TEXTO_MAX + 1)}).status_code, 400)
        self.assertEqual(self.post("/painel/api/conversas/nao-existe/mensagens", {"texto": "oi"}).status_code, 404)
        self.assertEqual(self.cli.get("/painel/api/turnos/nao-existe/eventos").status_code, 404)
        self.assertEqual(self.post("/painel/api/aprovacoes/x/talvez").status_code, 400)
        self.assertEqual(self.post("/painel/api/aprovacoes/x/executar").status_code, 409)
        self.assertEqual(self.post("/painel/api/conversas/nao-existe/apagar").status_code, 404)
        self.assertEqual(self.post("/painel/api/turnos/nao-existe/parar").json(), {"ok": False})
        self.assertEqual(self.sql("SELECT count(*) AS n FROM conversas")[0]["n"], 0)   # nada criado nas recusas

    def test_ferramenta_segura_vira_chip(self):
        self.entrar()
        def eco(texto: str) -> str:
            return f"eco: {texto}"
        self.registrar("_t_eco", eco)
        self.roteiro = [chamada("_t_eco", {"texto": "oi"}), texto("Feito, senhor.")]
        d = self.enviar("ecoe oi")
        evs = self.eventos(d["turno"])
        self.assertEqual([(e["tipo"], e.get("estado")) for e in evs],
                         [("ferramenta", "rodando"), ("ferramenta", "ok"), ("texto", None), ("fim", None)])
        self.assertEqual((evs[1]["nome"], evs[1]["argumentos"], evs[1]["resultado"], evs[1]["perigosa"]),
                         ("_t_eco", {"texto": "oi"}, "eco: oi", False))
        html = self.cli.get(d["url"]).text
        self.assertIn('<details class="ferramenta" data-estado="ok">', html)
        self.assertIn('<summary class="ferramenta__titulo">_t_eco · feito</summary>', html)
        self.assertIn("&#34;texto&#34;: &#34;oi&#34;", html)   # argumentos (JSON escapado)
        self.assertIn('<pre class="ferramenta__codigo">eco: oi</pre>', html)
        self.assertIn("Feito, senhor.", html)

    def test_ferramenta_com_erro(self):
        self.entrar()
        def quebra():
            raise RuntimeError("disco cheio")
        self.registrar("_t_quebra", quebra)
        self.roteiro = [chamada("_t_quebra"), texto("Não deu.")]
        d = self.enviar("quebre")
        self.assertEqual(self.eventos(d["turno"])[1]["estado"], "erro")
        html = self.cli.get(d["url"]).text
        self.assertIn('data-estado="erro"', html)
        self.assertIn("_t_quebra · erro", html)
        self.assertIn("[erro] _t_quebra: disco cheio", html)

    def test_aprovacao_executar_e_negar(self):
        self.entrar()
        feitos = []
        def perigo(alvo: str) -> str:
            feitos.append(alvo)
            return f"apagado: {alvo}"
        self.registrar("_t_perigo", perigo, perigosa=True)

        self.roteiro = [chamada("_t_perigo", {"alvo": "logs"}), texto("Como quiser, senhor.")]
        d = self.enviar("apague os logs")
        aid = self.esperar_aprovacao()
        self.assertEqual(self.post(f"/painel/api/aprovacoes/{aid}/executar").json(), {"ok": True})
        evs = self.eventos(d["turno"])
        self.assertEqual([(e["tipo"], e.get("estado")) for e in evs],
                         [("aprovar", None), ("ferramenta", "rodando"), ("ferramenta", "ok"), ("texto", None), ("fim", None)])
        cartao = evs[0]
        self.assertEqual((cartao["id"], cartao["nome"], cartao["argumentos"]), (aid, "_t_perigo", {"alvo": "logs"}))
        self.assertTrue(cartao["expira_em"])
        self.assertTrue(evs[2]["perigosa"])
        self.assertEqual(feitos, ["logs"])
        self.assertEqual(self.post(f"/painel/api/aprovacoes/{aid}/executar").status_code, 409)   # já decidida

        self.roteiro = [chamada("_t_perigo", {"alvo": "tmp"}), texto("Entendido.")]
        d2 = self.enviar("apague o tmp", d["conversa"])
        aid2 = self.esperar_aprovacao()
        self.assertEqual(self.post(f"/painel/api/aprovacoes/{aid2}/negar").json(), {"ok": True})
        evs2 = self.eventos(d2["turno"])
        ferr = next(e for e in evs2 if e["tipo"] == "ferramenta")
        self.assertEqual(ferr["estado"], "negada")
        self.assertTrue(ferr["resultado"].startswith("[não executada] Pedro negou"))
        self.assertEqual(feitos, ["logs"])
        self.assertEqual([a["status"] for a in self.sql("SELECT status FROM aprovacoes ORDER BY criada_em")],
                         ["aprovada", "negada"])

        html = self.cli.get(d["url"]).text
        self.assertIn("_t_perigo · feito", html)
        self.assertIn('data-estado="nao_executada"', html)
        self.assertIn("_t_perigo · não executada", html)

    def test_parar_uma_pergunta_por_vez_e_pagina_com_turno_em_andamento(self):
        self.entrar()
        self.roteiro = [lento()]
        d = self.enviar("conte até cem")
        cid, tid = d["conversa"], d["turno"]
        # recarregou no meio: a página marca o turno vivo, o bloco para o SSE religar e o Enviar desabilitado
        html = self.cli.get(d["url"]).text
        self.assertIn(f'data-turno-ativo="{tid}"', html)
        self.assertIn('<div class="resposta" data-vivo>', html)
        self.assertIn('<div class="resposta__corpo resposta__corpo--stream"></div>', html)
        self.assertRegex(html, r'<button id="enviar"[^>]*\sdisabled>')
        # uma pergunta por vez; apagar também espera
        r = self.post(f"/painel/api/conversas/{cid}/mensagens", {"texto": "outra"})
        self.assertEqual((r.status_code, r.json()["erro"]), (409, "Alfred ainda está respondendo a mensagem anterior."))
        self.assertEqual(self.post(f"/painel/api/conversas/{cid}/apagar").status_code, 409)
        # parar (depois de chegar algum texto): o fim vem com parado=true e o que já tinha chegado fica gravado
        fim = time.monotonic() + 5
        while conversa.obter_turno(tid).fila.qsize() < 2:
            self.assertLess(time.monotonic(), fim, "nenhum pedaço de texto chegou")
            time.sleep(0.01)
        self.assertEqual(self.post(f"/painel/api/turnos/{tid}/parar").json(), {"ok": True})
        evs = self.eventos(tid)
        self.assertEqual(evs[-1]["tipo"], "fim")
        self.assertTrue(evs[-1]["parado"])
        self.assertEqual(self.post(f"/painel/api/turnos/{tid}/parar").json(), {"ok": False})
        msgs = conversa.mensagens(cid)
        self.assertEqual([m["papel"] for m in msgs], ["user", "assistant"])
        self.assertTrue(msgs[1]["texto"].startswith("p0"))
        html = self.cli.get(d["url"]).text
        self.assertNotIn("data-turno-ativo=\"" + tid, html)
        self.assertRegex(html, r'<button id="enviar"[^>]*type="submit">Enviar</button>')

    def test_sse_depois_do_fim_manda_recarregar(self):
        self.entrar()
        self.roteiro = [texto("Pronto.")]
        d = self.enviar("oi")
        self.assertEqual(self.eventos(d["turno"])[-1]["tipo"], "fim")
        self.assertEqual(self.eventos(d["turno"]), [{"tipo": "fim", "turno": d["turno"], "recarregar": True}])

    def test_erro_do_ollama(self):
        self.entrar()
        def fora():
            raise RuntimeError("Ollama fora do ar (http://127.0.0.1:9)")
            yield  # noqa: gerador que falha ao começar
        self.roteiro = [fora]
        d = self.enviar("oi")
        evs = self.eventos(d["turno"])
        self.assertEqual((evs[-1]["tipo"], evs[-1]["erro"]), ("erro", "Ollama fora do ar (http://127.0.0.1:9)"))
        html = self.cli.get(d["url"]).text
        self.assertIn("Sem resposta gravada", html)


# ══════════════════════════════════════════════════════════════
#  Lista: busca e apagar
# ══════════════════════════════════════════════════════════════

class TestLista(Base):
    def test_busca_filtra_pela_fts(self):
        self.entrar()
        self.roteiro = [texto("DHT é a tabela distribuída."), texto("Bata as claras em neve.")]
        a = self.enviar("Como o BitTorrent acha os pares?")
        self.eventos(a["turno"])
        b = self.enviar("Receita de bolo de laranja")
        self.eventos(b["turno"])
        html = self.cli.get("/painel/conversa", params={"q": "bittorrent"}).text
        self.assertIn(a["url"], html)
        self.assertNotIn(b["url"], html)
        self.assertIn('value="bittorrent"', html)
        self.assertIn("conversas__trecho", html)
        html = self.cli.get("/painel/conversa", params={"q": "neve"}).text   # acha pela resposta do Alfred também
        self.assertIn(b["url"], html)
        self.assertNotIn(a["url"], html)
        self.assertIn("Nenhuma conversa com essas palavras.", self.cli.get("/painel/conversa", params={"q": "zzzz"}).text)
        html = self.cli.get("/painel/conversa").text
        self.assertLess(html.index(b["url"]), html.index(a["url"]))   # sem busca: a mais recente primeiro

    def test_apagar(self):
        self.entrar()
        def perigo() -> str:
            return "ok"
        self.registrar("_t_perigo2", perigo, perigosa=True)
        self.roteiro = [chamada("_t_perigo2"), texto("Feito.")]
        d = self.enviar("faça")
        conversa.aprovar(self.esperar_aprovacao())
        self.eventos(d["turno"])
        self.assertEqual(self.sql("SELECT count(*) AS n FROM aprovacoes")[0]["n"], 1)
        r = self.post(f"/painel/api/conversas/{d['conversa']}/apagar")
        self.assertEqual((r.status_code, r.json()["mensagem"]), (200, "Conversa apagada."))
        self.assertEqual(self.cli.get(d["url"]).status_code, 404)
        for tabela in ("conversas", "mensagens", "aprovacoes"):
            self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {tabela}")[0]["n"], 0, tabela)
        self.assertEqual(conversa.buscar("faça"), [])
        self.assertEqual(self.post(f"/painel/api/conversas/{d['conversa']}/apagar").status_code, 404)


# ══════════════════════════════════════════════════════════════
#  Markdown seguro: o que a conversa acrescentou
# ══════════════════════════════════════════════════════════════

class TestMarkdown(unittest.TestCase):
    def test_cerca_de_codigo_e_lista_numerada(self):
        html = str(painel._markdown_seguro(
            "Rode:\n```bash\ndf -h <dir>\n\n  echo **x**\n```\n1. um\n2) dois\n- a\n\n2026. não é lista"))
        self.assertIn("<pre><code>df -h &lt;dir&gt;\n\n  echo **x**</code></pre>", html)   # literal, escapado
        self.assertIn("<ol><li>um</li><li>dois</li></ol>", html)
        self.assertIn("<ul><li>a</li></ul>", html)
        self.assertIn("<p>2026. não é lista</p>", html)
        self.assertNotIn("```", html)
        # cerca sem fechar: o resto sai como código mesmo assim
        self.assertEqual(str(painel._markdown_seguro("```\nx = 1")), "<pre><code>x = 1</code></pre>")


if __name__ == "__main__":
    unittest.main()
