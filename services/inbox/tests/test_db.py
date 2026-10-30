"""
Testes do SQLite + FTS5 (_shared/lib_db.py): índice do cofre, histórico do WhatsApp e cache de classificação.

    cd services/inbox
    venv/bin/python -m unittest tests.test_db -v

Tudo em diretório temporário; nenhum LLM, rede ou cofre real.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="alfred-db-test-"))

from _shared import lib_db  # noqa: E402


def _nota(vault: Path, rel: str, texto: str):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    return p


class Base(unittest.TestCase):
    def setUp(self):
        # ALFRED_DB só durante o teste: outros módulos de teste (test_whatsapp) rodam no mesmo processo
        env = mock.patch.dict(os.environ, {"ALFRED_DB": str(TMP / "alfred.db")})
        env.start()
        self.addCleanup(env.stop)
        self.vault = TMP / "cofre"
        shutil.rmtree(self.vault, ignore_errors=True)
        self.vault.mkdir(parents=True)
        for f in TMP.glob("alfred.db*"):
            f.unlink()


class TestIndiceCofre(Base):
    def test_busca_ignora_acento_e_caixa(self):
        _nota(self.vault, "ideias.md", "# Ideias\nHorário de funcionamento da loja")
        lib_db.indexar_cofre(self.vault, "pedro")
        self.assertEqual(lib_db.buscar_notas("horario")[0]["caminho"], "ideias.md")
        self.assertEqual(lib_db.buscar_notas("HORÁRIO funcionamento")[0]["caminho"], "ideias.md")

    def test_prefixo_acha_plural(self):
        _nota(self.vault, "a.md", "As pendências do mês")
        lib_db.indexar_cofre(self.vault, "alfred")
        self.assertTrue(lib_db.buscar_notas("pendência"))

    def test_consulta_com_caracteres_especiais_nao_levanta(self):
        _nota(self.vault, "a.md", "texto qualquer c-d e:f")
        lib_db.indexar_cofre(self.vault, "alfred")
        for q in ('a-b "c" d:e', "*", '"', "NEAR(", "", "   ", "OR AND NOT"):
            self.assertIsInstance(lib_db.buscar_notas(q), list)

    def test_and_cai_para_or(self):
        _nota(self.vault, "a.md", "orçamento do servidor")
        lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual(len(lib_db.buscar_notas("orçamento inexistente")), 1)

    def test_titulo_pesa_mais_que_corpo(self):
        _nota(self.vault, "podcast.md", "# Podcast\nalgo")
        _nota(self.vault, "outra.md", "podcast " * 3 + "no meio de um texto longo")
        lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual(lib_db.buscar_notas("podcast")[0]["caminho"], "podcast.md")

    def test_incremental_atualiza_e_remove(self):
        a = _nota(self.vault, "a.md", "banana")
        _nota(self.vault, "b.md", "banana")
        r = lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual((r["novos"], r["total"]), (2, 2))
        r = lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual((r["novos"], r["atualizados"], r["removidos"]), (0, 0, 0))
        a.write_text("laranja", encoding="utf-8")
        os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 5))
        (self.vault / "b.md").unlink()
        r = lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual((r["atualizados"], r["removidos"], r["total"]), (1, 1, 1))
        self.assertEqual(lib_db.buscar_notas("banana"), [])
        self.assertEqual(lib_db.buscar_notas("laranja")[0]["caminho"], "a.md")

    def test_ignora_pastas_com_ponto_e_nao_md(self):
        _nota(self.vault, ".obsidian/x.md", "segredo")
        _nota(self.vault, ".alfred/y.md", "segredo")
        _nota(self.vault, "audio.mp3", "segredo")
        lib_db.indexar_cofre(self.vault, "alfred")
        self.assertEqual(lib_db.buscar_notas("segredo"), [])

    def test_filtros_de_cofre_e_pasta(self):
        _nota(self.vault, "pendencias/x.md", "reunião")
        _nota(self.vault, "outros/y.md", "reunião")
        lib_db.indexar_cofre(self.vault, "alfred")
        lib_db.indexar_cofre(self.vault, "pedro")
        self.assertEqual(len(lib_db.buscar_notas("reunião")), 4)
        self.assertEqual(len(lib_db.buscar_notas("reunião", cofre="pedro")), 2)
        r = lib_db.buscar_notas("reunião", cofre="pedro", pasta="pendencias")
        self.assertEqual([x["caminho"] for x in r], ["pendencias/x.md"])

    def test_sem_vault_definido_degrada(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ALFRED_DB")
            os.environ.pop("VAULT_ALFRED", None)
            self.assertEqual(lib_db.buscar_notas("a"), [])
            self.assertFalse(lib_db.registrar_wa("m1", "j", "c", "oi"))
            self.assertIsNone(lib_db.cache_get("email", "x"))


class TestHistoricoWhatsApp(Base):
    JID = "5514999990001@s.whatsapp.net"

    def test_registrar_e_historico_em_ordem(self):
        for i, (quem, t) in enumerate([(False, "oi Pedro"), (True, "oi!"), (False, "tem o orçamento?")]):
            self.assertTrue(lib_db.registrar_wa(f"m{i}", self.JID, "Carlos", t, from_me=quem))
        h = lib_db.historico_contato(self.JID, n=5)
        self.assertEqual([l.split("] ", 1)[1] for l in h.splitlines()],
                         ["Carlos: oi Pedro", "Pedro: oi!", "Carlos: tem o orçamento?"])

    def test_idempotente_e_ignora_vazio(self):
        self.assertTrue(lib_db.registrar_wa("m1", self.JID, "C", "oi"))
        self.assertFalse(lib_db.registrar_wa("m1", self.JID, "C", "oi de novo"))
        self.assertFalse(lib_db.registrar_wa("m2", self.JID, "C", "   "))
        self.assertEqual(lib_db.estatisticas()["wa_msgs"], 1)

    def test_historico_exclui_ids_e_trunca(self):
        for i in range(6):
            lib_db.registrar_wa(f"m{i}", self.JID, "C", f"mensagem {i} " + "x" * 300)
        h = lib_db.historico_contato(self.JID, n=3, excluir=["m5"])
        self.assertNotIn("mensagem 5", h)
        self.assertIn("mensagem 4", h)
        self.assertEqual(len(h.splitlines()), 3)
        self.assertLessEqual(len(lib_db.historico_contato(self.JID, n=6, max_chars=200)), 200)
        self.assertEqual(lib_db.historico_contato("outro@s.whatsapp.net"), "")

    def test_busca_e_filtro_por_conversa(self):
        lib_db.registrar_wa("m1", self.JID, "Carlos", "preciso do orçamento do servidor")
        lib_db.registrar_wa("m2", "outro@s.whatsapp.net", "Ana", "orçamento da reforma")
        self.assertEqual(len(lib_db.buscar_wa("orcamento")), 2)
        r = lib_db.buscar_wa("orcamento", jid=self.JID)
        self.assertEqual([x["message_id"] for x in r], ["m1"])
        self.assertEqual(lib_db.buscar_wa('a-b "c"'), [])

    def test_retencao(self):
        lib_db.registrar_wa("velha", self.JID, "C", "mensagem antiga")
        lib_db.registrar_wa("nova", self.JID, "C", "mensagem nova")
        con = lib_db._conectar()
        with con:
            con.execute("UPDATE wa_msgs SET gravado=? WHERE message_id='velha'",
                        ((datetime.now() - timedelta(days=100)).isoformat(),))
        con.close()
        self.assertEqual(lib_db.podar_wa(0), 0)
        self.assertEqual(lib_db.podar_wa(90), 1)
        self.assertEqual(lib_db.buscar_wa("antiga"), [])      # o índice FTS acompanha o DELETE
        self.assertEqual(len(lib_db.buscar_wa("nova")), 1)


class TestCacheClassificacao(Base):
    R = {"categoria": "promocao", "confianca": 0.95, "resumo": "oferta"}

    def test_so_ativa_depois_de_n_confirmacoes(self):
        for i in range(lib_db.MINIMO_CONFIRMACOES):
            self.assertIsNone(lib_db.cache_get("email", "loja@x.com"), f"ativou cedo (i={i})")
            lib_db.cache_put("email", "loja@x.com", self.R)
        self.assertEqual(lib_db.cache_get("email", "loja@x.com")["categoria"], "promocao")
        self.assertEqual(lib_db.estatisticas()["cache_usos"], 1)

    def test_categoria_diferente_reinicia(self):
        for _ in range(3):
            lib_db.cache_put("email", "k", self.R)
        lib_db.cache_put("email", "k", {**self.R, "categoria": "transacional"})
        self.assertIsNone(lib_db.cache_get("email", "k"))

    def test_nao_cacheavel_apaga(self):
        for _ in range(3):
            lib_db.cache_put("email", "k", self.R)
        lib_db.cache_put("email", "k", {"categoria": "evento"}, cacheavel=False)
        self.assertIsNone(lib_db.cache_get("email", "k"))
        self.assertEqual(lib_db.estatisticas()["cache_entradas"], 0)

    def test_expira_por_ttl(self):
        for _ in range(3):
            lib_db.cache_put("email", "k", self.R)
        self.assertIsNotNone(lib_db.cache_get("email", "k"))
        con = lib_db._conectar()
        with con:
            con.execute("UPDATE cache_classif SET confirmado=?",
                        ((datetime.now() - timedelta(days=31)).isoformat(),))
        con.close()
        self.assertIsNone(lib_db.cache_get("email", "k"))
        lib_db.cache_put("email", "k", self.R)               # uma revalidação do LLM reativa
        self.assertIsNotNone(lib_db.cache_get("email", "k"))

    def test_canais_separados(self):
        for _ in range(3):
            lib_db.cache_put("email", "k", self.R)
        self.assertIsNone(lib_db.cache_get("whatsapp", "k"))


class TestToolsDoOpenWebUI(Base):
    """tools/alfred_vault_*.py (rodam no container): FTS5 quando há índice, substring quando não há."""

    @staticmethod
    def _tool(arquivo, **valves):
        spec = importlib.util.spec_from_file_location(arquivo, AQUI.parent.parent.parent / "tools" / f"{arquivo}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        t = mod.Tools()
        for k, v in valves.items():
            setattr(t.valves, k, v)
        return t

    def test_reader_usa_fts_e_cai_no_rglob(self):
        _nota(self.vault, "projetos/servidor.md", "# Servidor\nOrçamento do rack")
        lib_db.indexar_cofre(self.vault, "pedro")
        t = self._tool("alfred_vault_reader", vault_path=str(self.vault), db_path=str(lib_db.db_path()))
        r = t.search_vault("ORÇAMENTO rack")
        self.assertIn("projetos/servidor.md", r)
        self.assertNotIn("...", r)                         # trecho do FTS, não a prévia de 300 caracteres
        self.assertIn("projetos/servidor.md", t.search_vault("orcamento", folder="projetos"))
        self.assertIn("Nenhuma nota", t.search_vault("inexistente"))
        # nota nova, ainda fora do índice: o fallback por substring a encontra
        _nota(self.vault, "nova.md", "bananada fresca")
        self.assertIn("nova.md", t.search_vault("bananada"))
        # sem .db: tudo pelo rglob
        t2 = self._tool("alfred_vault_reader", vault_path=str(self.vault), db_path=str(TMP / "nao-existe.db"))
        self.assertIn("projetos/servidor.md", t2.search_vault("rack"))

    def test_writer_so_enxerga_o_proprio_cofre(self):
        _nota(self.vault, "a.md", "banana")
        lib_db.indexar_cofre(self.vault, "pedro")          # outro cofre: o writer (cofre "alfred") não pode ler
        t = self._tool("alfred_vault_writer", vault_path=str(self.vault), db_path=str(lib_db.db_path()))
        self.assertIn("a.md", t.search_vault("banana"))     # cai no rglob, que enxerga o mesmo diretório
        lib_db.indexar_cofre(self.vault, "alfred")
        self.assertIn("score", t.search_vault("banana"))    # agora veio do FTS


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
