"""
Testes do RAG local (_shared/lib_rag.py): pedaços, fusão RRF, queda do ChromaDB, indexação incremental e contexto().

    cd services/inbox
    venv/bin/python -m unittest tests.test_rag -v

Offline: o ChromaDB vira uma coleção em memória e os embeddings um saco de palavras determinístico (com sinônimos,
para a busca vetorial achar o que o FTS5 não acha). Cofres e banco em diretório temporário.
"""

import math
import os
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="alfred-rag-test-"))

from _shared import lib_rag  # noqa: E402

SINONIMOS = {"automovel": "carro", "veiculo": "carro"}


def _embed(texto: str) -> list[float]:
    v = [0.0] * 512
    for t in lib_rag._sem_acento(texto).split():
        t = "".join(c for c in t if c.isalnum())
        if len(t) >= 3:
            v[zlib.crc32(SINONIMOS.get(t, t).encode()) % 512] += 1.0
    return v


class ChromaFalso:
    """Coleção em memória com a mesma interface do lib_chromadb (métodos usados pelo lib_rag)."""

    def __init__(self):
        self.online, self.cid, self.docs = True, "col-1", {}

    def ensure_collection(self, nome):
        return self.cid if self.online else None

    def upsert_document(self, cid, doc_id, texto, meta, vetor):
        if not self.online:
            return False
        self.docs[doc_id] = (texto, {k: str(v) for k, v in meta.items()}, vetor)
        return True

    def delete_document(self, cid, doc_id):
        if not self.online:
            return False
        self.docs.pop(doc_id, None)
        return True

    def query_similar(self, cid, vetor, n_results=3, where=None):
        if not self.online:
            return []

        def dist(v):
            na, nb = math.sqrt(sum(x * x for x in vetor)), math.sqrt(sum(x * x for x in v))
            return 1 - sum(a * b for a, b in zip(vetor, v)) / (na * nb) if na and nb else 1.0

        achados = sorted((dist(v), i, t, m) for i, (t, m, v) in self.docs.items()
                         if not where or all(m.get(k) == val for k, val in where.items()))
        return [{"id": i, "distance": d, "metadata": m, "document": t} for d, i, t, m in achados[:n_results]]

    def caminhos(self):
        return {m["caminho"] for _, m, _ in self.docs.values()}


