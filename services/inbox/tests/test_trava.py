"""
Testes da trava entre processos (core.trava_arquivo) e das gravações atômicas dos YAML do vault.

    cd services/inbox
    venv/bin/python -m unittest tests.test_trava -v

Usam PROCESSOS de verdade (multiprocessing, contexto "spawn") — a trava antiga (_LOCK em whatsapp.py) só
valia entre threads. Tudo offline: LLM/TTS trocados por funções locais, WhatsApp sempre simulado
(EVOLUTION_URL vazio + WHATSAPP_DRY_RUN=true, envios em <tmp>/whatsapp_enviados.jsonl), vault temporário.
"""

import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import yaml

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

import core  # noqa: E402

CTX = multiprocessing.get_context("spawn")   # filho limpo: nada herdado do processo de teste além do que passamos
ESPERA = 90                                  # s — teto para filhos (spawn importa os módulos do zero)


# ══════════════════════════════════════════════════════════════
#  Funções que rodam nos processos filhos (nível de módulo: o spawn importa por nome)
# ══════════════════════════════════════════════════════════════

def _preparar_filho(vault: str, logs: str):
    os.environ.update({
        "VAULT_ALFRED": vault, "EVOLUTION_URL": "", "EVOLUTION_API_KEY": "CHAVE-DE-TESTE",
        "WHATSAPP_DRY_RUN": "true", "WHATSAPP_NUMERO_PEDRO": "5514999990000", "INBOX_AUTOPLAY": "false",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9",
    })
    import core as c
    import whatsapp as w
    from _shared import lib_whatsapp as lw
    lw.ENVIADOS_FILE = Path(logs) / "whatsapp_enviados.jsonl"
    lw.IDS_ENVIADOS_FILE = Path(logs) / "whatsapp_ids_enviados.json"
    w.FILA_FILE = Path(logs) / "whatsapp_fila.json"
    c._notify = lambda *a, **k: None
    c._iniciar_aguardador = lambda *a, **k: None
    return c, w


def _filho_escritor(vault, logs, idx, n, barreira, saida):
    """Cria `n` pendências (mesmo título → disputa de id), `n` tarefas e `n` itens de agenda."""
    try:
        c, _w = _preparar_filho(vault, logs)
        import agenda

        def llm_lento(prompt, **kw):   # o LLM "demora": a janela de corrida do código antigo fica aberta
            time.sleep(0.02)
            return "## O que é\nTeste de concorrência."
        c.ollama_generate = llm_lento
        c.tts_save = lambda *a, **k: False
        barreira.wait(ESPERA)
        ids, tarefas, itens = [], [], []
        for k in range(n):
            m = {"message_id": f"<p{idx}-{k}@teste>", "from_addr": "x@y.com", "from_name": "X", "subject": "Convite",
                 "date_iso": "2026-10-20T10:00", "text": "corpo"}
            ev = {"tipo": "evento", "titulo": "Meetup concorrente", "organizador": "X", "quem": "", "data_inicio": None,
                  "data_fim": None, "local": "", "online": None, "link": "", "custo": "desconhecido",
                  "o_que_pedem": "", "prazo_resposta": None, "resumo": f"processo {idx} item {k}"}
            ids.append(c.criar_pendencia(m, ev, {}, "teste")["id"])
            tarefas.append(c._add_task(f"tarefa {idx}-{k}", None, ["teste"]))
            itens.append(agenda.adicionar(f"Compromisso {idx} {k}", datetime(2030, 1, 1 + k, 10, 0).astimezone(),
                                          notificar=False)["id"])
        saida.put(("ok", idx, ids, tarefas, itens))
    except BaseException as e:   # o pai precisa saber, senão fica esperando a fila
        saida.put(("erro", idx, repr(e)))


