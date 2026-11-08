"""
Testes da _shared/lib_academico (OpenAlex e Semantic Scholar), tudo offline: httpx trocado por MockTransport.

    cd services/researcher
    venv/bin/python -m unittest tests.test_academico -v
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))   # services/
from _shared import lib_academico as academico   # noqa: E402


def openalex_work(titulo, doi=None, landing=None, pdf=None, ano=2018, citacoes=3200, resumo=("Finds", "subnetworks"),
                  autores=("Jonathan Frankle", "Michael Carbin")):
    return {
        "id": "https://openalex.org/W2963446712",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": titulo,
        "publication_year": ano,
        "cited_by_count": citacoes,
        "authorships": [{"author": {"display_name": a}} for a in autores],
        "primary_location": {"landing_page_url": landing, "pdf_url": pdf},
        "locations": [{"landing_page_url": landing, "pdf_url": pdf}],
        "abstract_inverted_index": {palavra: [i] for i, palavra in enumerate(resumo)},
    }


def s2_paper(titulo, arxiv=None, doi=None, ano=2018, citacoes=10, resumo="Resumo.", autores=("A. Um", "B. Dois", "C. Três", "D. Quatro")):
    ids = {}
    if arxiv:
        ids["ArXiv"] = arxiv
    if doi:
        ids["DOI"] = doi
    return {"paperId": "abc", "title": titulo, "abstract": resumo, "year": ano, "citationCount": citacoes,
            "externalIds": ids, "url": "https://www.semanticscholar.org/paper/abc",
            "authors": [{"name": a} for a in autores]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.pedidos: list[httpx.Request] = []
        self.respostas = {}          # host → lista de respostas (uma por pedido) ou uma resposta fixa
        p = mock.patch.object(academico, "_transporte", httpx.MockTransport(self._responder))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(academico, "ESPERA_429", 0)
        p2.start()
        self.addCleanup(p2.stop)

    def _responder(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        resposta = self.respostas.get(request.url.host)
        if resposta is None:
            return httpx.Response(404, text="rota sem resposta no teste")
        if isinstance(resposta, list):
            return resposta.pop(0) if resposta else httpx.Response(500, text="acabaram as respostas")
        return resposta


class TestOpenAlex(_Base):
    def test_formato_comum_com_ids_e_resumo(self):
        self.respostas["api.openalex.org"] = httpx.Response(200, json={"results": [
            openalex_work("The Lottery Ticket Hypothesis", doi="10.48550/ARXIV.1803.03635",
                          landing="https://arxiv.org/abs/1803.03635v5", pdf="https://arxiv.org/pdf/1803.03635v5"),
            openalex_work("Só DOI", doi="10.1038/Nature17946", landing="https://www.nature.com/articles/nature17946"),
            openalex_work("Sem nada", landing="https://exemplo.org/artigo", ano="2018", citacoes=None, autores=()),
            openalex_work("", landing="https://exemplo.org/sem-titulo"),
            "lixo",
        ]})
        r = academico.buscar("lottery ticket hypothesis", "openalex", 5)
        self.assertEqual([x["url"] for x in r], ["https://arxiv.org/abs/1803.03635", "https://doi.org/10.1038/nature17946",
                                                 "https://exemplo.org/artigo"])
        arxiv, doi, nada = r
        self.assertEqual(arxiv["url_html"], "https://arxiv.org/html/1803.03635")   # sem o v5
        self.assertEqual(arxiv["arxiv_id"], "1803.03635")
        self.assertEqual(arxiv["doi"], "10.48550/arxiv.1803.03635")
        self.assertEqual(arxiv["snippet"], "Finds subnetworks")                     # índice invertido → texto
        self.assertEqual((arxiv["ano"], arxiv["citacoes"], arxiv["source"]), (2018, 3200, "OpenAlex"))
        self.assertEqual(arxiv["autores"], ["Jonathan Frankle", "Michael Carbin"])
        self.assertEqual(academico.identificador(arxiv), "arXiv:1803.03635")
        self.assertEqual((doi["url_html"], academico.identificador(doi)), ("", "doi:10.1038/nature17946"))
        self.assertEqual((nada["ano"], nada["citacoes"], nada["autores"], academico.identificador(nada)), (None, None, [], ""))
        # um pedido só, com a consulta e o limite; sem mailto quando o .env não tem
        self.assertEqual(len(self.pedidos), 1)
        params = dict(self.pedidos[0].url.params)
        self.assertEqual((params["search"], params["per-page"]), ("lottery ticket hypothesis", "5"))
        self.assertNotIn("mailto", params)
        self.assertEqual(self.pedidos[0].headers["user-agent"], academico.USER_AGENT)

    def test_mailto_do_env_e_limite_maximo(self):
        self.respostas["api.openalex.org"] = httpx.Response(200, json={"results": []})
        with mock.patch.dict(os.environ, {"OPENALEX_MAILTO": "pedro@example.org"}):
            self.assertEqual(academico.buscar("x", "openalex", 999), [])
        params = dict(self.pedidos[0].url.params)
        self.assertEqual((params["mailto"], params["per-page"]), ("pedro@example.org", str(academico.LIMITE_MAX)))

    def test_erros_viram_erro_academico(self):
        for resposta in (httpx.Response(500, text="x"), httpx.Response(429, text="x"),
                         httpx.Response(200, text="não é json"), httpx.Response(200, json=[1, 2])):
            self.pedidos.clear()
            self.respostas["api.openalex.org"] = resposta
            with self.assertRaises(academico.ErroAcademico):
                academico.buscar("x", "openalex")
            self.assertEqual(len(self.pedidos), 1)         # OpenAlex não repete em 429
        with mock.patch.object(academico, "_transporte", httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("fora")))):
            with self.assertRaises(academico.ErroAcademico):
                academico.buscar("x", "openalex")

    def test_consulta_vazia_e_fonte_desconhecida(self):
        self.assertEqual(academico.buscar("   ", "openalex"), [])
        self.assertEqual(self.pedidos, [])
        with self.assertRaises(academico.ErroAcademico):
            academico.buscar("x", "scholar")


class TestSemanticScholar(_Base):
    def test_formato_e_segunda_tentativa_no_429(self):
        self.respostas["api.semanticscholar.org"] = [
            httpx.Response(429, text="rate limit"),
            httpx.Response(200, json={"data": [
                s2_paper("Paper no arXiv", arxiv="2101.03961", doi="10.1/x"),
                s2_paper("Só DOI", doi="10.1/Y", autores=("Z",)),
                s2_paper("Sem IDs"),
            ]}),
        ]
        r = academico.buscar("mixture of experts", "semanticscholar", 3)
        self.assertEqual(len(self.pedidos), 2)
        params = dict(self.pedidos[1].url.params)
        self.assertEqual((params["query"], params["limit"], params["fields"]),
                         ("mixture of experts", "3", academico.S2_FIELDS))
        self.assertEqual([x["url"] for x in r], ["https://arxiv.org/abs/2101.03961", "https://doi.org/10.1/y",
                                                 "https://www.semanticscholar.org/paper/abc"])
        self.assertEqual(r[0]["url_html"], "https://arxiv.org/html/2101.03961")
        self.assertEqual(r[0]["autores"], ["A. Um", "B. Dois", "C. Três"])            # no máximo 3
        self.assertEqual(r[0]["source"], "Semantic Scholar")
        self.assertEqual(r[2]["url_html"], "")

    def test_dois_429_pulam_a_fonte(self):
        self.respostas["api.semanticscholar.org"] = [httpx.Response(429), httpx.Response(429)]
        with self.assertRaises(academico.ErroAcademico):
            academico.buscar("x", "semanticscholar")
        self.assertEqual(len(self.pedidos), 2)


class TestBuscarTodas(_Base):
    def test_junta_dedup_e_pula_fonte_que_falhou(self):
        self.respostas["api.openalex.org"] = httpx.Response(200, json={"results": [
            openalex_work("Lottery Ticket (OpenAlex)", landing="https://arxiv.org/abs/1803.03635"),
            openalex_work("Outro artigo", doi="10.1/outro", landing="https://x.example/"),
        ]})
        self.respostas["api.semanticscholar.org"] = httpx.Response(200, json={"data": [
            s2_paper("Lottery Ticket (S2)", arxiv="1803.03635"),      # mesmo artigo → mesma URL → fora
            s2_paper("Terceiro", doi="10.1/terceiro"),
        ]})
        r = academico.buscar_todas("lottery ticket", limite=5)
        self.assertEqual([x["title"] for x in r], ["Lottery Ticket (OpenAlex)", "Outro artigo", "Terceiro"])
        self.assertEqual([p.url.host for p in self.pedidos], ["api.openalex.org", "api.semanticscholar.org"])

        self.pedidos.clear()
        self.respostas["api.openalex.org"] = httpx.Response(503)
        with mock.patch.object(academico, "log") as log:
            r = academico.buscar_todas("lottery ticket", limite=5)
        self.assertEqual([x["title"] for x in r], ["Lottery Ticket (S2)", "Terceiro"])
        self.assertIn("pulando openalex", " ".join(str(c) for c in log.call_args_list))

    def test_fontes_do_perfis_e_lista_vazia(self):
        self.respostas["api.semanticscholar.org"] = httpx.Response(200, json={"data": [s2_paper("Um")]})
        r = academico.buscar_todas("x", ["semanticscholar", "inexistente"], 2)
        self.assertEqual([x["title"] for x in r], ["Um"])
        self.assertEqual([p.url.host for p in self.pedidos], ["api.semanticscholar.org"])   # a desconhecida só loga
        self.assertEqual(academico.buscar_todas("x", [], 2), [])


if __name__ == "__main__":
    unittest.main()