def _nota(vault: Path, rel: str, texto: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    return p


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        self.alfred, self.pedro = TMP / "alfred", TMP / "pedro"
        self.alfred.mkdir(parents=True)
        self.pedro.mkdir(parents=True)
        self.chroma = ChromaFalso()
        self.embeds = []   # textos mandados ao "Ollama" na indexação

        def embed_batch(textos):
            self.embeds += textos
            return [_embed(t) for t in textos]

        for p in (mock.patch.dict(os.environ, {"ALFRED_DB": str(TMP / "alfred.db"), "VAULT_ALFRED": str(self.alfred),
                                               "VAULT_PEDRO": str(self.pedro), "MODEL_EMBED": "bge-m3"}),
                  mock.patch.object(lib_rag, "chroma", self.chroma),
                  mock.patch.object(lib_rag, "DISTANCIA_MAX", 0.9),   # o saco de palavras dá cossenos baixos
                  mock.patch.object(lib_rag, "ollama_embed", _embed),
                  mock.patch.object(lib_rag, "ollama_embed_batch", embed_batch)):
            p.start()
            self.addCleanup(p.stop)


class TestPedacos(unittest.TestCase):
    def test_secoes_tamanho_maximo_e_frontmatter(self):
        texto = ("---\ntitle: x\ntags: [a]\n---\n# Nota\nintro curta\n\n## Grande\n" + ("palavra " * 400) +
                 "\n\n## Outra\n" + ("linha de texto " * 30))
        partes = lib_rag.pedacos(texto, maximo=1200)
        self.assertTrue(all(len(p["texto"]) <= 1200 for p in partes))
        self.assertNotIn("title: x", " ".join(p["texto"] for p in partes))
        self.assertIn("Grande", [p["secao"] for p in partes])
        self.assertEqual(partes[-1]["secao"], "Outra")
        self.assertTrue(partes[-1]["texto"].startswith("## Outra"))

    def test_titulo_dentro_de_codigo_nao_abre_secao(self):
        texto = "## Script\n```bash\n# comentário\necho oi\n```\n" + "x " * 200 + "\n## Depois\n" + "y " * 200
        self.assertEqual([p["secao"] for p in lib_rag.pedacos(texto)], ["Script", "Depois"])

    def test_secoes_miudas_juntam(self):
        partes = lib_rag.pedacos("# A\num\n## B\ndois\n## C\ntrês")
        self.assertEqual(len(partes), 1)
        self.assertIn("três", partes[0]["texto"])

    def test_imagem_base64_e_lixo_longo_saem(self):
        texto = "# Print\nVeja a tela: ![](data:image/png;base64," + "iVBORw0KGgo" * 500 + ")\nfim da nota"
        partes = lib_rag.pedacos(texto)
        self.assertEqual(len(partes), 1)
        self.assertNotIn("base64", partes[0]["texto"])
        self.assertIn("fim da nota", partes[0]["texto"])

    def test_vazia(self):
        self.assertEqual(lib_rag.pedacos("---\ntitle: x\n---\n\n   \n"), [])


class TestFusao(unittest.TestCase):
    def _r(self, caminho, trecho="t", cofre="pedro"):
        return {"cofre": cofre, "caminho": caminho, "titulo": caminho, "trecho": trecho}

    def test_rrf_premia_quem_esta_nas_duas_listas(self):
        fts = [self._r("a.md"), self._r("b.md", "snippet")]
        vetor = [self._r("b.md", "pedaço inteiro"), self._r("c.md")]
        res = lib_rag._fundir(fts, vetor, 10)
        self.assertEqual([r["caminho"] for r in res], ["b.md", "a.md", "c.md"])
        self.assertEqual([r["origem"] for r in res], ["ambos", "fts", "vetor"])
        self.assertAlmostEqual(res[0]["pontuacao"], round(1 / 62 + 1 / 61, 4))
        self.assertEqual(res[0]["trecho"], "pedaço inteiro")

    def test_mesmo_caminho_em_cofres_diferentes_nao_funde(self):
        res = lib_rag._fundir([self._r("x.md", cofre="alfred")], [self._r("x.md", cofre="pedro")], 10)
        self.assertEqual(sorted((r["cofre"], r["origem"]) for r in res), [("alfred", "fts"), ("pedro", "vetor")])

    def test_limite(self):
        res = lib_rag._fundir([self._r(f"{i}.md") for i in range(5)], [], 3)
        self.assertEqual([r["caminho"] for r in res], ["0.md", "1.md", "2.md"])


class TestBusca(Base):
    def setUp(self):
        super().setUp()
        _nota(self.pedro, "garagem.md", "# Garagem\nRevisão do automóvel marcada na oficina do bairro.")
        _nota(self.pedro, "projetos/rede.md", "# Rede\nTrocar o roteador e passar cabo até o escritório.")
        _nota(self.pedro, "projetos/carro.md", "# Carro\nVender o carro antigo antes de dezembro.")
        _nota(self.alfred, "research/carro-eletrico/synthesis.md", "# Carro elétrico\nAutonomia e recarga.")
        lib_rag.indexar()

    def test_vetor_acha_sinonimo_que_o_fts_nao_acha(self):
        res = {r["caminho"]: r for r in lib_rag.buscar("carro")}
        self.assertEqual(res["garagem.md"]["origem"], "vetor")
        self.assertIn("automóvel", res["garagem.md"]["trecho"])
        self.assertEqual(res["projetos/carro.md"]["origem"], "ambos")
        self.assertEqual(set(lib_rag.buscar("carro")[0]), {"cofre", "caminho", "titulo", "trecho", "pontuacao", "origem"})

    def test_filtros_cofre_e_pasta(self):
        self.assertEqual({r["cofre"] for r in lib_rag.buscar("carro", cofre="alfred")}, {"alfred"})
        self.assertEqual([r["caminho"] for r in lib_rag.buscar("carro", cofre="pedro", pasta="projetos")],
                         ["projetos/carro.md"])

    def test_vizinho_distante_fica_de_fora(self):
        self.assertEqual(lib_rag.buscar("orçamento inexistente xyzw"), [])

    def test_consulta_vazia(self):
        self.assertEqual(lib_rag.buscar("   "), [])


class TestChromaFora(Base):
    def setUp(self):
        super().setUp()
        _nota(self.pedro, "garagem.md", "# Garagem\nRevisão do automóvel marcada na oficina do bairro.")
        _nota(self.pedro, "projetos/carro.md", "# Carro\nVender o carro antigo antes de dezembro.")

    def test_indexar_offline_atualiza_so_fts_e_retoma_depois(self):
        self.chroma.online = False
        res = lib_rag.indexar(cofre="pedro")["pedro"]
        self.assertEqual(res["fts"]["novos"], 2)
        self.assertTrue(res["vetores"]["offline"])
        self.assertEqual(self.embeds, [])
        busca = lib_rag.buscar("carro")
        self.assertEqual([(r["caminho"], r["origem"]) for r in busca], [("projetos/carro.md", "fts")])
        self.chroma.online = True
        self.assertEqual(lib_rag.indexar(cofre="pedro")["pedro"]["vetores"]["novas"], 2)

    def test_chromadb_cai_depois_de_indexado(self):
        lib_rag.indexar()
        self.chroma.online = False
        self.assertEqual([r["origem"] for r in lib_rag.buscar("carro")], ["fts"])

    def test_resposta_estranha_do_chromadb_nao_levanta(self):
        lib_rag.indexar()
        with mock.patch.object(self.chroma, "query_similar", side_effect=TypeError("resposta sem 'ids'")):
            self.assertEqual([r["origem"] for r in lib_rag.buscar("carro")], ["fts"])

    def test_ollama_fora(self):
        lib_rag.indexar()
        with mock.patch.object(lib_rag, "ollama_embed", lambda t: []):
            self.assertEqual([r["origem"] for r in lib_rag.buscar("carro")], ["fts"])


class TestIncremental(Base):
    def _vetores(self, **kw):
        return lib_rag.indexar(cofre="pedro", **kw)["pedro"]["vetores"]

    def test_nova_alterada_apagada(self):
        a = _nota(self.pedro, "a.md", "# A\n" + "texto da seção um " * 30 + "\n## Dois\n" + "outra seção " * 40)
        _nota(self.pedro, "b.md", "# B\nnota curta")
        r = self._vetores()
        self.assertEqual((r["novas"], r["alteradas"], r["removidas"], r["total"]), (2, 0, 0, 2))
        self.assertEqual(sorted(self.chroma.docs), ["pedro:a.md#0", "pedro:a.md#1", "pedro:b.md#0"])

        self.embeds.clear()   # nada mudou → nada vai ao Ollama
        r = self._vetores()
        self.assertEqual((r["novas"], r["alteradas"], r["removidas"]), (0, 0, 0))
        self.assertEqual(self.embeds, [])

        os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))   # só o mtime mudou (touch)
        self.assertEqual(self._vetores()["alteradas"], 0)
        self.assertEqual(self.embeds, [])

        a.write_text("# A\nagora tem uma seção só", encoding="utf-8")   # menos pedaços: o que sobrou sai
        os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 20))
        r = self._vetores()
        self.assertEqual((r["novas"], r["alteradas"]), (0, 1))
        self.assertEqual(sorted(self.chroma.docs), ["pedro:a.md#0", "pedro:b.md#0"])
        self.assertIn("seção só", self.chroma.docs["pedro:a.md#0"][0])

        (self.pedro / "b.md").unlink()
        _nota(self.pedro, "c/nova.md", "# Nova\nchegou agora")
        r = self._vetores()
        self.assertEqual((r["novas"], r["removidas"], r["total"]), (1, 1, 2))
        self.assertEqual(self.chroma.caminhos(), {"a.md", "c/nova.md"})
        self.assertEqual({x["caminho"] for x in lib_rag.buscar("nota curta")}, set())

    def test_pastas_com_ponto_ignoradas(self):
        _nota(self.pedro, ".obsidian/x.md", "config")
        _nota(self.pedro, "ok.md", "# Ok\nconteúdo")
        self._vetores()
        self.assertEqual(self.chroma.caminhos(), {"ok.md"})

    def test_colecao_recriada_reindexa_tudo(self):
        _nota(self.pedro, "a.md", "# A\nconteúdo")
        self._vetores()
        self.chroma.cid, self.chroma.docs = "col-2", {}   # volume do ChromaDB zerado
        self.assertEqual(self._vetores()["novas"], 1)
        self.assertEqual(sorted(self.chroma.docs), ["pedro:a.md#0"])

    def test_tudo_reembeda(self):
        _nota(self.pedro, "a.md", "# A\nconteúdo")
        self._vetores()
        self.embeds.clear()
        self.assertEqual(self._vetores(tudo=True)["alteradas"], 1)
        self.assertEqual(len(self.embeds), 1)

    def test_falha_de_embedding_tenta_de_novo_e_para_se_ollama_caiu(self):
        for i in range(5):
            _nota(self.pedro, f"n{i}.md", f"# N{i}\nconteúdo {i}")
        with mock.patch.object(lib_rag, "ollama_embed_batch", lambda textos: [[] for _ in textos]):
            r = self._vetores()
        self.assertEqual((r["novas"], r["falhas"], r["total"]), (0, lib_rag.FALHAS_SEGUIDAS_MAX, 0))
        r = self._vetores()
        self.assertEqual((r["novas"], r["falhas"], r["total"]), (5, 0, 5))

    def test_varredura_interrompida_nao_apaga_nada(self):
        for i in range(4):
            _nota(self.pedro, f"n{i}.md", f"# N{i}\nconteúdo {i}")
        self._vetores()
        (self.pedro / "n3.md").unlink()
        for i in range(3):
            _nota(self.pedro, f"n{i}.md", f"# N{i}\nmudou {i}")
            os.utime(self.pedro / f"n{i}.md", (1, 1e9 + i))
        with mock.patch.object(self.chroma, "upsert_document", lambda *a: False):
            r = self._vetores()
        self.assertEqual((r["falhas"], r["removidas"], r["total"]), (3, 0, 4))
        self.assertIn("pedro:n3.md#0", self.chroma.docs)
        r = self._vetores()   # ChromaDB de volta: agora sim
        self.assertEqual((r["alteradas"], r["removidas"], r["total"]), (3, 1, 3))
        self.assertNotIn("pedro:n3.md#0", self.chroma.docs)

    def test_modelo_de_embedding_define_a_colecao(self):
        with mock.patch.dict(os.environ, {"MODEL_EMBED": "bge-m3:latest"}):
            self.assertEqual(lib_rag.colecao(), "cofres-bge-m3")
        with mock.patch.dict(os.environ, {"MODEL_EMBED": "nomic-embed-text-v2-moe:latest"}):
            self.assertEqual(lib_rag.colecao(), "cofres-nomic-embed-text-v2-moe")


