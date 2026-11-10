"""
Testes de _shared/lib_ferramentas.py (tool calling nativo do Ollama): schema gerado da assinatura, conversão de
argumentos, erro e timeout virando texto, marcação de perigosas e cada ferramenta.

    cd services/inbox
    venv/bin/python -m unittest tests.test_ferramentas -v

Offline: vault temporário (pendências, tasks.yml, agenda.yml, briefings, jornal, o índice FTS5 e o histórico do
WhatsApp de verdade); HTTP (Shell Executor, Researcher, SearXNG, páginas da web — httpx.MockTransport e DNS falso),
Ollama da agenda, caixa de e-mail (core.coletar), subprocess do briefing, envio de WhatsApp, limpeza, vetores do
lib_rag e lib_memoria mockados.
"""

import functools
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from unittest import mock

import httpx
import yaml

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

import agenda      # noqa: E402
import core        # noqa: E402
import whatsapp    # noqa: E402
import limpeza     # noqa: E402
from email.message import EmailMessage  # noqa: E402
from _shared import lib_alfred, lib_chromadb, lib_db, lib_mail, lib_memoria, lib_rag, lib_web  # noqa: E402
from _shared import lib_ferramentas as lf  # noqa: E402
from _shared import lib_wordcraft  # noqa: E402,F401  (registra as ferramentas documento_*)

TOKEN = "token-de-teste"
PERIGOSAS = {"executar_comando", "responder_pendencia", "responder_limpeza", "salvar_nota", "completar_tarefa",
             "criar_compromisso", "documento_substituir"}


