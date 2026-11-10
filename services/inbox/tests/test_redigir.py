"""
Testes de redigir.py e das rotas /redigir e /painel/redigir.

    cd services/inbox
    venv/bin/python -m unittest tests.test_redigir -v

Offline: vault temporário; Laya, memória, RAG, detector de compromisso e LLM mockados.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TOKEN = "token-de-teste"
BASE = "http://127.0.0.1:7073"
TMP = Path(tempfile.mkdtemp(prefix="alfred-redigir-test-"))
os.environ.update({"SHELL_EXECUTOR_TOKEN": TOKEN, "VAULT_ALFRED": str(TMP / "vault")})

import core      # noqa: E402
import main      # noqa: E402
import redigir   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-redigir-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prompts: list[str] = []
        self.rotulo = "candidatura"
        self.achado = None
        self.notificado = []

        def llm(prompt, **_):
            self.prompts.append(prompt)
            return {"corpo": "Texto do Alfred"}

        for p in (mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.tmp)}),
                  mock.patch.object(redigir.lib_laya, "escolher", lambda *a: self.rotulo),
                  mock.patch.object(redigir.lib_memoria, "contexto", lambda *a, **k: "FATO DA MEMORIA"),
                  mock.patch.object(redigir.lib_rag, "buscar", lambda *a, **k: [{"caminho": "n.md", "trecho": "NOTA DO COFRE"}]),
                  mock.patch.object(redigir, "data_citada", lambda *a, **k: self.achado),
                  mock.patch.object(redigir, "ollama_generate_json", llm),
                  mock.patch.object(redigir, "_perfil", lambda: {"descricao": "Pedro", "tom": "direto"}),
                  mock.patch.object(core, "_notify", lambda *a: self.notificado.append(a))):
            p.start()
            self.addCleanup(p.stop)
        self.cli = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))

    def pedir(self, **campos):
        corpo = {"para": ["a@b.c"], "assunto": "Application", "notas": "", "eventos": [], **campos}
        return self.cli.post("/redigir", json=corpo, headers=AUTH)

    def entrar(self) -> str:
        self.cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE})
        return re.search(r'csrf-token" content="([^"]+)', self.cli.get("/painel/redigir").text).group(1)


class TestContexto(Base):
    def test_laya_decide(self):
        self.assertEqual(redigir.classificar("Application", "", ""), "candidatura")

    def test_termo_explicito_vence_rotulo_errado(self):
        self.assertEqual(redigir.classificar("Entrevista técnica", "", ""), "entrevista")
        self.assertEqual(redigir.classificar("Convite", "confirmar reunião", ""), "evento")

    def test_sem_laya(self):
        self.rotulo = None
        self.assertEqual(redigir.classificar("Oi", "", ""), "outro")
        self.assertEqual(redigir.classificar("Webinar", "", ""), "evento")


class TestDataCitada(unittest.TestCase):
    # 2026-11-09 é segunda-feira
    REF = datetime(2026, 11, 9, 2, 0, tzinfo=core._now().tzinfo)

    def resolver(self, devolvido: datetime, texto: str) -> str:
        return redigir._primeira_ocorrencia(devolvido.replace(tzinfo=self.REF.tzinfo), texto, self.REF).strftime("%Y-%m-%d")

    def test_proxima_terca_e_a_primeira(self):
        self.assertEqual(self.resolver(datetime(2026, 11, 17, 14), "próxima terça às 14h"), "2026-11-10")

    def test_mesmo_dia_da_semana_vai_para_a_outra(self):
        self.assertEqual(self.resolver(datetime(2026, 11, 16, 9), "na segunda às 9h"), "2026-11-16")

    def test_data_numerica_manda(self):
        self.assertEqual(self.resolver(datetime(2026, 11, 17, 14), "terça, 17/11"), "2026-11-17")

    def test_ordinal_nao_e_dia(self):
        self.assertEqual(self.resolver(datetime(2026, 11, 20, 14), "segunda entrevista em breve"), "2026-11-20")


class TestRedigir(Base):
    def test_candidatura_devolve_na_hora_com_memoria(self):
        r = self.pedir().json()
        self.assertEqual((r["status"], r["texto"]), ("pronto", "Texto do Alfred"))
        self.assertIn("FATO DA MEMORIA", self.prompts[0])
        self.assertIn("NOTA DO COFRE", self.prompts[0])
        self.assertEqual(redigir.listar(), [])

    def test_exige_token(self):
        self.assertEqual(self.cli.post("/redigir", json={"assunto": "x"}).status_code, 401)

    def test_composicao_vazia(self):
        self.assertEqual(self.pedir(assunto="").status_code, 422)

    def test_llm_fora(self):
        with mock.patch.object(redigir, "ollama_generate_json", lambda *a, **k: None):
            self.assertEqual(self.pedir().status_code, 502)

    def _entrevista(self, eventos):
        amanha = (core._now() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        self.achado = {"titulo": "Entrevista", "inicio": amanha, "fim": None, "dia_inteiro": False, "local": ""}
        return amanha, eventos(amanha)

    def test_entrevista_com_choque_espera_o_painel(self):
        _, ev = self._entrevista(lambda d: [{"titulo": "Dentista", "inicio": d.isoformat(),
                                             "fim": (d + timedelta(minutes=30)).isoformat()}])
        r = self.pedir(assunto="Entrevista", eventos=ev).json()
        self.assertEqual(r["status"], "aguardando")
        self.assertEqual(self.cli.get(f"/redigir/{r['id']}", headers=AUTH).json(), {"status": "aguardando", "texto": ""})
        self.assertIn("Dentista", r["agenda"]["conflitos"][0])
        self.assertIn("Choque", self.prompts[0])
        self.assertIn("Não confirme", self.prompts[0])
        self.assertEqual(len(self.notificado), 1)

    def test_sem_choque(self):
        _, ev = self._entrevista(lambda d: [{"titulo": "Almoço", "inicio": (d - timedelta(hours=3)).isoformat()}])
        r = self.pedir(assunto="Entrevista", eventos=ev).json()
        self.assertEqual(r["agenda"]["conflitos"], [])
        self.assertIn("Nenhum choque", r["agenda"]["resumo"])

    def test_dia_inteiro_nao_choca(self):
        _, ev = self._entrevista(lambda d: [{"titulo": "Feriado", "inicio": d.date().isoformat(), "dia_inteiro": True}])
        r = self.pedir(assunto="Entrevista", eventos=ev).json()
        self.assertEqual(r["agenda"]["conflitos"], [])
        self.assertEqual(len(r["agenda"]["eventos"]), 1)

    def test_calendario_ilegivel_avisa(self):
        r = self.pedir(assunto="Entrevista", eventos=None).json()
        self.assertEqual(r["status"], "aguardando")
        self.assertIn("não consegui ler o calendário", r["agenda"]["resumo"])


class TestReformular(Base):
    def test_devolve_so_o_trecho_e_nao_passa_pelo_painel(self):
        r = self.cli.post("/reformular", json={"trecho": "texto velho", "instrucao": "mais formal", "assunto": "Entrevista"}, headers=AUTH)
        self.assertEqual(r.json(), {"texto": "Texto do Alfred"})
        self.assertIn("mais formal", self.prompts[0])
        self.assertIn("texto velho", self.prompts[0])
        self.assertEqual(redigir.listar(), [])

    def test_sem_trecho(self):
        self.assertEqual(self.cli.post("/reformular", json={"trecho": " "}, headers=AUTH).status_code, 422)

    def test_exige_token(self):
        self.assertEqual(self.cli.post("/reformular", json={"trecho": "x"}).status_code, 401)


class TestPainel(Base):
    def _pendente(self):
        return self.pedir(assunto="Entrevista").json()["id"]

    def test_tela_liberar_com_edicao(self):
        rid = self._pendente()
        csrf = self.entrar()
        self.assertIn("Texto do Alfred", self.cli.get("/painel/redigir").text)
        h = {"X-CSRF-Token": csrf, "Origin": BASE}
        r = self.cli.post(f"/painel/api/redigir/{rid}/liberar", json={"texto": "Editado"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.cli.get(f"/redigir/{rid}", headers=AUTH).json(), {"status": "liberado", "texto": "Editado"})
        self.assertEqual(self.cli.post(f"/painel/api/redigir/{rid}/recusar", json={}, headers=h).status_code, 409)

    def test_recusar(self):
        rid = self._pendente()
        h = {"X-CSRF-Token": self.entrar(), "Origin": BASE}
        self.cli.post(f"/painel/api/redigir/{rid}/recusar", json={}, headers=h)
        self.assertEqual(self.cli.get(f"/redigir/{rid}", headers=AUTH).json(), {"status": "recusado", "texto": ""})

    def test_exige_csrf(self):
        rid = self._pendente()
        self.entrar()
        self.assertEqual(self.cli.post(f"/painel/api/redigir/{rid}/liberar", json={"texto": "x"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