class TestContexto(Base):
    def setUp(self):
        super().setUp()
        _nota(self.pedro, "garagem.md", "# Garagem\nRevisão do automóvel marcada na oficina do bairro.")
        _nota(self.pedro, "projetos/carro.md", "# Carro\nVender o carro antigo antes de dezembro.")
        lib_rag.indexar()

    def test_citacoes_numeradas(self):
        ctx = lib_rag.contexto("carro")
        self.assertIn("[1] projetos/carro.md (pedro)\n", ctx)
        self.assertIn("[2] garagem.md (pedro)\n", ctx)
        self.assertIn("Vender o carro antigo", ctx)

    def test_limite_de_caracteres(self):
        _nota(self.pedro, "longa.md", "# Longa\n" + "carro " * 1000)
        lib_rag.indexar()
        ctx = lib_rag.contexto("carro", limite_chars=700)
        self.assertLessEqual(len(ctx), 700)
        self.assertIn("[1] ", ctx)

    def test_nada_achado(self):
        self.assertEqual(lib_rag.contexto("xyzw inexistente"), "")

    def test_so_fts_usa_o_pedaco_da_nota_e_nao_o_snippet(self):
        self.chroma.online = False
        _nota(self.pedro, "manual.md", "# Manual\n## Instalação\n" + "passo a passo de instalação " * 20 +
              "\n## Garantia\n" + "a garantia do roteador cobre defeitos de fábrica por dois anos. " * 8)
        lib_rag.indexar(cofre="pedro")
        ctx = lib_rag.contexto("garantia roteador")
        self.assertIn("[1] manual.md (pedro)\n## Garantia", ctx)


if __name__ == "__main__":
    unittest.main()