def resposta(status: int = 200, dados=None) -> httpx.Response:
    return httpx.Response(status, json=dados if dados is not None else {})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-ferramentas-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "alfred"
        self.pedro = self.tmp / "pedro"
        self.vault.mkdir()
        self.pedro.mkdir()
        env = {
            "VAULT_ALFRED": str(self.vault), "VAULT_PEDRO": str(self.pedro), "VAULT_NOTICIAS": "",
            "ALFRED_DB": str(self.tmp / "alfred.db"), "MEMORIA_DB": str(self.tmp / "memoria.db"),
            "SHELL_EXECUTOR_TOKEN": TOKEN, "PORT_SHELL_EXECUTOR": "7170", "PORT_RESEARCHER": "7171",
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",   # nada deve chegar ao Ollama
            "CHROMADB_URL": "http://127.0.0.1:9",      # nem ao ChromaDB
        }
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.dict(lib_chromadb._collection_cache, clear=True),
                  mock.patch.object(core, "_notify", lambda *a, **k: None),
                  mock.patch.object(core, "_iniciar_aguardador", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def registrar(self, nome: str, **kw):
        """Ferramenta temporária só deste teste."""
        def dec(funcao):
            lf.ferramenta(nome, kw.pop("descricao", "teste"), **kw)(funcao)
            self.addCleanup(lf._REGISTRO.pop, nome, None)
            return funcao
        return dec

    def tasks(self) -> list[dict]:
        return yaml.safe_load((self.vault / "tasks.yml").read_text(encoding="utf-8"))["tasks"]

    def gravar_tasks(self, tarefas: list[dict]):
        (self.vault / "tasks.yml").write_text(yaml.dump({"tasks": tarefas}, allow_unicode=True), encoding="utf-8")


# ══════════════════════════════════════════════════════════════
#  Registro: schema, conversão, erro, timeout, perigosas
# ══════════════════════════════════════════════════════════════

class TestRegistro(Base):
    def test_schema_da_assinatura_e_docstring(self):
        @self.registrar("_teste_schema", descricao="Faz um teste.")
        def f(texto: str, n: int, ligado: bool = False, fator: float = 1.5, talvez: Optional[str] = None,
              modo: Literal["a", "b"] = "a", vazio: str = ""):
            """
            Corpo da docstring não vai para o schema.

            :param texto: O texto
                em duas linhas
            :param n: Quantidade
            :param talvez: Opcional
            :return: nada
            """
        esq = next(e for e in lf.esquemas() if e["function"]["name"] == "_teste_schema")
        self.assertEqual(esq["type"], "function")
        self.assertEqual(esq["function"]["description"], "Faz um teste.")
        par = esq["function"]["parameters"]
        self.assertEqual(par["type"], "object")
        self.assertEqual(par["required"], ["texto", "n"])
        p = par["properties"]
        self.assertEqual(p["texto"], {"type": "string", "description": "O texto em duas linhas"})
        self.assertEqual(p["n"], {"type": "integer", "description": "Quantidade"})
        self.assertEqual(p["ligado"], {"type": "boolean", "description": "(padrão: false)"})
        self.assertEqual(p["fator"]["type"], "number")
        self.assertIn("1.5", p["fator"]["description"])
        self.assertEqual(p["talvez"], {"type": "string", "description": "Opcional"})   # Optional → tipo de dentro
        self.assertEqual(p["modo"], {"type": "string", "enum": ["a", "b"], "description": '(padrão: "a")'})
        self.assertEqual(p["vazio"], {"type": "string"})   # padrão vazio não polui a descrição

    def test_tipo_nao_suportado_e_nome_repetido_falham_na_importacao(self):
        with self.assertRaises(TypeError):
            lf.ferramenta("_teste_lista", "x")(lambda itens: None)   # sem anotação
        self.assertNotIn("_teste_lista", lf._REGISTRO)
        with self.assertRaises(ValueError):
            lf.ferramenta("listar_pendencias", "de novo")(lambda: None)

    def test_esquemas_de_todas_as_ferramentas(self):
        esqs = lf.esquemas()
        nomes = [e["function"]["name"] for e in esqs]
        self.assertEqual(len(nomes), len(set(nomes)))
        for antiga in ("listar_pendencias", "executar_comando", "pesquisar", "buscar_cofre", "lembrar",
                       "adicionar_tarefa", "gerar_briefing", "resumo_do_dia", "relatorio_sistema", "salvar_nota"):
            self.assertIn(antiga, nomes)
        for e in esqs:
            fn = e["function"]
            self.assertTrue(fn["description"].strip(), fn["name"])
            par = fn["parameters"]
            self.assertEqual(par["type"], "object")
            self.assertTrue(set(par["required"]) <= set(par["properties"]), fn["name"])
            for nome, prop in par["properties"].items():
                self.assertIn(prop["type"], ("string", "integer", "number", "boolean"), (fn["name"], nome))
        json.dumps(esqs)   # serializável para o /api/chat
        esqs[0]["function"]["parameters"]["properties"].clear()   # cópia: mexer nela não muda o registro
        self.assertTrue(lf.esquemas()[0]["function"]["parameters"]["properties"])

    def test_marcacao_de_perigosas(self):
        self.assertEqual({n for n in lf._REGISTRO if lf.perigosa(n)}, PERIGOSAS)
        self.assertFalse(lf.perigosa("nao_existe"))
        self.assertFalse(lf.perigosa("adicionar_tarefa"))

    def test_conversao_de_argumentos(self):
        recebidos = {}

        @self.registrar("_teste_args")
        def f(n: int, ligado: bool = False, fator: float = 1.0, nome: str = "x", modo: Literal["a", "b"] = "a"):
            recebidos.update(n=n, ligado=ligado, fator=fator, nome=nome, modo=modo)
            return "ok"

        self.assertEqual(lf.executar("_teste_args", {"n": "5", "ligado": "true", "fator": "2", "nome": 7,
                                                     "modo": "b", "extra": "ignorado"}), "ok")
        self.assertEqual(recebidos, {"n": 5, "ligado": True, "fator": 2.0, "nome": "7", "modo": "b"})
        lf.executar("_teste_args", '{"n": 3.0, "ligado": null}')   # texto JSON; null = padrão
        self.assertEqual((recebidos["n"], recebidos["ligado"]), (3, False))

        for args, trecho in (({}, "falta o argumento obrigatório 'n'"), ({"n": "três"}, "'n' deveria ser integer"),
                             ({"n": True}, "'n' deveria ser integer"), ({"n": 1, "ligado": "talvez"}, "'ligado'"),
                             ({"n": 1, "modo": "c"}, "'modo' deve ser um destes: a, b"), ("[1]", "objeto JSON"),
                             ("{quebrado", "argumentos inválidos")):
            r = lf.executar("_teste_args", args)
            self.assertTrue(r.startswith("[erro] argumentos inválidos para _teste_args"), r)
            self.assertIn(trecho, r)

    def test_erro_vira_texto_curto(self):
        @self.registrar("_teste_erro")
        def f():
            raise RuntimeError("deu ruim " + "x" * 1000)

        @self.registrar("_teste_keyerror")
        def g():
            raise KeyError("Pendência não encontrada: abc")

        r = lf.executar("_teste_erro", {})
        self.assertTrue(r.startswith("[erro] _teste_erro: deu ruim"))
        self.assertLess(len(r), lf.MAX_ERRO + 40)
        self.assertEqual(lf.executar("_teste_keyerror"), "[erro] _teste_keyerror: Pendência não encontrada: abc")
        self.assertEqual(lf.executar("nao_existe", {}), "[erro] ferramenta desconhecida: 'nao_existe'")

    def test_timeout(self):
        solta = threading.Event()
        self.addCleanup(solta.set)

        @self.registrar("_teste_lenta", timeout=0.2)
        def lenta():
            solta.wait(5)
            return "tarde demais"

        @self.registrar("_teste_lenta_perigosa", perigosa=True, timeout=0.2)
        def lenta_perigosa():
            solta.wait(5)

        r = lf.executar("_teste_lenta")
        self.assertEqual(r, "[erro] _teste_lenta não respondeu em 0.2 s.")
        r = lf.executar("_teste_lenta_perigosa")
        self.assertIn("não respondeu em 0.2 s", r)
        self.assertIn("Pode ter terminado depois", r)

    def test_saida_longa_e_cortada(self):
        @self.registrar("_teste_longa")
        def longa():
            return "a" * (lf.MAX_SAIDA + 50)

        r = lf.executar("_teste_longa")
        self.assertTrue(r.startswith("a" * lf.MAX_SAIDA))
        self.assertIn("cortado: mais 50 caracteres", r)

    def test_sem_vault_vira_erro(self):
        with mock.patch.dict(os.environ, {"VAULT_ALFRED": ""}):
            self.assertEqual(lf.executar("ver_briefings", {}), "[erro] ver_briefings: VAULT_ALFRED não definido no .env")


# ══════════════════════════════════════════════════════════════
#  Sistema e pesquisa (HTTP mockado)
# ══════════════════════════════════════════════════════════════

class TestHttp(Base):
    def setUp(self):
        super().setUp()
        self.pedidos = []
        self.respostas = []
        p = mock.patch.object(lf.httpx, "request", self._request)
        p.start()
        self.addCleanup(p.stop)

    def _request(self, metodo, url, **kw):
        self.pedidos.append((metodo, url, kw))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def test_executar_comando(self):
        self.respostas = [resposta(200, {"stdout": "Filesystem  Size\n/dev/sda 1T\n", "stderr": "", "returncode": 0})]
        self.assertEqual(lf.executar("executar_comando", {"comando": "df -h", "timeout": 5000}),
                         "Filesystem  Size\n/dev/sda 1T")
        metodo, url, kw = self.pedidos[0]
        self.assertEqual((metodo, url), ("POST", "http://127.0.0.1:7170/execute"))
        self.assertEqual(kw["json"], {"command": "df -h", "timeout": 1800})   # teto do executor
        self.assertEqual(kw["headers"]["Authorization"], f"Bearer {TOKEN}")

        self.respostas = [resposta(200, {"stdout": "", "stderr": "nada", "returncode": 2}),
                          resposta(200, {"stdout": "", "stderr": "", "returncode": 0}),
                          resposta(403, {"detail": "Comando não permitido"}),
                          resposta(401, {"detail": "Token inválido"}),
                          resposta(408, {"detail": "Timeout de 15s excedido"}),
                          httpx.ConnectError("recusada")]
        self.assertEqual(lf.executar("executar_comando", {"comando": "ls /x"}), "[stderr rc=2] nada")
        self.assertEqual(lf.executar("executar_comando", {"comando": "true"}), "[comando executado sem saída, rc=0]")
        self.assertIn("fora da whitelist", lf.executar("executar_comando", {"comando": "rm -rf /"}))
        self.assertIn("recusou o token", lf.executar("executar_comando", {"comando": "ls"}))
        self.assertIn("executor respondeu 408: Timeout de 15s excedido", lf.executar("executar_comando", {"comando": "x"}))
        self.assertEqual(lf.executar("executar_comando", {"comando": "ls"}),
                         "[erro] executar_comando: Shell Executor fora do ar (127.0.0.1:7170)")

    def test_monitor(self):
        self.respostas = [resposta(200, {"stdout": s}) for s in ("Model name: Ryzen", "up 3 days", "Mem: 32G",
                                                                 "/dev/sda 1T", "RTX 3060, 1816 MiB")]
        self.respostas.append(httpx.ConnectError("x"))   # docker: uma seção que falha não derruba o relatório
        r = lf.executar("relatorio_sistema", {})
        for trecho in ("## CPU\nModel name: Ryzen\nup 3 days", "## RAM\nMem: 32G", "## Disco\n/dev/sda 1T",
                       "## GPU\nRTX 3060", "## Docker\n[erro] Shell Executor fora do ar"):
            self.assertIn(trecho, r)

        enviados = len(self.pedidos)
        self.assertIn("Caminho inválido", lf.executar("status_disco", {"caminho": "/ ; rm -rf ~"}))
        self.assertEqual(len(self.pedidos), enviados)   # nada chegou ao executor
        self.respostas = [resposta(200, {"stdout": "/dev/nvme 2T"}), resposta(200, {"stdout": "GPU 0"})]
        self.assertEqual(lf.executar("status_disco", {"caminho": "/home"}), "/dev/nvme 2T")
        self.assertEqual(self.pedidos[-1][2]["json"]["command"], "df -h /home")
        self.assertEqual(lf.executar("status_gpu", {}), "GPU 0")

    def test_pesquisar(self):
        self.respostas = [resposta(200, {"status": "success", "topic": "RAG", "slug": "rag", "vault_path": "/v/research/rag",
                                         "cached": False, "queries_used": ["rag local", "rag sqlite"],
                                         "sources_found": 12})]
        r = lf.executar("pesquisar", {"tema": "RAG"})
        self.assertIn("Fontes: 12", r)
        self.assertIn('ler_nota("research/rag/synthesis.md", "alfred")', r)
        metodo, url, kw = self.pedidos[0]
        self.assertEqual((metodo, url), ("POST", "http://127.0.0.1:7171/research"))
        self.assertEqual(kw["json"]["profundo"], False)

        self.respostas = [resposta(200, {"topic": "RAG", "cached": True, "cache_type": "semantic",
                                         "matched_topic": "RAG local", "matched_slug": "rag-local",
                                         "similarity": 0.91, "vault_path": "/v", "synthesis": "Síntese X"})]
        r = lf.executar("pesquisar", {"tema": "RAG"})
        self.assertIn('semântico: tema parecido "RAG local"', r)
        self.assertIn("Síntese X", r)

        self.respostas = [resposta(202, {"status": "running", "profundo": True, "job_id": "3f9c0a1b2d4e",
                                         "topic": "RAG"})]
        r = lf.executar("pesquisar", {"tema": "RAG", "profundo": True})
        self.assertIn('andamento_pesquisa("3f9c0a1b2d4e")', r)
        self.assertEqual(self.pedidos[-1][2]["json"]["profundo"], True)

        self.respostas = [httpx.ReadTimeout("lento")]
        self.assertEqual(lf.executar("pesquisar", {"tema": "RAG"}),
                         "[erro] pesquisar: Researcher não respondeu em 300 s")

    def test_andamento_pesquisa(self):
        self.respostas = [
            resposta(200, {"status": "running", "etapa": "leitura", "mensagem": "lendo 3 páginas",
                           "subperguntas": ["a", "b"], "notas": [{}], "contagem": {"lidas": 2, "tentadas": 3}}),
            resposta(200, {"status": "done", "topic": "RAG", "resultado": {"vault_path": "/v/rag",
                                                                          "sources_found": 7, "synthesis": "Relatório"}}),
            resposta(200, {"status": "error", "erro": "SearXNG fora do ar"}),
            resposta(404, {"detail": "Pesquisa não encontrada"}),
        ]
        r = lf.executar("andamento_pesquisa", {"job_id": "3f9c0a1b2d4e"})
        self.assertIn("etapa leitura: lendo 3 páginas", r)
        self.assertIn("2 subpergunta(s), 2 de 3 página(s) lidas, 1 nota(s)", r)
        self.assertEqual(self.pedidos[0][:2], ("GET", "http://127.0.0.1:7171/research/3f9c0a1b2d4e"))
        self.assertIn("Relatório", lf.executar("andamento_pesquisa", {"job_id": "3f9c0a1b2d4e"}))
        self.assertIn("falhou: SearXNG fora do ar", lf.executar("andamento_pesquisa", {"job_id": "3f9c0a1b2d4e"}))
        self.assertEqual(lf.executar("andamento_pesquisa", {"job_id": "000000000000"}),
                         "Pesquisa não encontrada: 000000000000")


# ══════════════════════════════════════════════════════════════
#  Briefing, jornal e resumo do dia (vault temporário)
# ══════════════════════════════════════════════════════════════

class TestBriefingResumo(Base):
    def _briefing(self, nome: str, titulo: str) -> Path:
        d = self.vault / "briefings"
        d.mkdir(exist_ok=True)
        f = d / nome
        f.write_text(f'---\ntitle: "{titulo}"\n---\n\n# {titulo}\n\nCorpo do briefing.\n', encoding="utf-8")
        return f

    def test_gerar_briefing(self):
        md = self._briefing("2026-10-31_0700_ia.md", "IA hoje")
        ok = mock.Mock(returncode=0, stdout=f"[briefing] coletando\nBRIEFING_FILE={md}\n", stderr="")
        with mock.patch.object(lf.subprocess, "run", return_value=ok) as run:
            r = lf.executar("gerar_briefing", {})
        self.assertTrue(run.call_args[0][0][0].endswith("services/briefing/run.sh"))
        self.assertIn("Tech Briefing gerado", r)
        self.assertIn("Corpo do briefing.", r)
        self.assertNotIn("title:", r)

        falha = mock.Mock(returncode=1, stdout="Ollama fora do ar\n", stderr="")
        with mock.patch.object(lf.subprocess, "run", return_value=falha):
            r = lf.executar("gerar_briefing", {})
        self.assertIn("Falha ao gerar o briefing", r)
        self.assertIn("Ollama fora do ar", r)

        desligado = mock.Mock(returncode=0, stdout="[alfred] desligado (modo jogo)\n", stderr="")
        with mock.patch.object(lf.subprocess, "run", return_value=desligado):
            self.assertIn("não gerou arquivo", lf.executar("gerar_briefing", {}))

    def test_ver_e_ler_briefing(self):
        self.assertIn("Nenhum briefing", lf.executar("ver_briefings", {}))
        self._briefing("2026-10-30_0700_a.md", "Velho")
        novo = self._briefing("2026-10-31_0700_b.md", "Novo")
        novo.with_suffix(".mp3").write_bytes(b"ID3")
        os.utime(novo, (datetime.now().timestamp() + 5,) * 2)
        r = lf.executar("ver_briefings", {"quantidade": 1})
        self.assertIn("**Novo** (com áudio)", r)
        self.assertNotIn("Velho", r)
        self.assertIn("Corpo do briefing.", lf.executar("ler_briefing", {"arquivo": "2026-10-31_0700_b"}))
        self.assertIn("não encontrado", lf.executar("ler_briefing", {"arquivo": "nada.md"}))
        self.assertIn("caminho fora do cofre", lf.executar("ler_briefing", {"arquivo": "../tasks.yml"}))

    def test_jornal_de_hoje(self):
        self.assertEqual(lf.executar("jornal_de_hoje", {}), "Nenhuma edição do jornal foi gerada ainda.")
        d = self.vault / "jornal"
        d.mkdir()
        (d / "2026-10-31.md").write_text("---\ntitle: Jornal\n---\n\n# Edição de hoje\n", encoding="utf-8")
        (d / "2026-10-31.mp3").write_bytes(b"ID3")
        r = lf.executar("jornal_de_hoje", {})
        self.assertTrue(r.startswith("# Edição de hoje"))
        self.assertIn("Áudio disponível: 2026-10-31.mp3", r)

    def test_resumo_do_dia(self):
        self.assertIn("Nada registrado hoje", lf.executar("resumo_do_dia", {}))
        hoje = date.today()
        self._briefing("2026-10-31_0700_x.md", "Briefing do dia")
        (self.vault / "research" / "rag").mkdir(parents=True)
        (self.vault / "research" / "rag" / "synthesis.md").write_text("---\ntitle: RAG local\n---\n", encoding="utf-8")
        self.gravar_tasks([
            {"id": 1, "title": "Feita", "status": "done", "completed_at": f"{hoje}T10:00"},
            {"id": 2, "title": "Atrasada", "status": "open", "due_date": str(hoje - timedelta(days=2))},
            {"id": 3, "title": "Logo", "status": "open", "due_date": str(hoje + timedelta(days=3))},
            {"id": 4, "title": "Torta", "status": "open", "due_date": "2026-13-45"},
        ])
        fatos = [{"sujeito": "Pedro", "fato": "Prova de BD", "validade": str(hoje + timedelta(days=2))},
                 {"sujeito": "Pedro", "fato": "É vegetariano", "validade": "permanente"}]
        with mock.patch.object(lib_memoria, "listar", return_value=fatos):
            r = lf.executar("resumo_do_dia", {"dias": "1"})
        for trecho in ("Briefing do dia", "RAG local", "✓ #1 Feita", "⚠️ #2 Atrasada", "→ #3 Logo",
                       "? #4 Torta", "[Pedro] Prova de BD"):
            self.assertIn(trecho, r)
        self.assertNotIn("vegetariano", r)


# ══════════════════════════════════════════════════════════════
#  Pendências e limpeza
# ══════════════════════════════════════════════════════════════

class TestPendencias(Base):
    def setUp(self):
        super().setUp()
        pend = [
            {"id": "20261031-meetup", "tipo": "evento", "status": "aguardando", "titulo": "Meetup Python",
             "evento": {"resumo": "Meetup em Bauru", "local": "Bauru"},
             "arquivos": {"md": "pendencias/20261031-meetup.md"}},
            {"id": "20261031-carlos", "tipo": "whatsapp", "status": "aguardando", "titulo": "Carlos",
             "whatsapp": {"contato": "Carlos", "tipo_msg": "texto", "categoria": "profissional",
                          "urgencia": "alta", "texto": "Manda o orçamento?"}},
            {"id": "20261030-velha", "tipo": "evento", "status": "vou", "titulo": "Velha"},
        ]
        d = self.vault / "pendencias"
        d.mkdir()
        (d / "pendencias.yml").write_text(yaml.dump({"pendencias": pend}, allow_unicode=True), encoding="utf-8")
        (d / "20261031-meetup.md").write_text("---\nstatus: aguardando\n---\n\n# Meetup Python\n\nVale ir.\n",
                                              encoding="utf-8")

    def test_listar_pendencias(self):
        r = lf.executar("listar_pendencias", {})
        self.assertIn("2 pendência(s)", r)
        self.assertIn("[20261031-meetup] (evento, aguardando) **Meetup Python** — data não informada · Bauru", r)
        self.assertIn("[20261031-carlos] (whatsapp, aguardando) **Carlos**", r)
        self.assertIn("❝ Manda o orçamento? ❞", r)
        self.assertNotIn("Velha", r)
        self.assertIn("Velha", lf.executar("listar_pendencias", {"status": "todas"}))
        self.assertEqual(lf.executar("listar_pendencias", {"status": "expirada"}),
                         "Nenhuma pendência com status 'expirada'.")

    def test_ver_pendencia(self):
        self.assertEqual(lf.executar("ver_pendencia", {"pendencia_id": "20261031-meetup"}),
                         "# Meetup Python\n\nVale ir.\n")
        self.assertIn("titulo: Carlos", lf.executar("ver_pendencia", {"pendencia_id": "20261031-carlos"}))
        self.assertIn("não encontrada", lf.executar("ver_pendencia", {"pendencia_id": "x"}))

    def test_responder_email_vou_cria_tarefa_no_tasks_compartilhado(self):
        self.gravar_tasks([{"id": 7, "title": "Antiga", "status": "open"}])
        r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-meetup", "resposta": "vou",
                                                "nota": "só à tarde"})
        self.assertEqual(r, 'Anotado: VOU para "Meetup Python". Tarefa #8 criada em tasks.yml.')
        p = core.obter_pendencia("20261031-meetup")
        self.assertEqual((p["status"], p["respondido_via"], p["nota"], p["task_id"]), ("vou", "chat", "só à tarde", 8))
        self.assertEqual(self.tasks()[-1]["title"], "Ir: Meetup Python")
        # de novo: já resolvida, nada muda
        r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-meetup", "resposta": "nao_vou"})
        self.assertIn("já está com status 'vou'", r)
        self.assertEqual(len(self.tasks()), 2)

    def test_responder_email_invalido_e_inexistente(self):
        r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-meetup", "resposta": "talvez"})
        self.assertTrue(r.startswith("[erro] responder_pendencia: Resposta inválida"), r)
        self.assertEqual(core.obter_pendencia("20261031-meetup")["status"], "aguardando")
        self.assertEqual(lf.executar("responder_pendencia", {"pendencia_id": "x", "resposta": "vou"}),
                         "Pendência não encontrada: x")

    def test_responder_whatsapp(self):
        def falso(pid, texto, enviar, origem, nota):
            self.assertEqual((pid, enviar, origem), ("20261031-carlos", True, "via chat"))
            return {"id": pid, "status": "respondida", "titulo": "Carlos",
                    "whatsapp": {"contato": "Carlos", "envio": {"ok": True, "simulado": False}}}

        with mock.patch.object(whatsapp, "responder_texto", side_effect=falso):
            r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-carlos", "resposta": "Mando amanhã"})
        self.assertEqual(r, "Enviado a Carlos pelo WhatsApp:\n“Mando amanhã”")

        falhou = {"status": "aguardando", "whatsapp": {"contato": "Carlos", "envio": {"ok": False, "erro": "HTTP 500"}}}
        with mock.patch.object(whatsapp, "responder_texto", return_value=falhou):
            r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-carlos", "resposta": "Oi"})
        self.assertIn("falhou: HTTP 500", r)

        resolvida = core.PendenciaJaResolvida({"id": "20261031-carlos", "status": "respondida",
                                               "respondido_via": "painel"})
        with mock.patch.object(whatsapp, "responder_texto", side_effect=resolvida):
            r = lf.executar("responder_pendencia", {"pendencia_id": "20261031-carlos", "resposta": "Oi"})
        self.assertIn("resolvida por Painel do Alfred", r)

    def test_limpeza(self):
        lote = {"id": "limpeza-20261031-2210", "total": 16, "status": "aguardando",
                "itens": [{"date": "2026-09-01T10:00", "from": "promo@loja.com", "subject": f"Oferta {i}",
                           "motivo": "promocao há 60 dias"} for i in range(16)]}
        with mock.patch.object(limpeza, "listar_lotes", return_value=[lote]) as listar:
            r = lf.executar("listar_limpezas", {})
        listar.assert_called_once_with("aguardando")
        self.assertIn("[limpeza-20261031-2210] 16 e-mail(s) → Lixeira", r)
        self.assertIn("… e mais 1", r)
        with mock.patch.object(limpeza, "listar_lotes", return_value=[]):
            self.assertEqual(lf.executar("listar_limpezas", {}), "Nenhum pedido de limpeza aguardando.")

        with mock.patch.object(limpeza, "aprovar", return_value={**lote, "total": 15,
                                                                 "removidos_na_aprovacao": [{}]}) as aprovar:
            r = lf.executar("responder_limpeza", {"lote_id": lote["id"], "decisao": "aprovar"})
        aprovar.assert_called_once_with(lote["id"], "via chat")
        self.assertIn("aprovada: 15 e-mail(s)", r)
        self.assertIn("1 saíram do lote", r)
        with mock.patch.object(limpeza, "recusar") as recusar:
            r = lf.executar("responder_limpeza", {"lote_id": lote["id"], "decisao": "recusar", "nota": "não"})
        recusar.assert_called_once_with(lote["id"], "não")
        self.assertIn("recusada", r)
        with mock.patch.object(limpeza, "aprovar", side_effect=ValueError("limpeza-x está 'concluido'")):
            r = lf.executar("responder_limpeza", {"lote_id": "limpeza-x", "decisao": "aprovar"})
        self.assertEqual(r, "[erro] responder_limpeza: limpeza-x está 'concluido'")
        self.assertIn("deve ser um destes: aprovar, recusar",
                      lf.executar("responder_limpeza", {"lote_id": "x", "decisao": "apagar tudo"}))


