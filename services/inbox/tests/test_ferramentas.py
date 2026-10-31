"""
Testes de _shared/lib_ferramentas.py (tool calling nativo do Ollama): schema gerado da assinatura, conversão de
argumentos, erro e timeout virando texto, marcação de perigosas e cada ferramenta.

    cd services/inbox
    venv/bin/python -m unittest tests.test_ferramentas -v

Offline: vault temporário (pendências, tasks.yml, briefings, jornal e o índice FTS5 de verdade); HTTP (Shell
Executor, Researcher), subprocess do briefing, envio de WhatsApp, limpeza e lib_memoria mockados.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
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

import core        # noqa: E402
import whatsapp    # noqa: E402
import limpeza     # noqa: E402
from _shared import lib_db, lib_memoria  # noqa: E402
from _shared import lib_ferramentas as lf  # noqa: E402

TOKEN = "token-de-teste"
PERIGOSAS = {"executar_comando", "responder_pendencia", "responder_limpeza", "salvar_nota", "completar_tarefa"}


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
        }
        for p in (mock.patch.dict(os.environ, env),
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

        r = lf.executar("buscar_cofre", {"consulta": "servidor"})
        self.assertIn("2 resultado(s)", r)
        self.assertIn("[pedro] projetos/servidor.md", r)
        self.assertIn("[alfred] decisions/quic.md", r)
        r = lf.executar("buscar_cofre", {"consulta": "servidor", "cofre": "alfred"})
        self.assertIn("1 resultado(s)", r)
        self.assertNotIn("[pedro]", r)
        self.assertIn("Nenhuma nota", lf.executar("buscar_cofre", {"consulta": "inexistentequalquer"}))

    def test_buscar_cofre_passa_por_buscar(self):
        achado = {"cofre": "pedro", "caminho": "a.md", "titulo": "a", "trecho": "trecho", "score": 3.2}
        with mock.patch.object(lf, "_buscar", return_value=[achado]) as buscar:
            r = lf.executar("buscar_cofre", {"consulta": "rag", "limite": 99})
        buscar.assert_called_once_with("rag", None, 20)
        self.assertIn("### [pedro] a.md (score 3.2)\ntrecho", r)

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


if __name__ == "__main__":
    unittest.main()
