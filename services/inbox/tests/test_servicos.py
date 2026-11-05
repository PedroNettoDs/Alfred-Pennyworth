"""
Testes de regressão da trilha "serviços": shell executor, researcher e briefing.

    cd services/inbox
    venv/bin/python -m unittest tests.test_servicos -v

Tudo offline: nada de LLM, TTS, rede ou serviço real. Os módulos são carregados pelo caminho (importlib),
vault/logs em pastas temporárias.
"""

import importlib.util
import inspect
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
REPO = AQUI.parent.parent.parent
SERVICES = REPO / "services"
sys.path.insert(0, str(SERVICES))


def carregar(caminho: Path, nome: str):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════
#  Shell executor
# ══════════════════════════════════════════════════════════════

executor = carregar(SERVICES / "shell-executor" / "main.py", "alfred_executor_main")


class TestExecutorWhitelist(unittest.TestCase):
    PERMITIDOS = [
        # o que as ferramentas da conversa mandam (relatorio_sistema / gerar_briefing / executar_comando)
        "lscpu | grep -E 'Model name|CPU\\(s\\)|MHz' | head -5",
        "uptime", "free -h", "df -h / /home", "nvidia-smi",
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader",
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null",
        "systemctl --user status alfred-executor --no-pager", "systemctl --user is-active alfred-inbox",
        "systemctl status docker", "journalctl --user -u alfred-inbox -n 50 --no-pager",
        "docker stats --no-stream", "docker logs alfred-ollama --tail 20", "docker compose ps", "docker system df",
        "curl http://localhost:7071/health", "curl -s http://127.0.0.1:7072/health", "cat /proc/cpuinfo",
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
        self.assertFalse(r["truncado"])

    def test_saida_limitada(self):
        # antes: head -c 50M voltava inteiro (78,6 MB de resposta) e passava pelo /tmp (tmpfs)
        r = executor._executar([["head", "-c", "50M", "/dev/zero"]], 10, False)
        self.assertEqual(r["returncode"], 0)                # o comando vai até o fim: o excedente é lido e descartado
        self.assertTrue(r["truncado"])
        self.assertEqual(len(r["stdout"]), executor.MAX_SAIDA_BYTES)
        r = executor._executar([["sh", "-c", "head -c 3M /dev/zero >&2"]], 10, False)
        self.assertTrue(r["truncado"])
        self.assertEqual(len(r["stderr"]), executor.MAX_SAIDA_BYTES)
        self.assertEqual(r["stdout"], "")

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

    def test_limite_de_simultaneos(self):
        from fastapi.testclient import TestClient
        self.assertTrue(inspect.iscoroutinefunction(executor.health))   # /health fora do pool de threads
        vagas = threading.BoundedSemaphore(1)
        with mock.patch.object(executor, "TOKEN", "tk"), mock.patch.object(executor, "_vagas", vagas):
            c = TestClient(executor.app)
            h = {"Authorization": "Bearer tk"}
            # a vaga é devolvida depois do timeout (408)
            r = c.post("/execute", json={"command": "tail -n 1 -f /dev/null", "timeout": 1}, headers=h)
            self.assertEqual(r.status_code, 408)
            vagas.acquire()                                   # um comando ocupando a única vaga
            try:
                self.assertEqual(c.post("/execute", json={"command": "date"}, headers=h).status_code, 429)
                self.assertEqual(c.get("/health").status_code, 200)
            finally:
                vagas.release()
            self.assertEqual(c.post("/execute", json={"command": "date"}, headers=h).status_code, 200)


# ══════════════════════════════════════════════════════════════
#  Researcher
# ══════════════════════════════════════════════════════════════

researcher = carregar(SERVICES / "researcher" / "main.py", "alfred_researcher_main")
INDEXAR_LOCAL = researcher.indexar_local   # o setUp troca por um Mock; test_indexar_local_em_segundo_plano usa o original


class TestResearcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.indexar = mock.Mock()
        self.patches = [
            mock.patch.object(researcher, "VAULT_ALFRED", self.vault),
            mock.patch.object(researcher, "TOKEN", "tk"),
            mock.patch.object(researcher, "indexar_local", self.indexar),
            mock.patch.object(researcher, "_get_chroma", lambda: None),
            mock.patch.object(researcher, "searxng_search",
                              lambda q, categories="news,it", max_results=8:
                              [{"url": f"http://x/{q}", "title": q, "snippet": "s"}]),
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
        self.indexar.assert_called_once_with()   # nota nova vai para o índice local (RAG), não para o Open WebUI
        # outro tema só de símbolos não pega esta síntese como "cache exato"
        with mock.patch.object(researcher, "ollama_generate", lambda *a, **k: "## Visão Geral\nok"):
            self.assertFalse(self._post("!!!").json()["cached"])

    def test_indexar_local_em_segundo_plano(self):
        chamadas, pronto = [], threading.Event()

        def indexar_cofre(vault, nome):
            chamadas.append((vault, nome, threading.current_thread() is threading.main_thread()))
            pronto.set()

        with mock.patch.object(researcher.rag, "indexar_cofre", indexar_cofre):
            INDEXAR_LOCAL()
            self.assertTrue(pronto.wait(5))
        self.assertEqual(chamadas, [(self.vault, "alfred", False)])


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


if __name__ == "__main__":
    unittest.main()