# ══════════════════════════════════════════════════════════════
#  Tarefas (tasks.yml de verdade, no vault temporário)
# ══════════════════════════════════════════════════════════════

class TestTarefas(Base):
    def test_adicionar_tarefa(self):
        r = lf.executar("adicionar_tarefa", {"titulo": "Estudar BD", "vencimento": "5/11/2026",
                                             "tags": "fatec, estudo", "prioridade": "high"})
        self.assertEqual(r, 'Tarefa #1 criada: "Estudar BD" (vence em 2026-11-05)')
        t = self.tasks()[0]
        self.assertEqual((t["status"], t["priority"], t["tags"], t["due_date"]),
                         ("open", "high", ["fatec", "estudo"], "2026-11-05"))
        self.assertTrue((self.vault / ".tasks.yml.lock").exists())   # mesma trava do core
        self.assertIn("Tarefa #2", lf.executar("adicionar_tarefa", {"titulo": "Sem data"}))
        self.assertEqual(self.tasks()[1]["priority"], "normal")
        self.assertIn("Data de vencimento inválida", lf.executar("adicionar_tarefa", {"titulo": "x",
                                                                                     "vencimento": "sexta"}))
        self.assertEqual(len(self.tasks()), 2)

    def test_listar_completar_e_vencidas(self):
        ontem = date.today() - timedelta(days=1)
        self.gravar_tasks([
            {"id": 1, "title": "Atrasada", "status": "open", "priority": "high", "due_date": ontem},   # date do YAML
            {"id": 2, "title": "Sem data", "status": "open", "tags": ["casa"]},
            {"id": 3, "title": "Feita", "status": "done"},
            {"id": 4, "title": "Cancelada", "status": "cancelled"},
        ])
        r = lf.executar("listar_tarefas", {})
        self.assertIn(f"○ [!] #1 Atrasada · vence {ontem} ⚠️ VENCIDA", r)
        self.assertIn("○ [ ] #2 Sem data [casa]", r)
        self.assertNotIn("Feita", r)
        self.assertIn("✗ [ ] #4 Cancelada", lf.executar("listar_tarefas", {"filtro": "canceladas"}))
        self.assertIn("(4)", lf.executar("listar_tarefas", {"filtro": "todas"}))

        r = lf.executar("tarefas_vencidas", {})
        self.assertIn('⚠️ #1 "Atrasada" — venceu em', r)
        self.assertIn("1 dia(s) atrás", r)

        self.assertEqual(lf.executar("completar_tarefa", {"tarefa_id": "1"}), 'Tarefa #1 concluída: "Atrasada"')
        t = self.tasks()[0]
        self.assertEqual(t["status"], "done")
        self.assertTrue(t["completed_at"].startswith(date.today().isoformat()))
        self.assertEqual(lf.executar("completar_tarefa", {"tarefa_id": 1}), "Tarefa #1 já estava concluída.")
        self.assertEqual(lf.executar("completar_tarefa", {"tarefa_id": 99}), "Tarefa #99 não encontrada.")
        self.assertEqual(lf.executar("tarefas_vencidas", {}), "Nenhuma tarefa vencida.")

    def test_tasks_ilegivel_nao_e_sobrescrito(self):
        f = self.vault / "tasks.yml"
        f.write_text("tasks: [\n  - quebrado", encoding="utf-8")
        r = lf.executar("completar_tarefa", {"tarefa_id": 1})
        self.assertTrue(r.startswith("[erro] completar_tarefa: tasks.yml com erro de YAML"), r)
        self.assertEqual(f.read_text(encoding="utf-8"), "tasks: [\n  - quebrado")
        self.assertIn("erro de YAML", lf.executar("listar_tarefas", {}))


