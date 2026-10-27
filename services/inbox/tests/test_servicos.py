"""
Testes de regressão da trilha "serviços": shell executor, researcher, briefing, sync-knowledge e tools do Open WebUI.

    cd services/inbox
    venv/bin/python -m unittest tests.test_servicos -v

Tudo offline: nada de LLM, TTS, rede ou serviço real. Os módulos são carregados pelo caminho (importlib),
vault/logs em pastas temporárias, `requests` trocado por um módulo falso quando o venv não o tem.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import yaml

AQUI = Path(__file__).parent
REPO = AQUI.parent.parent.parent
SERVICES = REPO / "services"
TOOLS = REPO / "tools"
sys.path.insert(0, str(SERVICES))


def _requests_falso():
    """Módulo `requests` mínimo para importar as tools (o venv do inbox não tem requests)."""
    if "requests" in sys.modules:
        return sys.modules["requests"]
    try:
        import requests  # noqa: F401
        return requests
    except ImportError:
        pass
    m = types.ModuleType("requests")
    exc = types.SimpleNamespace(ConnectionError=type("ConnectionError", (Exception,), {}),
                                Timeout=type("Timeout", (Exception,), {}))
    m.exceptions = exc
    m.ConnectionError, m.Timeout = exc.ConnectionError, exc.Timeout
    m.post = m.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("rede no teste"))
    sys.modules["requests"] = m
    return m


def carregar(caminho: Path, nome: str):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RespostaFalsa:
    def __init__(self, status=200, dados=None):
        self.status_code, self._dados = status, dados or {}

    def json(self):
        return self._dados

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ══════════════════════════════════════════════════════════════
#  Shell executor
# ══════════════════════════════════════════════════════════════

executor = carregar(SERVICES / "shell-executor" / "main.py", "alfred_executor_main")


class TestExecutorWhitelist(unittest.TestCase):
    PERMITIDOS = [
        # o que as tools mandam (alfred_system_monitor / alfred_briefing / alfred_shell_executor)
        "lscpu | grep -E 'Model name|CPU\\(s\\)|MHz' | head -5",
        "uptime", "free -h", "df -h / /home", "nvidia-smi",
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader",
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null",
        "systemctl --user status alfred-executor --no-pager", "systemctl --user is-active alfred-inbox",
        "systemctl status docker", "journalctl --user -u alfred-inbox -n 50 --no-pager",
        "docker stats --no-stream", "docker logs alfred-ollama --tail 20", "docker compose ps", "docker system df",
        "curl http://localhost:7070/health", "curl -s http://127.0.0.1:7071/health", "cat /proc/cpuinfo",
        "date", "ls", "ls -la ~", "ps aux | grep ollama | head -3",
        executor.BRIEFING_RUN_SH, executor.BRIEFING_RUN_SH + " 'protocolo QUIC'",
    ]
    RECUSADOS = [
        "ls /tmp; echo INJETADO", "date && id -un", "date || id", "pwd | sed s/^/PIPE:/", "date | id",
        "date $(echo X >&2)", "date `id`", "ls\nid -un", "date > /tmp/x", "date < /etc/passwd", "date &",
        "echo $(whoami)", "ls \"$HOME\"", "ls {a,b}", "ls 'sem fechar", "ls |", "| ls", "dateX", "datex",
        "rm -rf /tmp/x", "systemctl stop alfred-inbox", "systemctl --user restart alfred-executor",
        "journalctl --user -u alfred-", "journalctl --user --vacuum-size=1 -u alfred-x", "journalctl -u sshd",
        "docker run -v /:/host alpine", "docker rm -f alfred-x", "docker stats", "docker --host x ps",
        "docker compose down", "docker system prune",
        "find / -delete", "find . -name x -exec id {} +", "find . -fprint /tmp/x",
        "curl http://localhost.evil.com", "curl http://localhost@evil.com", "curl http://localhost evil.com",
        "curl https://evil.com", "curl -o /tmp/x http://localhost", "curl -L http://localhost",
        "curl -d @/etc/passwd http://localhost", "curl -sSL http://localhost",
        "cat /etc/passwd", "cat /proc/../etc/passwd", "cat /proc/self/root/etc/passwd",
    ]

    def test_permitidos(self):
        for c in self.PERMITIDOS:
            with self.subTest(c=c):
                self.assertTrue(executor.is_allowed(c))

    def test_recusados(self):
        for c in self.RECUSADOS:
            with self.subTest(c=c):
                self.assertFalse(executor.is_allowed(c))

    def test_briefing_por_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "Alfred-Pennyworth"
            link.symlink_to(Path(executor.BRIEFING_RUN_SH).parent.parent.parent)
            self.assertTrue(executor.is_allowed(f"{link}/services/briefing/run.sh"))
            self.assertFalse(executor.is_allowed(f"{link}/services/briefing/run.sh; id"))

    def test_timeout_validado(self):
        from pydantic import ValidationError
        for t in (0, -5, 1801):
            with self.subTest(t=t), self.assertRaises(ValidationError):
                executor.CommandRequest(command="date", timeout=t)
        self.assertEqual(executor.CommandRequest(command="date").timeout, 15)
        self.assertEqual(executor.CommandRequest(command="x", timeout=1800).timeout, 1800)


class TestExecutorExecucao(unittest.TestCase):
    def test_pipeline_e_locale(self):
        argvs, descartar = executor._analisar("lscpu | grep -E 'Model name|CPU\\(s\\)|MHz' | head -5")
        r = executor._executar(argvs, 10, descartar)
        self.assertEqual(r["returncode"], 0)
        self.assertIn("Model name", r["stdout"])           # LC_ALL=C.UTF-8 (antes: "Nome do modelo" em pt_BR)

    def test_descarta_stderr(self):
        argvs, descartar = executor._analisar("ls /nao-existe-xyz 2>/dev/null")
        self.assertTrue(descartar)
        self.assertEqual(argvs, [["ls", "/nao-existe-xyz"]])
        r = executor._executar(argvs, 10, descartar)
        self.assertEqual(r["stderr"], "")
        self.assertNotEqual(r["returncode"], 0)

    def test_comando_inexistente(self):
        r = executor._executar([["comando-que-nao-existe-xyz"]], 5, False)
        self.assertEqual(r["returncode"], 127)

    def test_timeout_mata_o_grupo(self):
        marca = "31.4159"
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            executor._executar([["sh", "-c", f"sleep {marca} & sleep {marca}; wait"]], 1, False)
        self.assertLess(time.monotonic() - t0, 5)
        time.sleep(0.2)
        vivos = subprocess.run(["pgrep", "-f", f"sleep {marca}"], capture_output=True, text=True).stdout
        self.assertEqual(vivos.strip(), "", "filho do comando sobreviveu ao timeout")

    def test_endpoint(self):
        from fastapi.testclient import TestClient
        with mock.patch.object(executor, "TOKEN", "tk"):
            c = TestClient(executor.app)
            h = {"Authorization": "Bearer tk"}
            self.assertEqual(c.post("/execute", json={"command": "date && id"}, headers=h).status_code, 403)
            self.assertEqual(c.post("/execute", json={"command": "date", "timeout": 0}, headers=h).status_code, 422)
            r = c.post("/execute", json={"command": "date"}, headers=h)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["returncode"], 0)


# ══════════════════════════════════════════════════════════════
#  Researcher + sync-knowledge
# ══════════════════════════════════════════════════════════════

researcher = carregar(SERVICES / "researcher" / "main.py", "alfred_researcher_main")


class TestResearcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.patches = [
            mock.patch.object(researcher, "VAULT_ALFRED", self.vault),
            mock.patch.object(researcher, "TOKEN", "tk"),
            mock.patch.object(researcher, "WEBUI_TOKEN", ""),
            mock.patch.object(researcher, "_get_chroma", lambda: None),
            mock.patch.object(researcher, "searxng_search",
                              lambda q, max_results=8: [{"url": f"http://x/{q}", "title": q, "snippet": "s"}]),
        ]
        for p in self.patches:
            p.start()
        from fastapi.testclient import TestClient
        self.cliente = TestClient(researcher.app)
        self.h = {"Authorization": "Bearer tk"}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _post(self, topic, **kw):
        return self.cliente.post("/research", json={"topic": topic, "num_queries": 1, **kw}, headers=self.h)

    def test_sintese_vazia_503_sem_gravar(self):
        with mock.patch.object(researcher, "ollama_generate", lambda *a, **k: ""):
            r = self._post("teste sem ollama")
        self.assertEqual(r.status_code, 503)
        self.assertFalse((self.vault / "research").exists() and any((self.vault / "research").rglob("*.md")))

    def test_cache_ignora_sintese_vazia(self):
        d = self.vault / "research" / "tema-x"
        d.mkdir(parents=True)
        (d / "synthesis.md").write_text("---\ntitle: x\n---\n# tema x\n\n\n\n---\n*Sintetizado em 26/10/2026 18:38 "
                                        "a partir de 3 fontes*\n", encoding="utf-8")
        self.assertFalse(researcher._cache_valid(d / "synthesis.md"))
        (d / "synthesis.md").write_text("# tema x\n\n## Visão Geral\nconteúdo\n\n---\n*Sintetizado em ...*\n")
        self.assertTrue(researcher._cache_valid(d / "synthesis.md"))

    def test_tema_vazio_e_slug_vazio(self):
        self.assertEqual(self._post("   ").status_code, 422)
        with mock.patch.object(researcher, "ollama_generate", lambda *a, **k: "## Visão Geral\nok"):
            r = self._post("???")
        self.assertEqual(r.status_code, 200)
        slug = r.json()["slug"]
        self.assertTrue(slug.startswith("tema-") and len(slug) > 5)
        self.assertTrue((self.vault / "research" / slug / "synthesis.md").exists())
        self.assertFalse((self.vault / "research" / "synthesis.md").exists())
        # outro tema só de símbolos não pega esta síntese como "cache exato"
        with mock.patch.object(researcher, "ollama_generate", lambda *a, **k: "## Visão Geral\nok"):
            self.assertFalse(self._post("!!!").json()["cached"])

    def test_trigger_sync_usa_venv_e_loga(self):
        scripts = Path(self.tmp.name) / "scripts"
        scripts.mkdir()
        (scripts / "sync-knowledge.py").write_text(
            "import sys\nprint('exe=' + sys.executable)\nprint('erro-do-sync', file=sys.stderr)\n")
        log = Path(self.tmp.name) / "logs" / "sync-knowledge.log"
        with mock.patch.object(researcher, "SCRIPTS_DIR", scripts), mock.patch.object(researcher, "SYNC_LOG", log), \
                mock.patch.object(researcher, "WEBUI_TOKEN", "tok"):
            researcher.trigger_sync()
            for _ in range(100):
                if log.exists() and "erro-do-sync" in log.read_text():
                    break
                time.sleep(0.05)
        texto = log.read_text()
        self.assertIn("erro-do-sync", texto)
        self.assertIn(f"exe={sys.executable}", texto)


class TestSyncKnowledge(unittest.TestCase):
    def test_sem_token_mensagem_clara(self):
        env = {k: v for k, v in os.environ.items() if k != "WEBUI_API_TOKEN"}
        r = subprocess.run([sys.executable, str(SERVICES / "scripts" / "sync-knowledge.py")],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("WEBUI_API_TOKEN não definido", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_nao_usa_requests(self):
        fonte = (SERVICES / "scripts" / "sync-knowledge.py").read_text()
        self.assertNotIn("import requests", fonte)
        self.assertIn("httpx", fonte)


# ══════════════════════════════════════════════════════════════
#  Briefing (tech-briefing.py)
# ══════════════════════════════════════════════════════════════

sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))   # fetch_rss é trocado nos testes
briefing = carregar(SERVICES / "briefing" / "tech-briefing.py", "alfred_tech_briefing")


def _feed(nome, n):
    return [{"title": f"{nome} notícia número {i} sobre assunto {nome}{i}", "url": f"http://{nome}/{i}",
             "snippet": "s", "source": nome} for i in range(n)]


class TestBriefing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault = base / "vault"
        self.seen = base / "logs" / "briefing_seen.json"
        self.patches = [
            mock.patch.object(briefing, "VAULT_ALFRED", self.vault),
            mock.patch.object(briefing, "SEEN_FILE", self.seen),
            mock.patch.object(briefing, "load_config", lambda: {"max_items": 15}),
            mock.patch.object(briefing, "subprocess", mock.MagicMock()),           # notify-send
            mock.patch.object(briefing, "_cluster_items", lambda itens: itens),
            mock.patch.object(briefing, "_rank_clusters", lambda itens, pesos: []),
            mock.patch.object(briefing, "_build_clustered_news_text", lambda *a, **k: ("destaques", "radar")),
            mock.patch.object(briefing, "render_template", lambda nome, v: f"prompt {nome}"),
            mock.patch.object(briefing, "ollama_generate", lambda *a, **k: "## Briefing\ntexto http://A/0"),
            mock.patch.object(briefing, "generate_title_via_llm", lambda t: "Manchete do Dia"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_rodizio_entre_fontes_antes_do_corte(self):
        grupos = [_feed(n, 10 if n in ("A", "B") else 5) for n in "ABCDEFGHIJ"]
        itens = [it for g in grupos for it in g]
        saida = briefing._intercalar_fontes(itens, grupos)
        self.assertEqual(sorted(map(id, saida)), sorted(map(id, itens)))
        self.assertEqual({it["source"] for it in saida[:30]}, set("ABCDEFGHIJ"))
        self.assertEqual([it["source"] for it in saida[:10]], list("ABCDEFGHIJ"))

    def _cfg(self, audio=True):
        return {"perfil": "p", "_perfil": {}, "max_items": 15, "template_prompt": "t", "template_audio": "ta",
                "formato": ["md", "audio"] if audio else ["md"],
                "_fontes": [{"tipo": "rss", "feeds": [{"name": n} for n in "ABCDEFGHIJ"]}]}

    def _rodar(self, tts):
        feeds = {n: _feed(n, 10 if n in ("A", "B") else 5) for n in "ABCDEFGHIJ"}
        with mock.patch.object(briefing, "fetch_rss", lambda f: feeds[f["name"]]), \
                mock.patch.object(briefing, "generate_tts", tts), \
                mock.patch("builtins.print") as pr:
            briefing._run_briefing_nomeado(self._cfg(), "matinal_tech")
        return [c.args[0] for c in pr.call_args_list if str(c.args[0]).startswith("BRIEFING_FILE=")]

    def test_tts_falha_nao_aborta_e_grava_memoria(self):
        async def tts_quebrado(texto, voz, caminho):
            Path(caminho).write_bytes(b"")
            raise RuntimeError("No audio was received")
        saidas = self._rodar(tts_quebrado)
        self.assertEqual(len(saidas), 1)
        mds = list((self.vault / "briefings").glob("*.md"))
        self.assertEqual(len(mds), 1)
        self.assertRegex(mds[0].name, r"^\d{4}-\d{2}-\d{2}_\d{4}_matinal_tech_manchete-do-dia\.md$")
        self.assertEqual(list((self.vault / "briefings").glob("*.mp3")), [])     # sem .mp3 de 0 byte
        import json
        vistos = json.loads(self.seen.read_text())
        self.assertEqual(len(vistos), 30)
        self.assertEqual({u.split("/")[2] for u in vistos}, set("ABCDEFGHIJ"))  # todas as fontes entraram

    def test_tts_ok(self):
        async def tts(texto, voz, caminho):
            Path(caminho).write_bytes(b"ID3audio")
        self._rodar(tts)
        self.assertEqual(len(list((self.vault / "briefings").glob("*_matinal_tech_*.mp3"))), 1)

    def test_main_keyerror_interno_nao_vira_adhoc(self):
        with mock.patch.object(briefing, "get_briefing", lambda n: self._cfg()), \
                mock.patch.object(briefing, "_run_briefing_nomeado", side_effect=KeyError("title")), \
                mock.patch.object(briefing, "_run_adhoc") as adhoc, \
                mock.patch.object(briefing, "log") as lg, mock.patch.object(sys, "argv", ["x", "matinal_tech"]):
            with self.assertRaises(SystemExit):
                briefing.main()
        adhoc.assert_not_called()
        msgs = " ".join(str(c.args[0]) for c in lg.call_args_list)
        self.assertIn("Falha no briefing", msgs)
        self.assertNotIn("perfis.yml", msgs)

    def test_main_tema_vai_para_adhoc(self):
        with mock.patch.object(briefing, "get_briefing", side_effect=KeyError("x")), \
                mock.patch.object(briefing, "_run_adhoc") as adhoc, mock.patch.object(briefing, "log"), \
                mock.patch.object(sys, "argv", ["x", "protocolo QUIC"]):
            briefing.main()
        adhoc.assert_called_once_with("protocolo QUIC")


# ══════════════════════════════════════════════════════════════
#  Tools do Open WebUI
# ══════════════════════════════════════════════════════════════

_requests_falso()


class TestToolResearcher(unittest.TestCase):
    def setUp(self):
        self.mod = carregar(TOOLS / "alfred_researcher.py", "tool_alfred_researcher")
        self.tool = self.mod.Tools()

    def _com_resposta(self, dados):
        with mock.patch.object(self.mod.requests, "post", lambda *a, **k: RespostaFalsa(200, dados)):
            return self.tool.research_topic("protocolo QUIC")

    def test_cache_exato(self):
        out = self._com_resposta({"status": "success", "topic": "protocolo QUIC", "slug": "protocolo-quic",
                                  "vault_path": "/v/research/protocolo-quic", "cached": True, "cache_type": "exact",
                                  "synthesis": "# protocolo QUIC\nSÍNTESE", "files": ["index.md"]})
        self.assertIn("do cache", out)
        self.assertIn("SÍNTESE", out)
        self.assertNotIn("Erro", out)

    def test_cache_semantico(self):
        out = self._com_resposta({"topic": "Protocolo QUIC!", "slug": "protocolo-quic", "cached": True,
                                  "cache_type": "semantic", "matched_topic": "protocolo QUIC",
                                  "matched_slug": "protocolo-quic", "similarity": 0.93, "vault_path": "/v",
                                  "synthesis": "SÍNTESE", "files": ["index.md"]})
        self.assertIn("semântico", out)
        self.assertNotIn("Erro", out)

    def test_pesquisa_nova(self):
        out = self._com_resposta({"topic": "t", "slug": "t", "vault_path": "/v", "cached": False,
                                  "queries_used": ["a", "b"], "sources_found": 7, "files": ["index.md"]})
        self.assertIn("Fontes encontradas: 7", out)
        self.assertIn("a; b", out)


class TestToolShellExecutor(unittest.TestCase):
    def test_timeout_none(self):
        mod = carregar(TOOLS / "alfred_shell_executor.py", "tool_alfred_shell_executor")
        enviado = {}

        def post(url, headers=None, json=None, timeout=None):
            enviado.update(json=json, timeout=timeout)
            return RespostaFalsa(200, {"stdout": "ok", "stderr": "", "returncode": 0})
        with mock.patch.object(mod.requests, "post", post):
            self.assertEqual(mod.Tools().execute_command("date", None), "ok")
        self.assertEqual(enviado["json"]["timeout"], 15)



class _ComVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TestToolTasks(_ComVault):
    def setUp(self):
        super().setUp()
        self.mod = carregar(TOOLS / "alfred_tasks.py", "tool_alfred_tasks")
        self.tool = self.mod.Tools()
        self.tool.valves.vault_path = str(self.vault)

    def test_arquivo_ilegivel_nao_e_sobrescrito(self):
        self.tool.add_task("Uma")
        self.tool.add_task("Duas")
        f = self.vault / "tasks.yml"
        quebrado = f.read_text().replace("title: Duas", "title: Duas: revisar: [x")
        f.write_text(quebrado)
        self.assertIn("erro de YAML", self.tool.list_tasks("all"))
        r = self.tool.add_task("Nova após edição manual")
        self.assertIn("não criada", r)
        self.assertEqual(f.read_text(), quebrado)
        self.assertIn("Erro", self.tool.complete_task(1))
        self.assertIn("Erro", self.tool.list_overdue())
        self.assertEqual(f.read_text(), quebrado)
        f.write_text("- só uma lista\n")
        self.assertIn("não criada", self.tool.add_task("x"))
        self.assertEqual(f.read_text(), "- só uma lista\n")

    def test_due_date_validada_e_normalizada(self):
        self.assertIn("vence em 2026-10-01", self.tool.add_task("Boleto", "2026-10-1"))
        self.assertIn("vence em 2026-10-27", self.tool.add_task("BR", "27/10/2026"))
        self.assertIn("inválida", self.tool.add_task("Sexta", "sexta"))
        dados = yaml.safe_load((self.vault / "tasks.yml").read_text())
        self.assertEqual([t["due_date"] for t in dados["tasks"]], ["2026-10-01", "2026-10-27"])

    def test_data_invalida_no_arquivo_nao_quebra(self):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        (self.vault / "tasks.yml").write_text(yaml.safe_dump({"tasks": [
            {"id": 1, "title": "Torta", "status": "open", "due_date": "2026-10-1"},
            {"id": 2, "title": "Texto", "status": "open", "due_date": "sexta"},
            {"id": 3, "title": "Ontem", "status": "open", "due_date": ontem},
        ]}, allow_unicode=True) + "- id: 4\n  title: SemAspas\n  status: open\n  due_date: 2020-01-02\n")
        venc = self.tool.list_overdue()
        self.assertIn("Ontem", venc)
        self.assertIn("Torta", venc)          # 2026-10-1 normalizada
        self.assertIn("SemAspas", venc)       # date do YAML
        self.assertIn("inválida", venc)       # "sexta"
        lst = self.tool.list_tasks("all")
        self.assertIn("data inválida", lst)


class TestToolPendenciasTasks(_ComVault):
    def test_add_task_nao_sobrescreve_ilegivel(self):
        mod = carregar(TOOLS / "alfred_pendencias.py", "tool_alfred_pendencias")
        tool = mod.Tools()
        tool.valves.vault_path = str(self.vault)
        f = self.vault / "tasks.yml"
        f.write_text("tasks:\n- id: 7\n  title: a: b: [\n")
        self.assertIsNone(tool._add_task("Ir: banca", "2026-10-31", ["pendencia"]))
        self.assertIn("ilegível", tool._erro_tasks)
        self.assertEqual(f.read_text(), "tasks:\n- id: 7\n  title: a: b: [\n")
        f.unlink()
        self.assertEqual(tool._add_task("Ir: banca", "2026-10-31", ["pendencia"]), 1)


class TestToolMemory(_ComVault):
    def setUp(self):
        super().setUp()
        self.mod = carregar(TOOLS / "alfred_memory.py", "tool_alfred_memory")
        self.tool = self.mod.Tools()
        self.tool.valves.vault_path = str(self.vault)

    def test_expira_com_fuso_e_formato_br(self):
        self.tool.remember("tz", "com fuso", "2020-10-20T10:00:00-03:00")
        self.assertIn("expirou", self.tool.recall("tz"))
        self.tool.remember("br", "formato br", "01/01/2020")
        self.assertIn("expirou", self.tool.recall("br"))
        self.tool.remember("futuro", "ainda vale", "31/12/2099")
        self.assertEqual(self.tool.recall("futuro"), "futuro: ainda vale")
        self.assertIn("inválida", self.tool.remember("x", "y", "semana que vem"))
        todas = self.tool.recall_all()
        self.assertIn("futuro", todas)
        self.assertNotIn("com fuso", todas)

    def test_arquivo_ilegivel_nao_e_sobrescrito(self):
        f = self.vault / "memory.yml"
        f.write_text("memories:\n- key: a\n  value: b: c: [\n")
        self.assertIn("Erro", self.tool.remember("k", "v"))
        self.assertIn("Erro", self.tool.forget("a"))
        self.assertIn("Erro", self.tool.recall_all())
        self.assertEqual(f.read_text(), "memories:\n- key: a\n  value: b: c: [\n")


class TestToolDailyDigest(_ComVault):
    def test_days_back_none(self):
        mod = carregar(TOOLS / "alfred_daily_digest.py", "tool_alfred_daily_digest")
        tool = mod.Tools()
        tool.valves.vault_path = str(self.vault)
        (self.vault / "tasks.yml").write_text("tasks:\n- id: 1\n  title: SemAspas\n  status: open\n"
                                              "  due_date: 2020-01-02\n")
        r = tool.daily_digest(None)
        self.assertIn("SemAspas", r)



class TestToolBriefing(_ComVault):
    def setUp(self):
        super().setUp()
        self.mod = carregar(TOOLS / "alfred_briefing.py", "tool_alfred_briefing")
        self.tool = self.mod.Tools()
        self.tool.valves.vault_path = str(self.vault / "container")
        self.tool.valves.vault_alfred_host = str(self.vault / "host")
        self.pasta = self.vault / "container" / "briefings"
        self.pasta.mkdir(parents=True)
        antigo = self.pasta / "2026-10-26_ti-para-meepp.md"            # nome antigo, maior em ordem alfabética
        antigo.write_text("---\ntitle: \"Antigo\"\n---\nBRIEFING ANTIGO")
        os.utime(antigo, (time.time() - 3600, time.time() - 3600))

    def _gerar(self, stdout, rc):
        resp = RespostaFalsa(200, {"stdout": stdout, "stderr": "", "returncode": rc})
        with mock.patch.object(self.mod.requests, "post", lambda *a, **k: resp):
            return self.tool.gerar_briefing()

    def test_falha_nao_mostra_briefing_antigo(self):
        out = self._gerar("[ollama_generate] Erro: Connection refused\n[ERRO] Ollama não retornou síntese.", 1)
        self.assertIn("Falha ao gerar o briefing", out)
        self.assertIn("Ollama não retornou", out)
        self.assertNotIn("BRIEFING ANTIGO", out)

    def test_converte_pela_valve_vault_path(self):
        novo = self.pasta / "2026-10-26_1855_matinal_tech_anthropic.md"
        novo.write_text("---\ntitle: \"Novo\"\n---\nBRIEFING NOVO")
        host = self.vault / "host" / "briefings" / novo.name
        out = self._gerar(f"[x] ok\nBRIEFING_FILE={host}\n", 0)
        self.assertIn("BRIEFING NOVO", out)
        self.assertNotIn("BRIEFING ANTIGO", out)

    def test_ver_briefings_por_mtime_e_audio_vazio(self):
        novo = self.pasta / "2026-10-26_1855_matinal_tech_anthropic.md"
        novo.write_text("---\ntitle: \"Novo\"\n---\nx")
        novo.with_suffix(".mp3").write_bytes(b"")
        out = self.tool.ver_briefings(5)
        self.assertLess(out.index("Novo"), out.index("Antigo"))
        self.assertNotIn("com áudio", out)

    def test_ler_briefing_fora_da_pasta(self):
        (self.vault / "container" / "segredo.md").write_text("SEGREDO")
        self.assertNotIn("SEGREDO", self.tool.ler_briefing("../segredo.md"))
        self.assertNotIn("SEGREDO", self.tool.ler_briefing(str(self.vault / "container" / "segredo.md")))
        self.assertIn("BRIEFING ANTIGO", self.tool.ler_briefing("2026-10-26_ti-para-meepp"))


class TestToolsVault(_ComVault):
    def setUp(self):
        super().setUp()
        self.leitor = carregar(TOOLS / "alfred_vault_reader.py", "tool_alfred_vault_reader").Tools()
        self.escritor = carregar(TOOLS / "alfred_vault_writer.py", "tool_alfred_vault_writer").Tools()
        (self.vault / "pedro" / "projetos").mkdir(parents=True)
        (self.vault / "pedro" / "projetos" / "quic.md").write_text("notas sobre QUIC")
        (self.vault / "alfred").mkdir()
        (self.vault / "alfred" / "tasks.yml").write_text("SEGREDO-ALFRED QUIC")
        (self.vault / "fora.md").write_text("SEGREDO-FORA QUIC")
        self.leitor.valves.vault_path = str(self.vault / "pedro")
        self.escritor.valves.vault_path = str(self.vault / "alfred")

    def test_reader_nao_sai_do_vault(self):
        self.assertIn("notas sobre QUIC", self.leitor.read_note("projetos/quic.md"))
        for caminho in ("../alfred/tasks.yml", str(self.vault / "fora.md"), "/etc/hostname"):
            with self.subTest(caminho=caminho):
                self.assertIn("fora do vault", self.leitor.read_note(caminho))
        self.assertIn("fora do vault", self.leitor.search_vault("QUIC", "../alfred"))
        self.assertIn("quic.md", self.leitor.search_vault("QUIC"))

    def test_writer_nao_sai_do_vault(self):
        r = self.escritor.save_to_vault("fora do vault", "x", str(self.vault / "outra"))
        self.assertIn("fora do vault", r)
        self.assertFalse((self.vault / "outra").exists())
        self.assertIn("fora do vault", self.escritor.save_to_vault("t", "x", "../pedro"))
        self.assertIn("fora do vault", self.escritor.list_vault("../pedro"))
        self.assertIn("fora do vault", self.escritor.search_vault("QUIC", ".."))

    def test_writer_frontmatter_titulo_com_dois_pontos_e_sem_sobrescrever(self):
        r1 = self.escritor.save_to_vault("Decisão: usar QUIC", "conteudo 1", "decisions")
        r2 = self.escritor.save_to_vault("Decisão: usar QUIC", "conteudo 2", "decisions")
        self.assertNotEqual(r1, r2)
        notas = sorted((self.vault / "alfred" / "decisions").glob("*.md"))
        self.assertEqual(len(notas), 2)
        corpos = {n.read_text().split("---\n", 2)[2].strip() for n in notas}
        self.assertEqual(corpos, {"conteudo 1", "conteudo 2"})
        meta = yaml.safe_load(notas[0].read_text().split("---\n", 2)[1])
        self.assertEqual(meta["title"], "Decisão: usar QUIC")

    def test_writer_titulo_vazio(self):
        r = self.escritor.save_to_vault("???", "sem titulo", "logs")
        self.assertRegex(r, r"logs/\d{4}-\d{2}-\d{2} nota-\d{6}\.md$")


if __name__ == "__main__":
    unittest.main()
