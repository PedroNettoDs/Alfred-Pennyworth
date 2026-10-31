"""
Testes do modo profundo do researcher: plano, filtro de relevância, leitura das páginas e síntese com citações.

    cd services/researcher
    venv/bin/python -m unittest tests.test_profundo -v

Tudo offline: Ollama (ollama_generate / ollama_generate_json / ollama_embed), SearXNG (searxng_search),
DNS (_resolver) e httpx (_transporte_http = httpx.MockTransport) trocados por funções locais; vault e
estado dos jobs em pasta temporária. O modo antigo (síncrono) também é conferido aqui.
"""

import importlib.util
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

AQUI = Path(__file__).parent
RESEARCHER = AQUI.parent

_spec = importlib.util.spec_from_file_location("alfred_researcher_profundo", RESEARCHER / "main.py")
researcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(researcher)

CONFIG = {"modelo_notas": "leve:teste", "modelo_sintese": "grande:teste", "relevancia_minima": 0.5}


# ── Embeddings falsos: saco de palavras sobre um vocabulário fixo ──
VOCAB = ["rust", "async", "tokio", "runtime", "funciona", "alternativ", "smol", "bolo", "receita", "cenoura"]


def embed_falso(texto, *a, **k):
    t = texto.lower()
    return [float(t.count(p)) for p in VOCAB]


# ── DNS falso ─────────────────────────────────────────────────
def resolver_falso(host):
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    if host == "localhost":
        return ["127.0.0.1", "::1"]
    if host == "interno.example":
        return ["10.0.0.5"]
    if host == "misto.example":
        return ["93.184.216.34", "192.168.1.1"]
    if host == "naoexiste.example":
        raise socket.gaierror("nome não resolve")
    return ["93.184.216.34"]


def html(titulo, corpo):
    return (
        f"<html><head><title>{titulo}</title><script>var segredo = 'nao-extrair';</script>"
        f"<style>body {{ color: red }}</style></head><body>"
        f"<nav>Menu Início Contato</nav>"
        f"<article><h1>{titulo}</h1><p>{corpo}</p><p>{'Conteúdo de verdade sobre o assunto. ' * 10}</p></article>"
        f"<footer>Rodapé copyright</footer></body></html>"
    ).encode("utf-8")