# ══════════════════════════════════════════════════════════════
#  Memória (lib_memoria mockada)
# ══════════════════════════════════════════════════════════════

class TestMemoria(Base):
    def test_lembrar(self):
        fatos = [{"fato": "Prefere reuniões depois das 14h", "sujeito": "Pedro", "acao": "novo"},
                 {"fato": "É vegetariano", "sujeito": "Pedro", "acao": "descartado"}]
        with mock.patch.object(lib_memoria, "extrair", return_value=fatos) as extrair:
            r = lf.executar("lembrar", {"texto": "Prefiro reuniões depois das 14h e sou vegetariano"})
        extrair.assert_called_once_with("Prefiro reuniões depois das 14h e sou vegetariano", "chat", None)
        self.assertIn("• [Pedro] Prefere reuniões depois das 14h (novo)", r)
        self.assertIn("(já sabia)", r)
        with mock.patch.object(lib_memoria, "extrair", return_value=[]) as extrair:
            r = lf.executar("lembrar", {"texto": "oi", "sujeito": "Carlos"})
        extrair.assert_called_once_with("oi", "chat", "Carlos")
        self.assertTrue(r.startswith("Nada guardado"))

    def test_buscar_e_listar_memoria(self):
        with mock.patch.object(lib_memoria, "contexto", return_value="Fatos que Alfred já sabe:\n- [Pedro] x") as ctx:
            self.assertIn("[Pedro] x", lf.executar("buscar_memoria", {"consulta": "Pedro", "quantidade": 3}))
        ctx.assert_called_once_with("Pedro", 3)
        with mock.patch.object(lib_memoria, "contexto", return_value=""):
            self.assertEqual(lf.executar("buscar_memoria", {"consulta": "nada"}), "Nada na memória sobre 'nada'.")
        fatos = [{"id": 1, "fato": "É vegetariano", "sujeito": "Pedro", "tipo": "preferencia", "validade": "permanente"},
                 {"id": 2, "fato": "Prova de BD", "sujeito": "Pedro", "tipo": "rotina", "validade": "2026-11-05"}]
        with mock.patch.object(lib_memoria, "listar", return_value=fatos) as listar:
            r = lf.executar("listar_memoria", {"sujeito": "Pedro"})
        listar.assert_called_once_with("Pedro")
        self.assertIn("• #1 [Pedro] É vegetariano (preferencia)", r)
        self.assertIn("• #2 [Pedro] Prova de BD (rotina, até 2026-11-05)", r)
        with mock.patch.object(lib_memoria, "listar", return_value=[]):
            self.assertEqual(lf.executar("listar_memoria", {}), "Memória vazia.")