def _filho_responder(vault, logs, pid, barreira, saida):
    """whatsapp.responder_texto na mesma pendência que outro processo — envio lento (dentro do simulado)."""
    try:
        c, w = _preparar_filho(vault, logs)
        real = w.enviar_texto

        def enviar_lento(*a, **k):
            time.sleep(0.4)
            return real(*a, **k)
        w.enviar_texto = enviar_lento
        barreira.wait(ESPERA)
        try:
            e = w.responder_texto(pid, f"resposta do processo {os.getpid()}", origem="via teste")
            saida.put(("respondida", e["status"], bool((e["whatsapp"].get("envio") or {}).get("simulado"))))
        except c.PendenciaJaResolvida as e:
            saida.put(("ja_resolvida", e.status, e.caminho))
    except BaseException as e:
        saida.put(("erro", repr(e), ""))


def _filho_regravar(vault, logs, total, pronto, parar, saida):
    """Regrava pendencias.yml sem parar (dois conteúdos grandes alternados) até `parar`."""
    try:
        c, _w = _preparar_filho(vault, logs)
        versoes = [{"pendencias": [{"id": f"p{i}", "tipo": "evento", "status": "aguardando", "titulo": f"{v}-{i}" * 20,
                                    "evento": {"resumo": "x" * 300}} for i in range(total)]} for v in ("A", "B")]
        n = 0
        while not parar.is_set() and n < 5000:
            with c.trava_pendencias():
                c._save_pend(versoes[n % 2])
            n += 1
            pronto.set()   # o arquivo já existe: o pai pode começar a ler
        saida.put(("ok", n))
    except BaseException as e:
        saida.put(("erro", repr(e)))


def _filho_segurar(vault, logs, caminho, pronto, soltar):
    c, _w = _preparar_filho(vault, logs)
    with c.trava_arquivo(Path(caminho)):
        pronto.set()
        soltar.wait(ESPERA)


def _filho_tentar(vault, logs, caminho, timeout, saida):
    c, _w = _preparar_filho(vault, logs)
    t0 = time.monotonic()
    try:
        with c.trava_arquivo(Path(caminho), timeout=timeout):
            saida.put(("pegou", time.monotonic() - t0, ""))
    except TimeoutError as e:
        saida.put(("timeout", time.monotonic() - t0, str(e)))