class _Transporte:
    """httpx.MockTransport com rotas por URL; guarda o que foi pedido (para provar que IP privado nunca é tocado)."""

    def __init__(self, rotas):
        self.rotas, self.pedidos = rotas, []
        self.mock = httpx.MockTransport(self._responder)

    def _responder(self, request):
        url = str(request.url)
        self.pedidos.append(url)
        rota = self.rotas.get(url)
        if rota is None:
            return httpx.Response(404, text="não achei")
        return rota(request) if callable(rota) else rota


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        researcher._EMBED_CACHE.clear()
        self.patches = [
            mock.patch.object(researcher, "_resolver", resolver_falso),
            mock.patch.object(researcher, "_config_pesquisa", lambda: dict(CONFIG)),
            mock.patch.object(researcher, "ollama_embed", embed_falso),
            mock.patch.dict(os.environ, {"RESEARCH_MODEL_NOTAS": "", "RESEARCH_MODEL_SINTESE": ""}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def usar_transporte(self, rotas) -> _Transporte:
        t = _Transporte(rotas)
        p = mock.patch.object(researcher, "_transporte_http", t.mock)
        p.start()
        self.addCleanup(p.stop)
        return t


# ══════════════════════════════════════════════════════════════
#  1. Plano
# ══════════════════════════════════════════════════════════════

class TestPlano(_Base):
    def test_limpa_duplicatas_e_corta_em_6(self):
        chamadas = []

        def llm(prompt, model=None, timeout=120, schema=None, options=None):
            chamadas.append({"model": model, "schema": schema, "prompt": prompt})
            return {"subperguntas": ["  O que é Tokio? ", "o que é tokio?", "", 42, "Como funciona o executor?",
                                     "Tokio vs async-std", "Limitações do Tokio", "Uso em produção", "Benchmarks",
                                     "História"]}

        with mock.patch.object(researcher, "ollama_generate_json", llm):
            subs = researcher._plano("Tokio", "")
        self.assertEqual(subs, ["O que é Tokio?", "Como funciona o executor?", "Tokio vs async-std",
                                "Limitações do Tokio", "Uso em produção", "Benchmarks"])
        self.assertEqual(chamadas[0]["model"], "leve:teste")          # plano usa o modelo leve
        self.assertIs(chamadas[0]["schema"], researcher.SCHEMA_PLANO)  # saída estruturada (JSON Schema)
        self.assertEqual(researcher.SCHEMA_PLANO["properties"]["subperguntas"]["minItems"], 3)
        self.assertEqual(researcher.SCHEMA_PLANO["properties"]["subperguntas"]["maxItems"], 6)
        self.assertIn('"Tokio"', chamadas[0]["prompt"])

    def test_llm_fora_ou_lixo_pesquisa_o_tema(self):
        for resposta in (None, [], {"subperguntas": "não é lista"}, {"outra": ["x"]}):
            with mock.patch.object(researcher, "ollama_generate_json", lambda *a, r=resposta, **k: r):
                self.assertEqual(researcher._plano("Tema X", ""), ["Tema X"])

    def test_perfil_entra_no_prompt(self):
        prompts = []
        with mock.patch.object(researcher, "ollama_generate_json",
                               lambda p, **k: prompts.append(p) or {"subperguntas": ["a", "b", "c"]}):
            researcher._plano("Tema", "Contexto do usuário: dev de IA local")
        self.assertIn("dev de IA local", prompts[0])

    def test_modelo_por_env_passa_por_cima_do_perfis(self):
        with mock.patch.dict(os.environ, {"RESEARCH_MODEL_NOTAS": "env:leve", "RESEARCH_MODEL_SINTESE": "env:grande"}):
            self.assertEqual(researcher._modelo("notas"), "env:leve")
            self.assertEqual(researcher._modelo("sintese"), "env:grande")
        self.assertEqual(researcher._modelo("notas"), "leve:teste")
        with mock.patch.object(researcher, "_config_pesquisa", lambda: {}):
            self.assertEqual(researcher._modelo("notas"), "gemma4:e4b")
            self.assertIsNone(researcher._modelo("sintese"))             # None → MODEL_CHAT


# ══════════════════════════════════════════════════════════════
#  2. Filtro de relevância
# ══════════════════════════════════════════════════════════════

class TestFiltro(_Base):
    SUB = "O que é o runtime Tokio em Rust async?"

    def test_relevante_e_irrelevante(self):
        self.assertTrue(researcher._relevante(self.SUB, {"title": "Guia do Tokio runtime Rust",
                                                         "snippet": "Tokio é um runtime async para Rust"}))
        self.assertFalse(researcher._relevante(self.SUB, {"title": "Receita de bolo de cenoura",
                                                          "snippet": "bolo fofinho"}))
        self.assertFalse(researcher._relevante(self.SUB, {"title": "", "snippet": ""}))

    def test_limiar_vem_do_perfis(self):
        meio = {"title": "Rust", "snippet": "linguagem de sistemas"}   # cosseno 0.5 com a subpergunta
        with mock.patch.object(researcher, "_config_pesquisa", lambda: {"relevancia_minima": 0.9}):
            self.assertFalse(researcher._relevante(self.SUB, meio))
        with mock.patch.object(researcher, "_config_pesquisa", lambda: {"relevancia_minima": 0.1}):
            self.assertTrue(researcher._relevante(self.SUB, meio))

    def test_sem_embedding_deixa_passar(self):
        with mock.patch.object(researcher, "ollama_embed", lambda *a, **k: []):
            self.assertTrue(researcher._relevante(self.SUB, {"title": "Receita de bolo", "snippet": "x"}))

    def test_embedding_da_subpergunta_vem_do_cache(self):
        textos = []
        with mock.patch.object(researcher, "ollama_embed", lambda t, *a, **k: textos.append(t) or embed_falso(t)):
            for i in range(3):
                researcher._relevante(self.SUB, {"title": f"Tokio {i}", "snippet": "runtime"})
        self.assertEqual(textos.count(self.SUB), 1)
        self.assertEqual(len(textos), 4)

    def test_falha_de_embedding_nao_fica_no_cache(self):
        with mock.patch.object(researcher, "ollama_embed", lambda *a, **k: []):
            self.assertEqual(researcher._embedding("abc"), [])
        self.assertEqual(researcher._embedding("abc rust"), embed_falso("abc rust"))
        self.assertNotIn("abc", researcher._EMBED_CACHE)


# ══════════════════════════════════════════════════════════════
#  3. Leitura das fontes
# ══════════════════════════════════════════════════════════════

class TestLeitura(_Base):
    def test_extrai_texto_sem_script_nem_menu(self):
        self.usar_transporte({"https://tokio.example/guia": httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=html("Guia do Tokio", "Tokio é um runtime assíncrono para Rust."))})
        texto = researcher._baixar_pagina("https://tokio.example/guia")
        self.assertIn("Tokio é um runtime assíncrono para Rust.", texto)
        self.assertNotIn("nao-extrair", texto)
        self.assertNotIn("Menu Início", texto)
        self.assertNotIn("Rodapé", texto)

    def test_pagina_grande_e_truncada_sem_ler_tudo(self):
        enviados = [0]

        def sem_fim():
            yield b"<html><body><article>"
            while True:
                enviados[0] += 1
                if enviados[0] > 10_000:          # ~80 MB: se chegou aqui, o limite não funcionou
                    raise AssertionError("leu a página inteira")
                yield b"<p>" + b"Texto longo sobre Tokio. " * 300 + b"</p>"

        self.usar_transporte({"https://grande.example/": lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=sem_fim())})
        with mock.patch.object(researcher, "PAGINA_MAX_BYTES", 100_000):
            texto = researcher._baixar_pagina("https://grande.example/")
        self.assertTrue(texto)
        self.assertLessEqual(len(texto), researcher.TEXTO_MAX_CHARS)
        self.assertLess(enviados[0] * 7_500, 100_000 + 2 * 7_600)   # parou logo depois do limite

    def test_timeout_do_httpx(self):
        def lento(request):
            raise httpx.ReadTimeout("servidor lento", request=request)

        self.usar_transporte({"https://lento.example/": lento})
        self.assertIsNone(researcher._baixar_pagina("https://lento.example/"))

    def test_servidor_que_pinga_aos_poucos_estoura_o_teto_total(self):
        def gotejando():                          # ~3 s no total: sem o teto, o teste falha em vez de travar
            yield b"<html><body><article>"
            for _ in range(60):
                time.sleep(0.05)
                yield b"<p>um pouco</p>"

        self.usar_transporte({"https://gota.example/": lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=gotejando())})
        inicio = time.monotonic()
        with mock.patch.object(researcher, "PAGINA_TIMEOUT", 0.3):
            self.assertIsNone(researcher._baixar_pagina("https://gota.example/"))
        self.assertLess(time.monotonic() - inicio, 2)

    def test_url_privada_ou_esquema_invalido_nunca_e_pedida(self):
        t = self.usar_transporte({})
        for url in ("http://127.0.0.1:7073/painel", "http://localhost/", "http://192.168.0.10/",
                    "http://[::1]:11434/api/tags", "http://169.254.169.254/latest/meta-data",
                    "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "http://interno.example/",
                    "http://misto.example/", "http://naoexiste.example/", "file:///etc/passwd",
                    "ftp://arquivos.example/x", "javascript:alert(1)", "https:///sem-host"):
            self.assertIsNone(researcher._baixar_pagina(url), url)
        self.assertEqual(t.pedidos, [])

    def test_redirect_para_ip_privado_e_barrado(self):
        t = self.usar_transporte({
            "https://redir.example/": httpx.Response(302, headers={"location": "http://127.0.0.1:7073/painel"}),
            "http://127.0.0.1:7073/painel": httpx.Response(200, headers={"content-type": "text/html"},
                                                           content=html("Painel", "segredo do painel")),
        })
        self.assertIsNone(researcher._baixar_pagina("https://redir.example/"))
        self.assertEqual(t.pedidos, ["https://redir.example/"])

    def test_redirect_publico_relativo_e_seguido(self):
        t = self.usar_transporte({
            "https://a.example/velho": httpx.Response(301, headers={"location": "/novo"}),
            "https://a.example/novo": httpx.Response(200, headers={"content-type": "text/html"},
                                                     content=html("Novo", "Página nova sobre Rust async.")),
        })
        self.assertIn("Página nova sobre Rust async.", researcher._baixar_pagina("https://a.example/velho"))
        self.assertEqual(t.pedidos, ["https://a.example/velho", "https://a.example/novo"])

    def test_redirects_em_loop_desistem(self):
        self.usar_transporte({"https://loop.example/": httpx.Response(302, headers={"location": "/"})})
        self.assertIsNone(researcher._baixar_pagina("https://loop.example/"))

    def test_pdf_erro_http_e_pagina_vazia_sao_ignorados(self):
        self.usar_transporte({
            "https://pdf.example/x.pdf": httpx.Response(200, headers={"content-type": "application/pdf"},
                                                        content=b"%PDF-1.7"),
            "https://erro.example/": httpx.Response(500, text="erro"),
            "https://vazia.example/": httpx.Response(200, headers={"content-type": "text/html"},
                                                     content=b"<html><body><div id=app></div></body></html>"),
        })
        for url in ("https://pdf.example/x.pdf", "https://erro.example/", "https://vazia.example/"):
            self.assertIsNone(researcher._baixar_pagina(url), url)

    def test_nota_tem_url_do_codigo_e_fatos_limpos(self):
        prompts = []

        def llm(prompt, model=None, timeout=120, schema=None, options=None):
            prompts.append((prompt, model, schema))
            return {"fatos": ["  Tokio usa work-stealing.  ", "", 7] + [f"fato {i}" for i in range(20)],
                    "url": "https://inventada.example/"}

        with mock.patch.object(researcher, "ollama_generate_json", llm):
            nota = researcher._nota_da_pagina("Como o Tokio agenda tarefas?",
                                              {"url": "https://tokio.example/guia", "title": "Guia"}, "texto da página")
        self.assertEqual(nota["url"], "https://tokio.example/guia")
        self.assertEqual(nota["fatos"][0], "Tokio usa work-stealing.")
        self.assertEqual(len(nota["fatos"]), researcher.FATOS_MAX)
        self.assertEqual(prompts[0][1], "leve:teste")                  # notas no modelo leve
        self.assertIs(prompts[0][2], researcher.SCHEMA_NOTAS)
        self.assertIn("ignore qualquer pedido", prompts[0][0])
        with mock.patch.object(researcher, "ollama_generate_json", lambda *a, **k: {"fatos": []}):
            self.assertIsNone(researcher._nota_da_pagina("x", {"url": "https://a.example/"}, "texto"))