# ══════════════════════════════════════════════════════════════
#  Cofres (índice FTS5 de verdade num banco temporário)
# ══════════════════════════════════════════════════════════════

class TestCofres(Base):
    def test_buscar_cofre_pelo_indice(self):
        (self.pedro / "projetos").mkdir()
        (self.pedro / "projetos" / "servidor.md").write_text("# Servidor\n\nOrçamento do servidor da Fatec.\n",
                                                             encoding="utf-8")
        (self.vault / "decisions").mkdir()
        (self.vault / "decisions" / "quic.md").write_text("# QUIC\n\nDecidimos usar QUIC no servidor.\n",
                                                          encoding="utf-8")
        lib_db.indexar_cofre(self.pedro, "pedro")
        lib_db.indexar_cofre(self.vault, "alfred")

        # ChromaDB fora do ar (CHROMADB_URL na porta 9): lib_rag cai no FTS5 sozinho
        r = lf.executar("buscar_cofre", {"consulta": "servidor"})
        self.assertIn("2 resultado(s)", r)
        self.assertIn("cite a fonte como [n]", r)
        self.assertIn("] projetos/servidor.md (cofre pedro)\n", r)
        self.assertIn("] decisions/quic.md (cofre alfred)\n", r)
        r = lf.executar("buscar_cofre", {"consulta": "servidor", "cofre": "alfred"})
        self.assertIn("1 resultado(s)", r)
        self.assertIn("[1] decisions/quic.md (cofre alfred)", r)
        self.assertNotIn("cofre pedro", r)
        self.assertIn("Nenhuma nota", lf.executar("buscar_cofre", {"consulta": "inexistentequalquer"}))

    def test_buscar_cofre_passa_por_buscar(self):
        achados = [{"cofre": "pedro", "caminho": "a.md", "titulo": "a", "trecho": "trecho\n curto", "pontuacao": 0.03,
                    "origem": "ambos"},
                   {"cofre": "alfred", "caminho": "research/x/synthesis.md", "titulo": "x", "trecho": "y" * 1200,
                    "pontuacao": 0.01, "origem": "vetor"}]
        with mock.patch.object(lf, "_buscar", return_value=achados) as buscar:
            r = lf.executar("buscar_cofre", {"consulta": "rag", "limite": 99})
        buscar.assert_called_once_with("rag", None, 20)
        self.assertIn("[1] a.md (cofre pedro)\ntrecho curto", r)
        self.assertIn("[2] research/x/synthesis.md (cofre alfred)\n" + "y" * (lf.TRECHO_MAX - 1) + "…", r)
        self.assertNotIn("y" * lf.TRECHO_MAX, r)   # pedaço vetorial (até 1200) cortado

    def test_buscar_cofre_usa_busca_hibrida_do_lib_rag(self):
        (self.pedro / "carros.md").write_text("# Carros\n\nManutenção do carro a cada 10 mil km.\n", encoding="utf-8")
        (self.pedro / "viagem.md").write_text("# Viagem\n\nIr de automóvel até a praia.\n", encoding="utf-8")
        lib_db.indexar_cofre(self.pedro, "pedro")
        vetor = [{"cofre": "pedro", "caminho": "viagem.md", "titulo": "viagem", "trecho": "Ir de automóvel até a praia."},
                 {"cofre": "pedro", "caminho": "carros.md", "titulo": "carros", "trecho": "Manutenção do carro."}]
        with mock.patch.object(lib_rag, "_buscar_vetores", return_value=vetor) as vetores:
            r = lf.executar("buscar_cofre", {"consulta": "carro", "cofre": "pedro"})
        vetores.assert_called_once_with("carro", "pedro", "", 10)
        self.assertIn("2 resultado(s)", r)
        self.assertIn("[1] carros.md (cofre pedro)", r)    # FTS + vetor ("ambos") vem primeiro
        self.assertIn("[2] viagem.md (cofre pedro)", r)    # só o vetor acha "automóvel" para "carro"

    def test_ler_nota(self):
        (self.pedro / "ideias.md").write_text("Ideia boa", encoding="utf-8")
        (self.vault / "log.md").write_text("Log do Alfred", encoding="utf-8")
        self.assertEqual(lf.executar("ler_nota", {"caminho": "ideias.md"}), "Ideia boa")
        self.assertEqual(lf.executar("ler_nota", {"caminho": "log.md", "cofre": "alfred"}), "Log do Alfred")
        self.assertIn("não encontrada no cofre pedro", lf.executar("ler_nota", {"caminho": "log.md"}))
        for fora in ("../alfred/log.md", "/etc/passwd"):
            r = lf.executar("ler_nota", {"caminho": fora})
            self.assertTrue(r.startswith("[erro] ler_nota: caminho fora do cofre"), r)
        (self.pedro / "link").symlink_to(self.vault)
        self.assertIn("caminho fora do cofre", lf.executar("ler_nota", {"caminho": "link/log.md"}))

    def test_salvar_e_listar_cofre(self):
        r = lf.executar("salvar_nota", {"titulo": "Decisão: usar QUIC", "conteudo": "Porque sim.", "pasta": "decisions"})
        hoje = date.today().isoformat()
        self.assertEqual(r, f"Nota salva em: decisions/{hoje} Decisão usar QUIC.md")
        texto = (self.vault / "decisions" / f"{hoje} Decisão usar QUIC.md").read_text(encoding="utf-8")
        meta = yaml.safe_load(texto.split("---")[1])
        self.assertEqual((meta["title"], meta["folder"]), ("Decisão: usar QUIC", "decisions"))
        self.assertTrue(texto.endswith("Porque sim."))
        r = lf.executar("salvar_nota", {"titulo": "Decisão: usar QUIC", "conteudo": "De novo.", "pasta": "decisions"})
        self.assertIn("(2).md", r)   # não sobrescreve
        self.assertEqual(lf.executar("salvar_nota", {"titulo": "x", "conteudo": "  "}), "Conteúdo vazio: nada salvo.")
        self.assertIn("caminho fora do cofre", lf.executar("salvar_nota", {"titulo": "x", "conteudo": "y",
                                                                           "pasta": "../pedro"}))
        self.assertEqual(list(self.pedro.iterdir()), [])

        r = lf.executar("listar_cofre", {"pasta": "decisions"})
        self.assertIn("2 nota(s)", r)
        self.assertIn(f"- decisions/{hoje} Decisão usar QUIC.md", r)
        self.assertIn("não encontrada", lf.executar("listar_cofre", {"pasta": "nada"}))



# ══════════════════════════════════════════════════════════════
#  Agenda (agenda.yml de verdade; Ollama da detecção mockado)
# ══════════════════════════════════════════════════════════════

def deteccao(dia: date, hora: str = "15:00", titulo: str = "Dentista", local: str = "Clínica Sorriso") -> dict:
    """Resposta do Ollama (SCHEMA_COMPROMISSO) para agenda.detectar_compromisso."""
    return {"existe": True, "tipo": "evento", "titulo": titulo, "data": dia.isoformat(), "hora": hora,
            "hora_fim": "", "local": local, "confianca": 0.9, "trecho": "dentista sexta 15h"}