# ══════════════════════════════════════════════════════════════
#  Testes
# ══════════════════════════════════════════════════════════════

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-trava-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault = self.tmp / "vault"
        self.logs = self.tmp / "logs"
        self.vault.mkdir()
        self.logs.mkdir()
        for p in (mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.vault)}),
                  mock.patch.object(core, "_notify", lambda *a, **k: None),           # nada de notificação real
                  mock.patch.object(core, "_iniciar_aguardador", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        self.procs: list = []
        self.addCleanup(self._matar_filhos)

    def _matar_filhos(self):
        for p in self.procs:
            if p.is_alive():
                p.terminate()
            p.join(5)

    def filho(self, alvo, *args) -> "multiprocessing.Process":
        p = CTX.Process(target=alvo, args=(str(self.vault), str(self.logs), *args), daemon=True)
        p.start()
        self.procs.append(p)
        return p

    def gravar_pendencias(self, pendencias: list[dict]):
        with core.trava_pendencias():
            core._save_pend({"pendencias": pendencias})

    def enviados(self) -> list[str]:
        f = self.logs / "whatsapp_enviados.jsonl"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


class TestProcessosGravando(Base):
    def test_varios_processos_nao_perdem_gravacoes(self):
        """(a) N processos criando pendências (mesmo título), tarefas e itens de agenda ao mesmo tempo."""
        n_proc, n = 4, 5
        barreira, saida = CTX.Barrier(n_proc), CTX.Queue()
        for i in range(n_proc):
            self.filho(_filho_escritor, i, n, barreira, saida)
        res = [saida.get(timeout=ESPERA) for _ in range(n_proc)]
        self.assertTrue(all(r[0] == "ok" for r in res), res)

        pend = core.listar_pendencias("todas")
        criados = [pid for r in res for pid in r[2]]
        self.assertEqual(len(pend), n_proc * n)                              # nenhuma apagada
        self.assertEqual(sorted(p["id"] for p in pend), sorted(criados))
        self.assertEqual(len(set(criados)), n_proc * n)                      # ids únicos apesar do mesmo título
        for pid in criados:
            md = core.pend_dir() / f"{pid}.md"
            self.assertTrue(md.is_file() and md.stat().st_size > 0, pid)    # reserva virou o texto completo

        tasks = yaml.safe_load((self.vault / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(len(tasks), n_proc * n)
        self.assertEqual(sorted(t["id"] for t in tasks), list(range(1, n_proc * n + 1)))   # ids únicos e seguidos
        self.assertEqual(sorted(t["id"] for t in tasks), sorted(tid for r in res for tid in r[3]))

        import agenda
        itens = agenda.listar()
        self.assertEqual(len(itens), n_proc * n)
        self.assertEqual(len({i["id"] for i in itens}), n_proc * n)
        self.assertFalse(list(self.vault.rglob("*.tmp")))                   # nenhum temporário largado

    def test_dois_processos_respondendo_a_mesma_pendencia_enviam_uma_vez(self):
        """(b) painel + notificação (ou dois cliques) em processos diferentes → um envio só."""
        pid = "20261020-carlos-concorrente"
        self.gravar_pendencias([{
            "id": pid, "tipo": "whatsapp", "status": "aguardando", "titulo": "Carlos: orçamento",
            "evento": {"resumo": "orçamento"}, "email": None, "arquivos": {"md": None, "mp3": None},
            "whatsapp": {"remote_jid": "5514988887777@s.whatsapp.net", "numero": "5514988887777", "contato": "Carlos",
                         "grupo": False, "key": None, "rascunho": "Recebi!"},
            "respondido_em": None, "resposta": None, "resposta_texto": None, "nota": None,
        }])
        barreira, saida = CTX.Barrier(2), CTX.Queue()
        for _ in range(2):
            self.filho(_filho_responder, pid, barreira, saida)
        res = sorted(saida.get(timeout=ESPERA) for _ in range(2))
        self.assertEqual([r[0] for r in res], ["ja_resolvida", "respondida"], res)
        self.assertEqual(res[1][1:], ("respondida", True))                   # envio simulado
        self.assertEqual(res[0][1], "respondida")
        self.assertEqual(res[0][2], "teste")                                 # caminho = respondido_via desconhecido
        self.assertEqual(len(self.enviados()), 1, self.enviados())           # exatamente um envio
        p = core.obter_pendencia(pid)
        self.assertEqual((p["status"], p["respondido_via"]), ("respondida", "teste"))
        self.assertTrue(p["whatsapp"]["envio"]["ok"]); self.assertTrue(p["whatsapp"]["envio"]["simulado"])
        self.assertIn("resposta do processo", p["resposta_texto"])
        self.assertEqual(p["whatsapp"]["rascunho"], "Recebi!")

    def test_leitor_nunca_ve_arquivo_pela_metade(self):
        """(c) outro processo regrava pendencias.yml sem parar; quem lê sempre vê um arquivo inteiro."""
        total = 300
        pronto, parar, saida = CTX.Event(), CTX.Event(), CTX.Queue()
        self.filho(_filho_regravar, total, pronto, parar, saida)
        self.assertTrue(pronto.wait(ESPERA))
        leituras, fim = 0, time.monotonic() + 1.5
        try:
            while time.monotonic() < fim:
                dados = yaml.safe_load(core.pend_file().read_text(encoding="utf-8"))
                self.assertIsInstance(dados, dict)
                self.assertEqual(len(dados["pendencias"]), total)
                self.assertEqual(len(core._load_pend()["pendencias"]), total)   # nunca "YAML ilegível → vazio"
                leituras += 1
        finally:
            parar.set()
        r = saida.get(timeout=ESPERA)
        self.assertEqual(r[0], "ok", r)
        self.assertGreater(r[1], 5)            # o outro processo regravou várias vezes durante as leituras
        self.assertGreater(leituras, 5)


class TestTrava(Base):
    def test_reentrante_na_mesma_thread_e_exclusiva_entre_threads(self):
        """(d)"""
        f = core.pend_file()
        with core.trava_arquivo(f):
            with core.trava_pendencias():          # mesma trava de novo, mesma thread: não trava
                with core.trava_arquivo(self.vault / "tasks.yml"):
                    core._save_pend({"pendencias": []})
            # outra thread não entra enquanto esta segura (nem com o nível já de volta a 1)
            erro: list = []

            def tentar():
                try:
                    with core.trava_arquivo(f, timeout=0.2):
                        erro.append("entrou")
                except TimeoutError as e:
                    erro.append(e)
            t = threading.Thread(target=tentar)
            t.start(); t.join(10)
            self.assertIsInstance(erro[0], TimeoutError)
        # solta tudo: a trava volta a ficar livre (nível 0, flock solto)
        self.assertEqual(core._TRAVAS[str(core._arquivo_trava(f).resolve())]["nivel"], 0)

        # exclusão mútua: incrementos ler-mudar-gravar de várias threads não se perdem
        contador = self.vault / "contador.txt"
        contador.write_text("0")

        def somar():
            for _ in range(25):
                with core.trava_arquivo(contador):
                    v = int(contador.read_text())
                    time.sleep(0.0005)
                    core.gravar_atomico(contador, str(v + 1))
        ts = [threading.Thread(target=somar) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(contador.read_text(), "100")

    def test_trava_segura_por_esta_thread_bloqueia_outro_processo(self):
        f = core.pend_file()
        with core.trava_arquivo(f), core.trava_arquivo(f):   # reentrada também segura o flock
            saida = CTX.Queue()
            self.filho(_filho_tentar, str(f), 0.3, saida)
            r = saida.get(timeout=ESPERA)
            self.assertEqual(r[0], "timeout", r)
        saida = CTX.Queue()
        self.filho(_filho_tentar, str(f), 5, saida)
        self.assertEqual(saida.get(timeout=ESPERA)[0], "pegou")

    def test_timeout_quando_outro_processo_segura(self):
        """(e) outro processo segura a trava → TimeoutError legível depois de `timeout`; solto, volta a funcionar."""
        f = core.pend_file()
        pronto, soltar = CTX.Event(), CTX.Event()
        p = self.filho(_filho_segurar, str(f), pronto, soltar)
        self.assertTrue(pronto.wait(ESPERA))
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError) as cm:
            with core.trava_arquivo(f, timeout=0.3):
                pass
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("pendencias.yml", str(cm.exception))
        # escrita de alto nível também respeita a trava (e não grava nada)
        with mock.patch.object(core, "trava_pendencias", lambda: core.trava_arquivo(f, timeout=0.2)):
            with self.assertRaises(TimeoutError):
                core.responder("qualquer", "vou")
        soltar.set()
        p.join(ESPERA)
        with core.trava_arquivo(f, timeout=5):     # nada ficou preso do lado de cá
            pass
        self.assertFalse(core.pend_file().exists())


class TestJaResolvida(Base):
    def _email(self, pid, status, **extra):
        return {"id": pid, "tipo": "evento", "status": status, "titulo": f"Evento {pid}",
                "evento": {"data_inicio": "2030-01-01"}, "arquivos": {"md": None, "mp3": None},
                "respondido_em": None, "resposta": None, "nota": None, **extra}

    def test_responder_so_se_aguardando_nao_muda_pendencia_resolvida(self):
        """(f)"""
        self.gravar_pendencias([self._email("p-vou", "vou", respondido_via="terminal"),
                                self._email("p-notif", "nao_vou", nota="via notificação"),
                                self._email("p-exp", "expirada"),
                                self._email("p-cel", "respondida_manual"),
                                self._email("p-velha", "ignorada"),
                                self._email("p-ok", "aguardando")])
        antes = core.pend_file().read_bytes()
        casos = {"p-vou": "terminal", "p-notif": "notificação no PC",
                 "p-exp": "expiração automática (o evento já passou)", "p-cel": "resposta direta pelo celular",
                 "p-velha": "outro caminho (terminal, notificação ou chat)"}
        for pid, caminho in casos.items():
            with self.subTest(pid), self.assertRaises(core.PendenciaJaResolvida) as cm:
                core.responder(pid, "nao_vou", origem="via painel", so_se_aguardando=True)
            self.assertEqual(cm.exception.caminho, caminho)
            self.assertEqual(cm.exception.pid, pid)
            self.assertIn("já está com status", str(cm.exception))
        self.assertEqual(core.pend_file().read_bytes(), antes)              # nada mudou
        self.assertFalse((self.vault / "tasks.yml").exists())

        # aguardando: responde, grava respondido_via e cria a tarefa
        e = core.responder("p-ok", "vou", origem="via painel", so_se_aguardando=True)
        self.assertEqual((e["status"], e["respondido_via"], e["task_id"]), ("vou", "painel", 1))
        self.assertEqual(core.caminho_resolucao(core.obter_pendencia("p-ok")), "Painel do Alfred")
        # sem so_se_aguardando o comportamento antigo continua (CLI pode mudar de ideia)
        e = core.responder("p-ok", "nao_vou", "mudei de ideia", origem="via terminal")
        self.assertEqual((e["status"], e["respondido_via"], e["nota"]), ("nao_vou", "terminal", "mudei de ideia"))

    def test_via_e_caminhos(self):
        self.assertEqual(core._via("via notificação no PC"), "notificação no PC")
        self.assertEqual(core._via("Via API"), "API")
        self.assertIsNone(core._via("")); self.assertIsNone(core._via("via "))
        self.assertEqual(core._via("celular"), "celular")
        for via, txt in (("celular", "resposta direta pelo celular"), ("WhatsApp", "resposta citada no WhatsApp"),
                         ("API", "API (N8N, chat ou extensão)"), ("terminal", "terminal"),
                         ("notificação no PC", "notificação no PC"), ("painel", "Painel do Alfred"),
                         ("chat", "chat do Open WebUI"), ("outra coisa", "outra coisa")):
            self.assertEqual(core.caminho_resolucao({"status": "respondida", "respondido_via": via}), txt)


class TestToolsOpenWebUI(Base):
    """As tools rodam no container (autocontidas) e gravam direto no vault: mesma trava, mesmo .lock."""

    def tool(self, nome: str):
        import importlib.util
        spec = importlib.util.spec_from_file_location(nome, INBOX.parent.parent / "tools" / f"{nome}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        t = mod.Tools()
        t.valves.vault_path = str(self.vault)
        return t

    def test_tool_e_core_disputam_o_mesmo_lock(self):
        pend = self.tool("alfred_pendencias")
        tasks = self.tool("alfred_tasks")
        f = core.pend_file()
        with pend._trava(f):
            with self.assertRaises(TimeoutError):
                with core.trava_arquivo(f, timeout=0.2):
                    pass
        with core.trava_arquivo(self.vault / "tasks.yml"):
            with self.assertRaises(TimeoutError):
                with tasks._trava(timeout=0.2):
                    pass
        self.assertTrue((self.vault / "pendencias" / ".pendencias.yml.lock").exists())
        self.assertTrue((self.vault / ".tasks.yml.lock").exists())

    def test_tool_responde_email_com_respondido_via_chat(self):
        pend = self.tool("alfred_pendencias")
        tasks = self.tool("alfred_tasks")
        self.gravar_pendencias([{"id": "p-chat", "tipo": "evento", "status": "aguardando", "titulo": "Meetup",
                                 "evento": {"data_inicio": "2030-01-01"}, "arquivos": {"md": None, "mp3": None}}])
        core._add_task("do host", None, ["x"])
        self.assertIn("Tarefa #2", tasks.add_task("do chat"))
        self.assertIn("VOU", pend.responder_pendencia("p-chat", "vou"))
        p = core.obter_pendencia("p-chat")
        self.assertEqual((p["status"], p["respondido_via"], p["task_id"]), ("vou", "chat", 3))
        self.assertEqual(core.caminho_resolucao(p), "chat do Open WebUI")
        self.assertIn("já está com status", pend.responder_pendencia("p-chat", "nao_vou"))
        self.assertEqual([t["id"] for t in yaml.safe_load((self.vault / "tasks.yml").read_text())["tasks"]], [1, 2, 3])
        self.assertFalse(list(self.vault.rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