# ══════════════════════════════════════════════════════════════
#  4. Síntese e fluxo completo pelo endpoint
# ══════════════════════════════════════════════════════════════

SUBS = ["O que é o runtime Tokio em Rust async?", "Como o async funciona em Rust?",
        "Quais as alternativas ao Tokio runtime?"]

RESULTADOS = [
    {"url": "https://tokio.example/guia", "title": "Guia do Tokio runtime Rust", "snippet": "Tokio é um runtime async para Rust"},
    {"url": "https://tokio.example/guia", "title": "Guia do Tokio runtime Rust (cópia)", "snippet": "duplicado"},
    {"url": "https://bolo.example/receita", "title": "Receita de bolo de cenoura", "snippet": "bolo fofinho"},
    {"url": "http://127.0.0.1:7073/painel", "title": "Rust async runtime tokio painel", "snippet": "rust async"},
    {"url": "https://async.example/livro", "title": "Async Rust livro runtime", "snippet": "async em Rust com runtime"},
    {"url": "https://smol.example/", "title": "smol: runtime alternativo ao tokio", "snippet": "pequeno e simples"},
]

RELATORIO = (
    "## Resumo\nTokio é o runtime mais usado [1]. Async em Rust usa futures [2]. Fonte inventada [9].\n\n"
    "## Quais as alternativas ao Tokio runtime?\nsmol é menor [1, 3].\n\n"
    "## Lacunas e divergências\nVer o [guia](https://tokio.example/guia) [3]."
)