class TestAgenda(Base):
    def em(self, dias: int, hora: int = 15) -> datetime:
        d = date.today() + timedelta(days=dias)
        return datetime(d.year, d.month, d.day, hora, 0)

    def test_ver_agenda(self):
        self.assertEqual(lf.executar("ver_agenda", {}), "Nada na agenda nos próximos 7 dias.")
        agenda.adicionar("Consulta hoje", self.em(0), local="Hospital", notificar=False)
        agenda.adicionar("Prova de cálculo", self.em(3), dia_inteiro=True, tipo="lembrete", notificar=False)
        agenda.adicionar("Viagem longe", self.em(10), notificar=False)
        agenda.adicionar("Ontem", self.em(-1), notificar=False)
        desfeito = agenda.adicionar("Desmarcado", self.em(1), notificar=False)
        agenda.desfazer(desfeito["id"])

        r = lf.executar("ver_agenda", {})
        self.assertIn("Agenda nos próximos 7 dias (2):", r)
        self.assertIn("**Consulta hoje** (evento) · Hospital · a caminho do calendário", r)
        self.assertIn("**Prova de cálculo** (lembrete)", r)
        self.assertIn("dia inteiro", r)
        self.assertLess(r.index("Consulta hoje"), r.index("Prova de cálculo"))   # em ordem de data
        for fora in ("Viagem longe", "Ontem", "Desmarcado"):
            self.assertNotIn(fora, r)
        r = lf.executar("ver_agenda", {"dias": 1})
        self.assertIn("Agenda hoje (1):", r)
        self.assertNotIn("Prova", r)
        self.assertIn("Viagem longe", lf.executar("ver_agenda", {"dias": 11}))

    def test_criar_compromisso_e_perigosa(self):
        self.assertTrue(lf.perigosa("criar_compromisso"))
        self.assertFalse(lf.perigosa("ver_agenda"))
        esq = next(e for e in lf.esquemas() if e["function"]["name"] == "criar_compromisso")
        self.assertEqual(esq["function"]["parameters"]["required"], ["texto"])

    def test_criar_compromisso(self):
        dia = date.today() + timedelta(days=2)
        detectar = mock.Mock(wraps=agenda.detectar_compromisso)
        with mock.patch.object(agenda, "ollama_generate_json", return_value=deteccao(dia)) as llm, \
                mock.patch.object(agenda, "detectar_compromisso", detectar):
            r = lf.executar("criar_compromisso", {"texto": "marca dentista daqui a 2 dias às 15h"})
            self.assertEqual(detectar.call_args.kwargs["para_mim"], True)   # Pedro anotando para ele mesmo
            self.assertIn("o próprio Pedro", llm.call_args_list[0].args[0])   # 1ª chamada: detecção; 2ª: resumo
            self.assertIn(f"Anotado na agenda: Dentista (evento) — {dia:%d/%m}", r)
            self.assertIn("15:00 · Clínica Sorriso", r)
            itens = agenda.listar()
            self.assertEqual(len(itens), 1)
            item = itens[0]
            self.assertEqual((item["titulo"], item["status"], item["origem"]), ("Dentista", "pendente", {"canal": "conversa"}))
            self.assertTrue(item["inicio"].startswith(f"{dia.isoformat()}T15:00"))
            self.assertIn("marca dentista", item["descricao"])
            self.assertIn(f"[id {item['id']}]", r)
            # pedir de novo não duplica (agenda.adicionar é idempotente por título + dia)
            r = lf.executar("criar_compromisso", {"texto": "marca dentista daqui a 2 dias às 15h"})
        self.assertIn("Já estava na agenda", r)
        self.assertEqual(len(agenda.listar()), 1)

    def test_criar_compromisso_sem_data_nao_cria(self):
        sem = {**deteccao(date.today()), "existe": False, "tipo": "nenhum"}
        with mock.patch.object(agenda, "ollama_generate_json", return_value=sem):
            r = lf.executar("criar_compromisso", {"texto": "marca dentista sexta"})
        self.assertIn("Nada foi criado", r)
        # sem cara de compromisso (prefiltro), nem chama o Ollama
        with mock.patch.object(agenda, "ollama_generate_json") as llm:
            self.assertIn("Nada foi criado", lf.executar("criar_compromisso", {"texto": "oi tudo bem"}))
        llm.assert_not_called()
        self.assertEqual(agenda.listar(), [])


# ══════════════════════════════════════════════════════════════
#  WhatsApp (histórico wa_msgs de verdade num banco temporário)
# ══════════════════════════════════════════════════════════════

SILVA, SOUZA, ANA, GRUPO = ("5511999990001@s.whatsapp.net", "5511999990002@s.whatsapp.net",
                            "5511999990003@s.whatsapp.net", "120363000000000001@g.us")


class TestMensagens(Base):
    def setUp(self):
        super().setUp()
        msgs = [
            ("m1", SILVA, "Carlos Silva", "O orçamento do servidor ficou em 8 mil", False),
            ("m2", SILVA, "Carlos Silva", "Fechado, pode comprar o servidor", True),
            ("m3", SOUZA, "Carlos Souza", "Orçamento da festa: 2 mil", False),
            ("m4", ANA, "Ana Júlia", "Meu endereço novo é Rua das Flores, 10", False),
            ("m5", ANA, "Ana Júlia", "Valeu, anotei", True),
            ("m6", ANA, "Ana Júlia", "Amanhã passo aí", False),
            ("m7", GRUPO, "Carlos Silva", "Orçamento no grupo da Fatec", False),
        ]
        for i, (mid, jid, contato, texto, eu) in enumerate(msgs):
            lib_db.registrar_wa(mid, jid, contato, texto, grupo=jid == GRUPO, from_me=eu,
                                ts=f"2026-10-2{i}T10:0{i}:00")

    def test_buscar_mensagens_em_todas_as_conversas(self):
        r = lf.executar("buscar_mensagens", {"consulta": "orçamento"})
        self.assertIn("3 mensagem(ns) com 'orçamento':", r)
        for jid in (SILVA, SOUZA, GRUPO):
            self.assertIn(f"({jid})", r)
        self.assertIn("Carlos Silva", r)
        self.assertIn("Nenhuma mensagem com 'inexistentequalquer'.",
                      lf.executar("buscar_mensagens", {"consulta": "inexistentequalquer"}))

    def test_buscar_mensagens_de_um_contato(self):
        r = lf.executar("buscar_mensagens", {"consulta": "servidor", "contato": "carlos silva"})   # sem caixa
        self.assertIn("na conversa com Carlos Silva", r)
        self.assertIn("Pedro: Fechado, pode comprar o servidor", r)
        self.assertNotIn("festa", r)
        self.assertNotIn("grupo", r)                                  # grupo fica fora da conversa individual
        r = lf.executar("buscar_mensagens", {"consulta": "endereço", "contato": "ana julia"})   # sem acento
        self.assertIn("na conversa com Ana Júlia", r)
        self.assertIn("Rua das Flores", r)
        r = lf.executar("buscar_mensagens", {"consulta": "orçamento", "contato": GRUPO})        # JID direto
        self.assertIn("Orçamento no grupo da Fatec", r)
        self.assertIn("Nenhuma mensagem com 'festa' na conversa com Ana Júlia.",
                      lf.executar("buscar_mensagens", {"consulta": "festa", "contato": "Ana"}))

    def test_contato_ambiguo_ou_desconhecido(self):
        r = lf.executar("buscar_mensagens", {"consulta": "orçamento", "contato": "Carlos"})
        self.assertIn("Mais de uma conversa para 'Carlos'", r)
        self.assertIn(f"Carlos Silva ({SILVA})", r)
        self.assertIn(f"Carlos Souza ({SOUZA})", r)
        self.assertNotIn(GRUPO, r)
        self.assertIn("Nenhuma conversa de WhatsApp com 'Beatriz'",
                      lf.executar("conversa_com", {"contato": "Beatriz"}))

    def test_conversa_com(self):
        r = lf.executar("conversa_com", {"contato": "Ana", "n": 2})
        self.assertTrue(r.startswith(f"Últimas mensagens com Ana Júlia ({ANA}):\n"), r)
        linhas = r.splitlines()[1:]
        self.assertEqual(len(linhas), 2)
        self.assertIn("Pedro: Valeu, anotei", linhas[0])              # em ordem cronológica
        self.assertIn("Ana Júlia: Amanhã passo aí", linhas[1])
        self.assertIn("Rua das Flores", lf.executar("conversa_com", {"contato": "99990003"}))   # pelo número
        self.assertIn("Nenhuma mensagem guardada", lf.executar("conversa_com", {"contato": "5511000@s.whatsapp.net"}))


# ══════════════════════════════════════════════════════════════
#  E-mail (core.coletar mockado com e-mails reais passados pelo lib_mail.parse_message)
# ══════════════════════════════════════════════════════════════

def email_(mid: str, de: str, assunto: str, quando: datetime, texto: str = "", html: str = "", lido: bool = True):
    m = EmailMessage()
    m["Message-ID"], m["From"], m["To"], m["Subject"] = f"<{mid}>", de, "pedro@example.com", assunto
    m["Date"] = quando.astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    m["X-Mozilla-Status"] = "0001" if lido else "0000"
    if html:
        m.set_content(texto or "")
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(texto or "oi")
    return lib_mail.parse_message(m, folder="INBOX", account="imap.example.com")


