"""
Testes da aba Ferramentas do Painel (painel_ferramentas.py, painel_web/ferramentas.yml).

    cd services/inbox
    venv/bin/python -m unittest tests.test_painel_ferramentas -v

Offline: vault temporário. Confere a navegação (Ferramentas no lugar de Redigir; Pesquisar logo abaixo de Conversa),
as ferramentas da conversa extraídas do lib_ferramentas e que o ferramentas.yml só cita o que existe.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
RAIZ = INBOX.parent.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TOKEN = "token-de-teste"
BASE = "http://127.0.0.1:7073"
TMP = Path(tempfile.mkdtemp(prefix="alfred-ferramentas-test-"))
os.environ.update({"SHELL_EXECUTOR_TOKEN": TOKEN, "VAULT_ALFRED": str(TMP / "vault")})

import main                  # noqa: E402
import painel                # noqa: E402
import painel_ferramentas    # noqa: E402
from _shared import lib_ferramentas  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _funcoes():
    return [f for g in painel_ferramentas.catalogo() for f in g["funcoes"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))
        self.cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE})

    def nav(self, html: str) -> str:
        return re.search(r'<nav class="nav".*?</nav>', html, re.S).group(0)


class TestNavegacao(Base):
    def test_ferramentas_no_lugar_de_redigir(self):
        nav = self.nav(self.cli.get("/painel/ferramentas").text)
        self.assertIn('href="/painel/ferramentas" aria-current="page"', nav)
        self.assertNotIn("/painel/redigir", nav)
        self.assertNotIn(">Redigir<", nav)

    def test_pesquisar_entre_conversa_e_pendencias(self):
        nav = self.nav(self.cli.get("/painel/ferramentas").text)
        rotas = re.findall(r'href="(/painel[^"]*)"', nav)
        self.assertEqual(rotas[:4], ["/painel", "/painel/conversa", "/painel/pesquisar", "/painel/pendencias"])

    def test_redigir_fica_dentro_de_ferramentas(self):
        html = self.cli.get("/painel/redigir").text
        self.assertIn('href="/painel/ferramentas" aria-current="page"', self.nav(html))
        self.assertIn('class="voltar" href="/painel/ferramentas"', html)

    def test_contador_do_redigir_na_navegacao_e_no_cartao(self):
        cont = {"aguardando": 0, "whatsapp": 0, "email": 0, "limpeza_aguardando": 0, "redigir_aguardando": 2}
        with mock.patch.object(painel, "_contadores", lambda: cont):
            html = self.cli.get("/painel/ferramentas").text
        self.assertIn('data-contador="redigir_aguardando">2<', self.nav(html))
        self.assertIn("2 aguardando", html)

    def test_sem_sessao_vai_para_o_login(self):
        cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))
        for rota in ("/painel/ferramentas", "/painel/ferramentas/conversa"):
            r = cli.get(rota, follow_redirects=False)
            self.assertEqual(r.status_code, 303, rota)


class TestTelas(Base):
    def test_lista_todas_as_ferramentas_da_conversa(self):
        html = self.cli.get("/painel/ferramentas").text
        registro = lib_ferramentas._REGISTRO
        self.assertGreater(len(registro), 30)
        for nome in registro:
            self.assertIn(f'id="t-{nome}"', html, nome)
        perigosas = sum(1 for f in registro.values() if f["perigosa"])
        self.assertEqual(html.count("Pede aprovação"), perigosas + 1)   # +1: o texto explicativo da seção
        self.assertIn(f"Ferramentas da conversa ({len(registro)})", html)

    def test_lista_todas_as_funcoes(self):
        html = self.cli.get("/painel/ferramentas").text
        for f in _funcoes():
            self.assertIn(f'href="/painel/ferramentas/{f["slug"]}"', html, f["slug"])

    def test_documentacao_de_cada_funcao(self):
        for f in _funcoes():
            r = self.cli.get(f"/painel/ferramentas/{f['slug']}")
            self.assertEqual(r.status_code, 200, f["slug"])
            self.assertRegex(r.text, r'<div class="prosa doc"><h2>', f"{f['slug']}: documentação vazia")
            for nome in f.get("ferramentas") or []:
                self.assertIn(f'id="t-{nome}"', r.text, f"{f['slug']}: {nome}")
            if f.get("tela"):
                self.assertIn(f'href="{f["tela"]}"', r.text, f["slug"])

    def test_funcao_desconhecida(self):
        r = self.cli.get("/painel/ferramentas/nao-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Função não encontrada", r.text)

    def test_area_e_parametros_extraidos_do_codigo(self):
        areas = {a["area"]: a for a in painel_ferramentas.ferramentas_conversa()}
        self.assertEqual(areas["Pesquisa"]["detalhe"], "Researcher")
        self.assertIn("documento_substituir", [t["nome"] for t in areas["Documento no WordCraft"]["ferramentas"]])
        t = next(t for t in areas["Pendências, jornal e limpeza"]["ferramentas"] if t["nome"] == "listar_pendencias")
        self.assertTrue(t["codigo"].startswith("services/_shared/lib_ferramentas.py:"))
        self.assertEqual(t["parametros"][0]["nome"], "status")
        self.assertTrue(t["parametros"][0]["tipo"].startswith("um destes: aguardando"))


class TestCatalogo(unittest.TestCase):
    """O ferramentas.yml só cita ferramenta, tela, contador e caminho que existem."""

    def test_slugs_unicos(self):
        slugs = [f["slug"] for f in _funcoes()]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_campos_obrigatorios(self):
        for f in _funcoes():
            for campo in ("slug", "nome", "resumo", "doc"):
                self.assertTrue(f.get(campo), f"{f.get('slug')}: falta {campo}")

    def test_ferramentas_citadas_existem(self):
        for f in _funcoes():
            for nome in f.get("ferramentas") or []:
                self.assertIn(nome, lib_ferramentas._REGISTRO, f"{f['slug']}: {nome}")

    def test_caminhos_citados_existem(self):
        for f in _funcoes():
            for c in (f.get("arquivos") or []) + (f.get("docs") or []):
                self.assertTrue((RAIZ / c).exists(), f"{f['slug']}: {c}")

    def test_contadores_citados_existem(self):
        chaves = set(painel._contadores())
        for f in _funcoes():
            if f.get("contador"):
                self.assertIn(f["contador"], chaves, f["slug"])

    def test_telas_citadas_abrem(self):
        cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))
        cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE})
        for f in _funcoes():
            if f.get("tela"):
                self.assertTrue(f["tela"].startswith("/painel/"), f["slug"])
                self.assertEqual(cli.get(f["tela"]).status_code, 200, f["tela"])


if __name__ == "__main__":
    unittest.main()