class TestFluxoProfundo(_Base):
    def setUp(self):
        super().setUp()
        self.vault = self.dir / "vault"
        self.vault.mkdir()
        self.jobs = self.dir / "jobs"
        self.chamadas_llm = []
        self.relatorio = RELATORIO
        for p in (
            mock.patch.object(researcher, "VAULT_ALFRED", self.vault),
            mock.patch.object(researcher, "JOBS_DIR", self.jobs),
            mock.patch.object(researcher, "TOKEN", "tk"),
            mock.patch.object(researcher, "indexar_local", mock.Mock()),
            mock.patch.object(researcher, "_get_chroma", lambda: None),
            mock.patch.object(researcher, "searxng_search", lambda q, max_results=8: [dict(r) for r in RESULTADOS]),
            mock.patch.object(researcher, "ollama_generate_json", self._llm_json),
            mock.patch.object(researcher, "ollama_generate", self._llm_texto),
        ):
            p.start()
            self.addCleanup(p.stop)
        pagina = lambda titulo, corpo: httpx.Response(200, headers={"content-type": "text/html"},
                                                      content=html(titulo, corpo))
        self.transporte = self.usar_transporte({
            "https://tokio.example/guia": pagina("Guia do Tokio", "Tokio é um runtime assíncrono para Rust."),
            "https://async.example/livro": pagina("Async Rust", "Futures são preguiçosas em Rust."),
            "https://smol.example/": pagina("smol", "smol é um runtime pequeno."),
            "https://bolo.example/receita": pagina("Bolo", "Bata os ovos."),
        })
        from fastapi.testclient import TestClient
        self.cliente = TestClient(researcher.app)
        self.h = {"Authorization": "Bearer tk"}

    def _llm_json(self, prompt, model=None, timeout=120, schema=None, options=None):
        self.chamadas_llm.append(("json", model, schema, options))
        if schema is researcher.SCHEMA_PLANO:
            return {"subperguntas": list(SUBS)}
        url = re.search(r"^Página: .* \((https?://[^)]+)\)$", prompt, re.MULTILINE).group(1)
        return {"fatos": [f"Fato lido em {url}"]}

    def _llm_texto(self, prompt, model=None, timeout=180, options=None, **k):
        self.chamadas_llm.append(("texto", model, prompt, options))
        if prompt.startswith("Gere "):                     # modo antigo: geração de consultas
            return '["rust async"]'
        return self.relatorio

    def _post(self, topic="Tokio runtime em Rust", **kw):
        return self.cliente.post("/research", headers=self.h,
                                 json={"topic": topic, "profundo": True, "paginas_por_subpergunta": 1, **kw})

    def _esperar(self, job_id):
        for _ in range(500):
            dados = self.cliente.get(f"/research/{job_id}", headers=self.h).json()
            if dados["status"] != "running":
                return dados
            time.sleep(0.01)
        self.fail("job profundo não terminou")

    def test_fluxo_completo_com_citacoes(self):
        r = self._post()
        self.assertEqual(r.status_code, 202)
        corpo = r.json()
        self.assertEqual(corpo["status"], "running")
        self.assertEqual(corpo["progresso"], f"/research/{corpo['job_id']}")
        job = self._esperar(corpo["job_id"])
        self.assertEqual(job["status"], "done", job.get("erro"))
        self.assertEqual(job["etapa"], "concluido")
        self.assertEqual(job["subperguntas"], SUBS)

        # uma página por subpergunta, na ordem do SearXNG; bolo (irrelevante) e painel (IP privado) nunca baixados
        self.assertEqual(self.transporte.pedidos,
                         ["https://tokio.example/guia", "https://async.example/livro", "https://smol.example/"])
        self.assertEqual([n["url"] for n in job["notas"]],
                         ["https://tokio.example/guia", "https://async.example/livro", "https://smol.example/"])
        self.assertEqual([n["subpergunta"] for n in job["notas"]], SUBS)
        self.assertEqual(job["contagem"]["lidas"], 3)
        self.assertEqual(job["contagem"]["tentadas"], 4)   # o painel privado foi tentado e recusado

        res = job["resultado"]
        self.assertTrue(res["profundo"])
        self.assertEqual(res["sources_found"], 3)
        self.assertEqual(res["files"], ["index.md", "synthesis.md", "sources.md"])

        sintese = (self.vault / "research" / res["slug"] / "synthesis.md").read_text(encoding="utf-8")
        self.assertIn("modo: profundo", sintese.split("\n---", 1)[0])
        self.assertIn("Tokio é o runtime mais usado [1].", sintese)
        self.assertNotIn("[9]", sintese)                     # citação fora da lista sai
        self.assertIn("smol é menor [1][3].", sintese)       # [1, 3] → [1][3]
        self.assertIn("[guia](https://tokio.example/guia)", sintese)   # link markdown fica
        self.assertIn("## Fontes\n\n- [1] [Guia do Tokio runtime Rust](https://tokio.example/guia)\n"
                      "- [2] [Async Rust livro runtime](https://async.example/livro)\n"
                      "- [3] [smol: runtime alternativo ao tokio](https://smol.example/)", sintese)
        fontes = (self.vault / "research" / res["slug"] / "sources.md").read_text(encoding="utf-8")
        self.assertIn("Fato lido em https://async.example/livro", fontes)
        self.assertIn("**Subpergunta**: Como o async funciona em Rust?", fontes)
        indice = (self.vault / "research" / res["slug"] / "index.md").read_text(encoding="utf-8")
        self.assertIn("- Quais as alternativas ao Tokio runtime?", indice)

        # modelos por etapa: leve no plano e nas notas, grande (com num_ctx maior) só na síntese
        leves = [c for c in self.chamadas_llm if c[0] == "json"]
        self.assertEqual({c[1] for c in leves}, {"leve:teste"})
        self.assertEqual(len(leves), 4)                      # 1 plano + 3 notas
        sinteses = [c for c in self.chamadas_llm if c[0] == "texto"]
        self.assertEqual(len(sinteses), 1)
        self.assertEqual(sinteses[0][1], "grande:teste")
        self.assertEqual(sinteses[0][3], {"num_ctx": researcher.NUM_CTX_SINTESE})
        self.assertIn("[3] smol: runtime alternativo ao tokio — https://smol.example/", sinteses[0][2])
        self.assertIn("- Fato lido em https://smol.example/", sinteses[0][2])

        # estado gravado de forma atômica: só o JSON final, nenhum temporário largado
        self.assertEqual([p.name for p in self.jobs.iterdir()], [f"{corpo['job_id']}.json"])
        self.assertEqual(json.loads((self.jobs / f"{corpo['job_id']}.json").read_text())["status"], "done")

        # pedido profundo de novo: agora é cache exato (síntese profunda), síncrono
        r2 = self._post()
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["cache_type"], "exact")

    def test_sintese_vazia_erro_sem_gravar(self):
        self.relatorio = ""
        job = self._esperar(self._post().json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("síntese vazia", job["erro"])
        self.assertFalse((self.vault / "research").exists())

    def test_nenhuma_pagina_lida_erro_sem_gravar(self):
        self.transporte.rotas.clear()
        job = self._esperar(self._post().json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("Nenhuma página", job["erro"])
        self.assertFalse((self.vault / "research").exists())
        self.assertFalse(any(c[0] == "texto" for c in self.chamadas_llm))   # nem chama o modelo grande

    def test_sem_beautifulsoup_erro_claro(self):
        with mock.patch.object(researcher, "BeautifulSoup", None):
            job = self._esperar(self._post().json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("pip install -r requirements.txt", job["erro"])

    def test_cache_rapido_nao_serve_pedido_profundo(self):
        d = self.vault / "research" / "tokio-runtime-em-rust"
        d.mkdir(parents=True)
        (d / "synthesis.md").write_text("---\ntitle: x\n---\n# Tokio\n\n## Visão Geral\nsó trechos\n", encoding="utf-8")
        r = self._post()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self._esperar(r.json()["job_id"])["status"], "done")
        # o pedido rápido aceita a síntese profunda que ficou no lugar
        rapido = self.cliente.post("/research", headers=self.h, json={"topic": "Tokio runtime em Rust"})
        self.assertEqual(rapido.json()["cache_type"], "exact")
        self.assertIn("## Fontes", rapido.json()["synthesis"])

    def test_cache_semantico_profundo_filtra_por_modo(self):
        d = self.vault / "research" / "outro"
        d.mkdir(parents=True)
        (d / "synthesis.md").write_text("# Outro\n\n## Resumo\nconteúdo\n", encoding="utf-8")
        hoje = time.strftime("%Y-%m-%d")
        docs = [{"id": "outro", "distance": 0.1,
                 "metadata": {"slug": "outro", "topic": "Outro", "date": hoje, "vault_path": str(d)}}]
        with mock.patch.object(researcher, "_get_chroma", lambda: "cid"), \
                mock.patch.object(researcher.chroma, "query_similar", lambda *a, **k: [dict(x) for x in docs]):
            self.assertIsNotNone(researcher.check_semantic_cache("tema"))
            self.assertIsNone(researcher.check_semantic_cache("tema", so_profundo=True))
            docs[0]["metadata"]["modo"] = "profundo"
            self.assertIsNotNone(researcher.check_semantic_cache("tema", so_profundo=True))

    def test_mesmo_tema_em_andamento_devolve_o_mesmo_job(self):
        researcher._jobs["abcdef123456"] = {"id": "abcdef123456", "topic": "Tokio runtime em Rust",
                                            "slug": "tokio-runtime-em-rust", "status": "running"}
        self.addCleanup(researcher._jobs.pop, "abcdef123456", None)
        r = self._post()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["job_id"], "abcdef123456")
        self.assertEqual(self.transporte.pedidos, [])
        self.assertEqual(self.cliente.get("/research/abcdef123456", headers=self.h).json()["status"], "running")

    def test_get_status_auth_404_e_interrompido(self):
        self.assertEqual(self.cliente.get("/research/abcdef123456").status_code, 401)
        self.assertEqual(self.cliente.get("/research/abcdef123456", headers=self.h).status_code, 404)
        self.assertEqual(self.cliente.get("/research/..%2F..%2Fsegredo", headers=self.h).status_code, 404)
        self.jobs.mkdir()
        (self.jobs / "0123456789ab.json").write_text(json.dumps({"id": "0123456789ab", "status": "running",
                                                                 "etapa": "leitura"}))
        dados = self.cliente.get("/research/0123456789ab", headers=self.h).json()
        self.assertEqual(dados["status"], "interrompido")      # serviço reiniciou no meio do job
        self.assertEqual(dados["etapa"], "leitura")

    def test_modo_antigo_continua_sincrono(self):
        r = self.cliente.post("/research", headers=self.h, json={"topic": "Rust async", "num_queries": 1})
        self.assertEqual(r.status_code, 200)
        corpo = r.json()
        self.assertFalse(corpo["cached"])
        self.assertEqual(corpo["queries_used"], ["rust async"])
        self.assertEqual(corpo["sources_found"], 5)          # dedup por URL do modo antigo, sem filtro nem download
        self.assertNotIn("job_id", corpo)
        self.assertEqual(self.transporte.pedidos, [])
        sintese = (self.vault / "research" / "rust-async" / "synthesis.md").read_text(encoding="utf-8")
        self.assertNotIn("modo: profundo", sintese)


class TestUtilitarios(unittest.TestCase):
    def test_limpar_citacoes(self):
        self.assertEqual(researcher._limpar_citacoes("a [1] b [2; 4] c [0] d [5] e [x](http://y) [2](z)", 4),
                         "a [1] b [2][4] c  d  e [x](http://y) [2](z)")

    def test_gravacao_atomica_nao_estraga_o_arquivo_antigo(self):
        with tempfile.TemporaryDirectory() as tmp:
            alvo = Path(tmp) / "estado" / "job.json"
            researcher._gravar_json_atomico(alvo, {"status": "running"})
            with self.assertRaises(TypeError):
                researcher._gravar_json_atomico(alvo, {"status": object()})   # falha no meio da escrita
            self.assertEqual(json.loads(alvo.read_text()), {"status": "running"})
            self.assertEqual([p.name for p in alvo.parent.iterdir()], ["job.json"])


if __name__ == "__main__":
    unittest.main()