class TestConversasAnteriores(Base):
    def conversa(self, titulo: str, *mensagens) -> str:
        motor = lf._inbox("conversa")
        cid = motor.nova_conversa(titulo)["id"]
        with closing(motor._conectar()) as con, con:
            for papel, texto, *extra in mensagens:
                motor._gravar_mensagem(con, cid, papel, texto, ferramenta=extra[0] if extra else None)
        return cid

    def test_buscar_conversas(self):
        cid = self.conversa("DHT no TG", ("user", "Como funciona o Kademlia?"), ("assistant", "Por distância XOR, senhor."))
        self.conversa("Bolo", ("user", "Receita de bolo de laranja"))
        r = lf.executar("buscar_conversas", {"consulta": "Kademlia"})
        self.assertTrue(r.startswith("1 conversa(s) sobre 'Kademlia'; leia a íntegra com ler_conversa(id):"), r)
        self.assertRegex(r, rf"\[{cid}\] \d\d/\d\d/\d{{4}} · DHT no TG\n")
        self.assertIn("[Kademlia]", r)
        self.assertNotIn("Bolo", r)
        self.assertFalse(lf.perigosa("buscar_conversas"))

    def test_buscar_conversas_em_linguagem_natural(self):
        cid = self.conversa("DHT", ("user", "Como funciona o Kademlia?"))
        r = lf.executar("buscar_conversas", {"consulta": "o que falamos sobre Kademlia afinal"})
        self.assertIn(cid, r)

    def test_buscar_conversas_nada(self):
        self.assertEqual(lf.executar("buscar_conversas", {"consulta": "zzzzz"}), "Nenhuma conversa anterior sobre 'zzzzz'.")

    def test_ler_conversa(self):
        cid = self.conversa("DHT", ("user", "Como funciona?"), ("tool", "", "buscar_web"), ("assistant", "x" * 2000))
        r = lf.executar("ler_conversa", {"conversa_id": f" {cid} "})
        self.assertTrue(r.startswith('Conversa "DHT":\n['), r)
        self.assertRegex(r, r"\[\d{4}-\d\d-\d\d \d\d:\d\d\] Pedro: Como funciona\?")
        self.assertIn("Alfred: " + "x" * (lf.CONVERSA_MSG_MAX - 1) + "…", r)
        self.assertNotIn("buscar_web", r)
        self.assertFalse(lf.perigosa("ler_conversa"))

    def test_ler_conversa_inexistente(self):
        self.assertIn("Conversa não encontrada: 'x'", lf.executar("ler_conversa", {"conversa_id": "x"}))


class TestEmail(Base):
    def setUp(self):
        super().setUp()
        agora = datetime.now()
        self.emails = [
            email_("a1@x", "Carlos Silva <carlos@fatec.br>", "Reunião do TCC", agora - timedelta(hours=2),
                   texto="Podemos nos ver quinta?", lido=False),
            email_("b2@x", "Loja <promo@loja.com>", "50% OFF", agora - timedelta(hours=5),
                   html="<html><head><style>p{color:red}</style><script>rastreio()</script></head><body>"
                        "<p>Só <b>hoje</b>: 50% OFF em tudo</p>"
                        "<a href='https://loja.com/oferta?utm_source=x'>ver</a></body></html>"),
            email_("c3@x", "Mãe <mae@example.com>", "Almoço domingo", agora - timedelta(hours=30),
                   texto="Vem almoçar domingo?"),
        ]
        registro = self.tmp / "inbox_categorias.json"
        registro.write_text(json.dumps({"a1@x": {"categoria": "contato"}, "b2@x": {"categoria": "promocao"}}),
                            encoding="utf-8")
        self.coletar = mock.Mock(return_value=list(self.emails))
        for p in (mock.patch.object(limpeza, "REGISTRO_FILE", registro),
                  mock.patch.object(core, "coletar", self.coletar)):
            p.start()
            self.addCleanup(p.stop)

    def test_emails_recentes(self):
        r = lf.executar("emails_recentes", {"dias": 1})
        self.assertEqual(self.coletar.call_args.kwargs["janela_horas"], 24)
        self.assertIn("3 e-mail(s) nas últimas 24 h:", r)
        self.assertIn("Carlos Silva <carlos@fatec.br> · Reunião do TCC · contato · não lido · id a1@x", r)
        self.assertIn("Loja <promo@loja.com> · 50% OFF · promocao · id b2@x", r)
        self.assertIn("Mãe <mae@example.com> · Almoço domingo · sem categoria · id c3@x", r)
        self.assertIn("ler_email(message_id)", r)
        lf.executar("emails_recentes", {})
        self.assertEqual(self.coletar.call_args.kwargs["janela_horas"], 48)   # padrão: 2 dias

    def test_emails_recentes_por_categoria(self):
        r = lf.executar("emails_recentes", {"categoria": "Promoção"})       # caixa e acento não importam
        self.assertIn("1 e-mail(s) nos últimos 2 dias na categoria promocao:", r)
        self.assertNotIn("Reunião", r)
        self.assertIn("Nenhum e-mail nos últimos 2 dias na categoria pessoal.",
                      lf.executar("emails_recentes", {"categoria": "pessoal"}))
        self.coletar.reset_mock()
        r = lf.executar("emails_recentes", {"categoria": "urgente"})
        self.assertIn("Categoria desconhecida: 'urgente'", r)
        self.assertIn("promocao", r)
        self.coletar.assert_not_called()

    def test_ler_email_texto_limpo(self):
        r = lf.executar("ler_email", {"message_id": "<b2@x>"})               # aceita com < >
        self.assertIn("De: Loja <promo@loja.com>", r)
        self.assertIn("Assunto: 50% OFF", r)
        self.assertIn("Categoria: promocao", r)
        self.assertIn("Só hoje: 50% OFF em tudo", r)
        for sujeira in ("<p>", "<b>", "rastreio", "color:red"):
            self.assertNotIn(sujeira, r)
        self.assertEqual(self.coletar.call_args.kwargs["janela_horas"], lf.EMAILS_LER_DIAS * 24)
        r = lf.executar("ler_email", {"message_id": "c3@x"})
        self.assertIn("Vem almoçar domingo?", r)
        self.assertNotIn("Categoria:", r)
        self.assertIn("E-mail não encontrado", lf.executar("ler_email", {"message_id": "zz@x"}))

    def test_ler_email_longo_e_cortado(self):
        longo = email_("d4@x", "Newsletter <news@x.com>", "Edição", datetime.now(), texto="palavra " * 2000)
        self.coletar.return_value = [longo]
        r = lf.executar("ler_email", {"message_id": "d4@x"})
        self.assertIn("… (cortado: mais", r)
        self.assertLess(len(r), lf.EMAIL_MAX + 300)


# ══════════════════════════════════════════════════════════════
#  Web: SearXNG e leitura de página (httpx.MockTransport + DNS falso, nada sai da máquina)
# ══════════════════════════════════════════════════════════════

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


def pagina_html(titulo: str, corpo: str) -> bytes:
    return (f"<html><head><title>{titulo}</title><script>var segredo = 'nao-extrair';</script></head><body>"
            f"<h1>{titulo}</h1><p>{corpo}</p></body></html>").encode("utf-8")


class _Transporte:
    """httpx.MockTransport com rotas por URL; guarda o que foi pedido (prova que IP privado nunca é tocado)."""

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


class TestWeb(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(lib_web, "resolver", resolver_falso)
        p.start()
        self.addCleanup(p.stop)

    def usar_transporte(self, rotas) -> _Transporte:
        t = _Transporte(rotas)
        p = mock.patch.object(lib_web, "_transporte_http", t.mock)
        p.start()
        self.addCleanup(p.stop)
        return t

    def test_buscar_web(self):
        achados = [{"title": "Python 3.14", "url": "https://docs.python.org/3.14/", "snippet": "O que há de novo"},
                   {"title": "", "url": "https://exemplo.org/py", "snippet": ""}]
        with mock.patch.object(lf, "searxng_search", return_value=achados) as busca:
            r = lf.executar("buscar_web", {"consulta": "python 3.14", "n": 99})
        busca.assert_called_once_with("python 3.14", categories="general", max_results=10)
        self.assertIn("2 resultado(s) para 'python 3.14'", r)
        self.assertIn("[1] Python 3.14\nhttps://docs.python.org/3.14/\nO que há de novo", r)
        self.assertIn("[2] https://exemplo.org/py\nhttps://exemplo.org/py", r)   # sem título: a URL
        self.assertIn("ler_pagina(url)", r)
        with mock.patch.object(lf, "searxng_search", return_value=[]):
            self.assertIn("Nenhum resultado na web", lf.executar("buscar_web", {"consulta": "x"}))

    def test_buscar_artigos(self):
        achados = [{"title": "The Lottery Ticket Hypothesis", "url": "https://arxiv.org/abs/1803.03635",
                    "url_html": "https://arxiv.org/html/1803.03635", "snippet": "Finds subnetworks.", "source": "OpenAlex",
                    "doi": "10.48550/arxiv.1803.03635", "arxiv_id": "1803.03635", "ano": 2018, "citacoes": 3200,
                    "autores": ["Jonathan Frankle", "Michael Carbin"]},
                   {"title": "Sem metadados", "url": "https://exemplo.org/a", "url_html": "", "snippet": "", "source": "Semantic Scholar",
                    "doi": "", "arxiv_id": "", "ano": None, "citacoes": None, "autores": ["A", "B", "C"]}]
        with mock.patch.object(lf.lib_academico, "buscar_todas", return_value=achados) as busca:
            r = lf.executar("buscar_artigos", {"consulta": "lottery ticket hypothesis", "n": 99})
        busca.assert_called_once_with("lottery ticket hypothesis", limite=10)
        self.assertIn("2 artigo(s) para 'lottery ticket hypothesis'", r)
        self.assertIn("[1] The Lottery Ticket Hypothesis\nJonathan Frankle, Michael Carbin · 2018 · 3200 citações · "
                      "arXiv:1803.03635 · OpenAlex\nhttps://arxiv.org/abs/1803.03635\nFinds subnetworks.", r)
        self.assertIn("[2] Sem metadados\nA, B, C et al. · Semantic Scholar\nhttps://exemplo.org/a", r)
        self.assertIn("ler_pagina(url)", r)
        with mock.patch.object(lf.lib_academico, "buscar_todas", return_value=achados) as busca:
            self.assertIn("1 artigo(s)", lf.executar("buscar_artigos", {"consulta": "x", "n": 1}))   # corta em n
        busca.assert_called_once_with("x", limite=1)
        with mock.patch.object(lf.lib_academico, "buscar_todas", return_value=[]):
            self.assertIn("Nenhum artigo encontrado", lf.executar("buscar_artigos", {"consulta": "x"}))

    def test_buscar_web_vai_ao_searxng_local(self):
        pedidos = []

        def searxng(request):
            pedidos.append(request.url)
            return httpx.Response(200, json={"results": [
                {"title": "Resultado", "url": "https://a.example/", "content": "trecho <b>com</b> tag"},
                {"title": "Sem URL", "url": ""}]})

        cliente = httpx.Client
        with mock.patch.dict(os.environ, {"SEARXNG_URL": "http://127.0.0.1:8899"}), \
                mock.patch.object(lib_alfred.httpx, "Client",
                                  lambda **kw: cliente(transport=httpx.MockTransport(searxng), **kw)):
            r = lf.executar("buscar_web", {"consulta": "rust async"})
        self.assertEqual((pedidos[0].host, pedidos[0].port, pedidos[0].path), ("127.0.0.1", 8899, "/search"))
        self.assertEqual(pedidos[0].params["q"], "rust async")
        self.assertEqual(pedidos[0].params["categories"], "general")
        self.assertIn("1 resultado(s)", r)
        self.assertIn("trecho com tag", r)

    def test_ler_pagina(self):
        self.usar_transporte({"https://docs.example/guia": httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=pagina_html("Guia do Tokio", "Tokio é um runtime assíncrono para Rust."))})
        r = lf.executar("ler_pagina", {"url": " https://docs.example/guia "})
        self.assertTrue(r.startswith("Página: https://docs.example/guia\n\n"), r)
        self.assertIn("Tokio é um runtime assíncrono para Rust.", r)
        self.assertNotIn("nao-extrair", r)
        self.assertNotIn("<p>", r)

    def test_ler_pagina_texto_puro_e_sem_texto(self):
        self.usar_transporte({
            "https://a.example/leia.txt": httpx.Response(200, headers={"content-type": "text/plain; charset=latin-1"},
                                                         content="Olá, café".encode("latin-1")),
            "https://a.example/app": httpx.Response(200, headers={"content-type": "text/html"},
                                                    content=b"<html><body><div id=app></div></body></html>"),
        })
        self.assertIn("Olá, café", lf.executar("ler_pagina", {"url": "https://a.example/leia.txt"}))
        self.assertIn("não tem texto legível", lf.executar("ler_pagina", {"url": "https://a.example/app"}))

    def test_url_privada_nunca_e_pedida(self):
        t = self.usar_transporte({})
        for url in ("http://127.0.0.1:7073/painel", "http://localhost:11434/api/tags", "http://192.168.0.10/",
                    "http://[::1]:8001/", "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/",
                    "http://[::ffff:127.0.0.1]/", "http://interno.example/", "http://misto.example/",
                    "http://naoexiste.example/", "file:///etc/passwd", "ftp://arquivos.example/x", "https:///sem-host"):
            r = lf.executar("ler_pagina", {"url": url})
            self.assertTrue(r.startswith("[erro] ler_pagina: URL recusada"), (url, r))
        self.assertEqual(t.pedidos, [])

    def test_redirect_para_loopback_e_barrado(self):
        t = self.usar_transporte({
            "https://redir.example/": httpx.Response(302, headers={"location": "http://127.0.0.1:7073/painel"}),
            "http://127.0.0.1:7073/painel": httpx.Response(200, headers={"content-type": "text/html"},
                                                           content=pagina_html("Painel", "segredo do painel")),
        })
        r = lf.executar("ler_pagina", {"url": "https://redir.example/"})
        self.assertIn("[erro] ler_pagina: URL recusada", r)
        self.assertIn("127.0.0.1:7073", r)
        self.assertNotIn("segredo", r)
        self.assertEqual(t.pedidos, ["https://redir.example/"])

    def test_redirect_publico_e_seguido(self):
        t = self.usar_transporte({
            "https://a.example/velho": httpx.Response(301, headers={"location": "/novo"}),
            "https://a.example/novo": httpx.Response(200, headers={"content-type": "text/html"},
                                                     content=pagina_html("Novo", "Página nova sobre Rust async.")),
        })
        r = lf.executar("ler_pagina", {"url": "https://a.example/velho"})
        self.assertIn("Página: https://a.example/novo", r)
        self.assertIn("Página nova sobre Rust async.", r)
        self.assertEqual(t.pedidos, ["https://a.example/velho", "https://a.example/novo"])
        self.usar_transporte({"https://loop.example/": httpx.Response(302, headers={"location": "/"})})
        self.assertIn("redirects demais", lf.executar("ler_pagina", {"url": "https://loop.example/"}))

    def test_pagina_grande_e_truncada_sem_ler_tudo(self):
        enviados = [0]

        def sem_fim():
            yield b"<html><body>"
            while True:
                enviados[0] += 1
                if enviados[0] > 2_000:            # ~15 MB: se chegou aqui, o limite não funcionou
                    raise AssertionError("leu a página inteira")
                yield b"<p>" + b"Texto longo sobre Tokio. " * 300 + b"</p>"

        self.usar_transporte({"https://grande.example/": lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=sem_fim())})
        r = lf.executar("ler_pagina", {"url": "https://grande.example/"})
        self.assertIn(f"página grande: só os primeiros {lib_web.MAX_BYTES} bytes foram lidos", r)
        self.assertIn("Texto longo sobre Tokio.", r)
        self.assertIn("… (cortado: mais", r)
        self.assertLess(len(r), lf.PAGINA_MAX + 300)
        self.assertLess(enviados[0] * 7_507, lib_web.MAX_BYTES + 2 * 7_510)   # parou logo depois do limite

    def test_timeout(self):
        def lento(request):
            raise httpx.ReadTimeout("servidor lento", request=request)

        self.usar_transporte({"https://lento.example/": lento})
        r = lf.executar("ler_pagina", {"url": "https://lento.example/"})
        self.assertTrue(r.startswith("[erro] ler_pagina: falha ao baixar https://lento.example/"), r)
        self.assertIn("ReadTimeout", r)

    def test_servidor_que_pinga_aos_poucos_estoura_o_teto_total(self):
        def gotejando():                          # ~3 s no total: sem o teto, o teste falha em vez de travar
            yield b"<html><body>"
            for _ in range(60):
                time.sleep(0.05)
                yield b"<p>um pouco</p>"

        self.usar_transporte({"https://gota.example/": lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=gotejando())})
        inicio = time.monotonic()
        with mock.patch.object(lib_web, "baixar", functools.partial(lib_web.baixar, timeout=0.3)):
            r = lf.executar("ler_pagina", {"url": "https://gota.example/"})
        self.assertIn("passou de 0.3 s", r)
        self.assertLess(time.monotonic() - inicio, 2)

    def test_pdf_e_erro_http_viram_erro_curto(self):
        self.usar_transporte({
            "https://pdf.example/x.pdf": httpx.Response(200, headers={"content-type": "application/pdf"},
                                                        content=b"%PDF-1.7"),
            "https://erro.example/": httpx.Response(500, text="erro"),
        })
        self.assertIn("tipo 'application/pdf' ignorado", lf.executar("ler_pagina", {"url": "https://pdf.example/x.pdf"}))
        self.assertIn("HTTP 500", lf.executar("ler_pagina", {"url": "https://erro.example/"}))


if __name__ == "__main__":
    unittest.main()
