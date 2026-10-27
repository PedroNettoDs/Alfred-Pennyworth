"""
Regressões dos testes funcionais (trilha WhatsApp / Painel / vigia / CLI):
datas inexistentes no "Agendar", lembrete do Agendar disparado pela thread da fila da API, reagendar sem
tarefa duplicada, agrupamento de áudio não transcrito, Evolution sem instância (404) / chave recusada,
lote com inbox inexistente, plural com 1 e-mail, "Próximos na agenda" sem itens desfeitos, fallback do
aviso do vigia e mensagens do CLI (qrcode, perguntar, responder, podcast).

    cd services/inbox
    venv/bin/python -m unittest tests.test_correcoes_whatsapp -v

Offline: reaproveita o ambiente de tests.test_painel (vault temporário, LLM/TTS mockados, EVOLUTION_URL vazio
+ WHATSAPP_DRY_RUN=true — nada sai da máquina).
"""

import importlib.util
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from tests import test_painel as tp

core, whatsapp, painel, agenda, limpeza, lib_whatsapp = tp.core, tp.whatsapp, tp.painel, tp.agenda, tp.limpeza, tp.lib_whatsapp

TEXTO_AUDIO = "Oi Pedro, aqui é o Carlos da Fatec. Consegue mandar o orçamento até sexta?"


def _fake_transcrever(dados, mimetype, max_segundos=600):
    return {"texto": TEXTO_AUDIO, "metodo": "teste", "duracao_s": 5}


def _cli():
    spec = importlib.util.spec_from_file_location("inbox_assistant_corr", tp.INBOX / "inbox-assistant.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════
#  1. interpretar_quando
# ══════════════════════════════════════════════════════════════

class TestInterpretarQuando(unittest.TestCase):
    AGORA = datetime(2026, 10, 26, 18, 40).astimezone()

    def q(self, txt):
        r = whatsapp.interpretar_quando(txt, self.AGORA)
        return r and r[0].strftime("%d/%m/%Y %H:%M")

    def test_data_inexistente_devolve_none(self):
        for txt in ("31/02", "12/13", "29/02", "32/10 10h", "0/0", "10/13 14:00", "31/02 10:00", "dia 0", "dia 32"):
            self.assertIsNone(whatsapp.interpretar_quando(txt, self.AGORA), txt)
        self.assertEqual(self.q("29/02/2028"), "29/02/2028 09:00")      # com ano bissexto explícito, vale

    def test_dia_n_e_proximo_dia_n(self):
        self.assertEqual(self.q("dia 30 às 14h"), "30/10/2026 14:00")
        self.assertEqual(self.q("dia 26 19h"), "26/10/2026 19:00")      # hoje, ainda não passou
        self.assertEqual(self.q("dia 26 18h"), "26/11/2026 18:00")      # hoje, já passou → mês que vem
        self.assertEqual(self.q("dia 3"), "03/11/2026 09:00")
        agora = datetime(2026, 11, 5, 10, 0).astimezone()               # novembro não tem 31 → dezembro
        self.assertEqual(whatsapp.interpretar_quando("dia 31", agora)[0].strftime("%d/%m/%Y"), "31/12/2026")

    def test_so_hora_passada_vai_para_amanha(self):
        self.assertEqual(self.q("16:00"), "27/10/2026 16:00")
        self.assertEqual(self.q("10h"), "27/10/2026 10:00")
        self.assertEqual(self.q("20:00"), "26/10/2026 20:00")
        self.assertEqual(self.q("hoje 10h"), "26/10/2026 10:00")        # "hoje" explícito continua hoje


# ══════════════════════════════════════════════════════════════
#  1/2. Painel "Agendar": data inválida, reagendar, lembrete pela thread da fila
# ══════════════════════════════════════════════════════════════

class TestAgendar(tp.Base):
    def test_data_inexistente_e_400_nao_500(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/agendar"
        for txt in ("31/02 10:00", "29/02", "32/10", "10/13 14:00", "0/0"):
            r = self.acao(cli, url, {"quando": txt}, csrf=token)
            self.assertEqual((r.status_code, r.json().get("campo")), (400, "quando"), txt)
        with mock.patch.object(whatsapp, "interpretar_quando", side_effect=ValueError("x")):
            r = self.acao(cli, url, {"quando": "amanhã 10h"}, csrf=token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(core.obter_pendencia(p["id"])["whatsapp"].get("lembrar_em"), None)

    def test_reagendar_reaproveita_a_tarefa(self):
        import yaml
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/agendar"
        r1 = self.acao(cli, url, {"quando": "amanhã 14h"}, csrf=token).json()
        r2 = self.acao(cli, url, {"quando": "depois de amanhã 10h"}, csrf=token).json()
        self.assertEqual(r1["task_id"], r2["task_id"])
        tasks = yaml.safe_load((core.vault() / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]["title"].endswith("— 10:00"), tasks[0]["title"])
        self.assertEqual(tasks[0]["due_date"], (core._now() + timedelta(days=2)).strftime("%Y-%m-%d"))
        # tarefa concluída no meio → reagendar cria outra
        tasks[0]["status"] = "done"
        (core.vault() / "tasks.yml").write_text(yaml.dump({"tasks": tasks}, allow_unicode=True), encoding="utf-8")
        r3 = self.acao(cli, url, {"quando": "amanhã 9h"}, csrf=token).json()
        self.assertNotEqual(r3["task_id"], r1["task_id"])

    def test_lembrete_dispara_na_thread_da_fila_uma_vez(self):
        p = self.criar_wa()
        chamadas = []
        with mock.patch.object(core, "_iniciar_aguardador", lambda cmd, pid: chamadas.append((cmd, pid))), \
             mock.patch.object(core, "_tem_gui", lambda: True):
            futuro = core._now() + timedelta(hours=2)
            whatsapp.agendar_pendencia(p["id"], futuro)
            whatsapp.processar_fila()
            self.assertEqual(chamadas, [])                                  # ainda não venceu
            passado = core._now() - timedelta(minutes=1)
            whatsapp.agendar_pendencia(p["id"], passado)                     # reagendado para agora
            whatsapp.processar_fila()
            whatsapp.processar_fila()                                        # 2ª volta não repete
            self.assertEqual(chamadas, [("_aguardar-whatsapp", p["id"])])
            wa = core.obter_pendencia(p["id"])["whatsapp"]
            self.assertEqual(wa["lembrete_disparado_em"], wa["lembrar_em"])
            # o aguardador que dormiu até o horário não mostra o mesmo lembrete de novo
            self.assertFalse(whatsapp.marcar_lembrete(p["id"], wa["lembrar_em"]))
            # pendência resolvida antes do horário: nada dispara
            whatsapp.agendar_pendencia(p["id"], passado - timedelta(minutes=5))
            whatsapp.responder_texto(p["id"], "ignorar")
            whatsapp.processar_fila()
            self.assertEqual(len(chamadas), 1)

    def test_aguardador_so_mostra_se_reservar_o_lembrete(self):
        p = self.criar_wa()
        cli = _cli()
        menus = []

        def menu(*a, **k):
            menus.append(1)
            return "agenda" if len(menus) == 1 else ""

        def dormir(iso, pid):   # enquanto "dorme", a thread da API dispara o lembrete
            whatsapp.disparar_lembretes(core._now() + timedelta(days=400))

        with mock.patch.object(cli, "_menu_whatsapp_gui", menu), \
             mock.patch.object(cli, "_caixa_texto_gui", lambda *a, **k: "amanhã 10h"), \
             mock.patch.object(cli, "_dormir_ate", dormir), \
             mock.patch.object(cli.shutil, "which", lambda nome: None):
            self.assertEqual(cli.cmd_aguardar_whatsapp(mock.Mock(id=p["id"])), 0)
        self.assertEqual(len(menus), 1)    # saiu sem mostrar de novo: a notificação nova é da API

    def test_agenda_da_notificacao_com_data_inexistente_pergunta_de_novo(self):
        p = self.criar_wa()
        cli = _cli()
        caixas = ["31/02 10h", None]
        with mock.patch.object(cli, "_caixa_texto_gui", lambda *a, **k: caixas.pop(0)):
            self.assertIsNone(cli._agendar_gui(core.obter_pendencia(p["id"])))
        self.assertEqual(caixas, [])


# ══════════════════════════════════════════════════════════════
#  3. Agrupar áudio não transcrito
# ══════════════════════════════════════════════════════════════

class TestAgruparAudio(tp.Base):
    def test_key_e_quoted_da_mesma_mensagem_e_erro_registrado(self):
        with mock.patch.object(whatsapp, "transcrever_audio", _fake_transcrever):
            p = self.criar_wa()
            cli = tp.TestClient(tp.main.app)
            h = {"apikey": "CHAVE-DE-TESTE"}
            d2 = cli.post("/whatsapp/webhook", params={"enviar": "false"}, json=tp.payload("evolution_audio.json"),
                          headers=h).json()
            self.assertEqual(d2["acao"], "agrupada", d2)
            wa = core.obter_pendencia(p["id"])["whatsapp"]
            self.assertEqual(wa["quoted_message"], {"conversation": TEXTO_AUDIO})
            d3 = cli.post("/whatsapp/webhook", params={"enviar": "false"},
                          json=tp.payload("evolution_v237_audio_sem_base64.json"), headers=h).json()
        self.assertEqual(d3["acao"], "agrupada", d3)
        wa = core.obter_pendencia(p["id"])["whatsapp"]
        self.assertEqual(wa["key"]["id"], "A56164FA6A523054ADF7EEDAF6881119")
        self.assertIsNone(wa["quoted_message"])                   # nada de citar o texto de outra mensagem
        self.assertIn("download falhou", wa["mensagens"][-1]["erro"])
        self.assertIn("áudio não transcrito", d3["notificacao"]["texto"])
        md = (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8")
        self.assertIn("(audio): áudio não transcrito: áudio sem base64", md)


# ══════════════════════════════════════════════════════════════
#  4. Evolution: instância inexistente (404), chave recusada, sem resposta
# ══════════════════════════════════════════════════════════════

class TestConexaoWhatsApp(tp.Base):
    def status_com(self, est):
        with mock.patch.dict(os.environ, {"EVOLUTION_URL": "http://127.0.0.1:9"}), \
             mock.patch.object(lib_whatsapp, "estado_instancia", lambda timeout=30: est):
            st = whatsapp.status(timeout=1)
        return st, painel._conexao_whatsapp(st)

    def test_404_vira_sem_instancia(self):
        st, c = self.status_com({"ok": False, "status": 404, "resposta": {"response": {"message": ["does not exist"]}}})
        self.assertEqual(st["estado_instancia"], "inexistente")
        self.assertEqual((c["tom"], c["rotulo"], c["estado"]), ("erro", "Sem instância", "erro"))
        self.assertIn("./run.sh whatsapp qrcode", c["detalhe"])

    def test_401_403_pede_conferir_a_chave(self):
        for codigo in (401, 403):
            _, c = self.status_com({"ok": False, "status": codigo})
            self.assertEqual(c["tom"], "erro")
            self.assertIn("EVOLUTION_API_KEY", c["detalhe"])
            self.assertNotIn("sem resposta", c["detalhe"])

    def test_sem_resposta_so_para_rede(self):
        _, c = self.status_com({"ok": False, "erro": "[Errno 111] Connection refused"})
        self.assertEqual(c["rotulo"], "Indisponível")
        self.assertIn("sem resposta", c["detalhe"])
        _, c = self.status_com({"ok": False, "status": 500})
        self.assertNotIn("sem resposta", c["detalhe"])


# ══════════════════════════════════════════════════════════════
#  5. Painel: lote com inbox inexistente, plural, Início sem "desfazer"
# ══════════════════════════════════════════════════════════════

class TestPainelLimpezaEInicio(tp.Base):
    def propor(self, n):
        with mock.patch.object(limpeza, "candidatos", lambda *a, **k: tp._candidatos(n)):
            return limpeza.propor(notificar=False)

    def test_inbox_inexistente_da_400(self):
        lote = self.propor(2)
        cli = self.entrar()
        with mock.patch.object(limpeza, "aprovar", side_effect=KeyError("Inbox não encontrada: pedro")):
            r = self.acao(cli, f"/painel/api/limpeza/{lote['id']}/aprovar", {})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Inbox não encontrada: pedro", r.json()["erro"])

    def test_plural_com_um_email(self):
        lote = self.propor(1)
        cli = self.entrar()
        html = self.guardar(cli.get("/painel/limpeza")).text
        self.assertIn("Ver o e-mail<", html)
        self.assertIn('data-dialogo-mensagem="O e-mail irá para a Lixeira da conta.', html)
        self.assertNotIn("Os 1 e-mails", html)
        with mock.patch.object(limpeza, "aprovar", lambda lid, nota: {"status": "aprovado", "total": 1,
                                                                      "removidos_na_aprovacao": ["x"]}):
            r = self.acao(cli, f"/painel/api/limpeza/{lote['id']}/aprovar", {})
        self.assertEqual(r.json()["mensagem"], "Lote aprovado: 1 e-mail irá para a Lixeira quando a extensão do "
                                               "Thunderbird passar. 1 saiu do lote na revalidação.")

    def test_inicio_nao_lista_item_desfeito(self):
        amanha = core._now().replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
        a = agenda.adicionar("Dentista Dr Silva", amanha, notificar=False)
        agenda.adicionar("Reunião Attano", amanha, notificar=False)
        agenda.registrar_resultado(a["id"], "criar", True, "Agenda")
        agenda.desfazer(a["id"])
        self.assertEqual(agenda.obter(a["id"])["status"], "desfazer")
        html = self.guardar(self.entrar().get("/painel/parcial/inicio")).text
        self.assertIn("Reunião Attano", html)
        self.assertNotIn("Dentista Dr Silva", html)


# ══════════════════════════════════════════════════════════════
#  6. vigia: aviso cai em core._notify se a notificação D-Bus falhar
# ══════════════════════════════════════════════════════════════

class TestVigiaFallback(unittest.TestCase):
    def rodar(self, run):
        import vigia
        avisos = []
        with mock.patch.object(vigia.subprocess, "run", run), \
             mock.patch.object(vigia.shutil, "which", lambda *a, **k: "/usr/bin/python3"), \
             mock.patch.object(vigia.core, "_notify", lambda t, c: avisos.append(t)):
            self.assertEqual(vigia.aguardar_qr("HTTP 404"), 0)
        return avisos

    def test_rc_diferente_de_zero_e_excecao_viram_aviso_simples(self):
        rc2 = lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "sem barramento")
        self.assertEqual(self.rodar(rc2), ["Alfred — WhatsApp desconectado"])

        def explode(*a, **k):
            raise OSError("sem python")
        self.assertEqual(self.rodar(explode), ["Alfred — WhatsApp desconectado"])

    def test_notificacao_com_botoes_nao_duplica(self):
        depois = lambda *a, **k: subprocess.CompletedProcess(a, 0, "depois\n", "")
        self.assertEqual(self.rodar(depois), [])


# ══════════════════════════════════════════════════════════════
#  7. CLI: whatsapp qrcode, perguntar (EOF na nota), responder, podcast
# ══════════════════════════════════════════════════════════════

class TestCLI(tp.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cli = _cli()

    def rodar(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["inbox-assistant.py", *argv]), redirect_stdout(out):
            rc = self.cli.main()
        return rc, out.getvalue()

    def qrcode(self, est):
        criadas = []
        with mock.patch.dict(os.environ, {"EVOLUTION_URL": "http://127.0.0.1:9"}), \
             mock.patch.object(lib_whatsapp, "estado_instancia", lambda timeout=30: est), \
             mock.patch.object(lib_whatsapp, "criar_instancia", lambda *a, **k: criadas.append(1) or {"ok": False, "erro": "x"}):
            rc, out = self.rodar("whatsapp", "qrcode")
        return rc, out, criadas

    def test_qrcode_so_cria_instancia_com_404(self):
        rc, out, criadas = self.qrcode({"ok": False, "status": 401})
        self.assertEqual((rc, criadas), (1, []))
        self.assertIn("EVOLUTION_API_KEY", out)
        rc, out, criadas = self.qrcode({"ok": False, "erro": "[Errno 111] Connection refused"})
        self.assertEqual((rc, criadas), (1, []))
        self.assertIn("inacessível", out)
        self.assertNotIn("não existe", out)
        rc, out, criadas = self.qrcode({"ok": False, "status": 404})
        self.assertEqual((rc, criadas), (1, [1]))
        self.assertIn("não existe ainda — criando", out)

    def test_perguntar_eof_na_nota_grava_a_resposta(self):
        e = self.criar_email()
        respostas = iter(["vou"])

        def entrada(prompt=""):
            try:
                return next(respostas)
            except StopIteration:
                raise EOFError
        with mock.patch("builtins.input", entrada):
            rc, out = self.rodar("perguntar")
        self.assertEqual(rc, 0)
        self.assertEqual(core.obter_pendencia(e["id"])["status"], "vou")
        self.assertIn("anotado: VOU", out)

    def test_responder_so_diz_criada_quando_criou(self):
        e = self.criar_email()
        p = core.obter_pendencia(e["id"])
        with mock.patch.object(core, "obter_pendencia", lambda pid: {**p, "task_id": 7}), \
             mock.patch.object(core, "responder", lambda *a, **k: {**p, "status": "vou", "task_id": 7}):
            rc, out = self.rodar("responder", e["id"], "vou")
        self.assertEqual(rc, 0)
        self.assertNotIn("criada", out)
        with mock.patch.object(core, "responder", lambda *a, **k: {**p, "status": "vou", "task_id": 8}):
            self.assertIn("tarefa #8 criada", self.rodar("responder", e["id"], "vou")[1])
        with mock.patch.object(core, "responder",
                               lambda *a, **k: {**p, "status": "vou", "task_id": 8, "_task_criada": False}):
            self.assertNotIn("criada", self.rodar("responder", e["id"], "vou")[1])

    def test_podcast_semana_ou_dias_invalidos_sai_2_sem_traceback(self):
        import semanal
        with mock.patch.object(semanal, "cmd_semanal",
                               side_effect=ValueError("Semana inválida: 'lixo' (use AAAA-Www, ex.: 2026-W43)")):
            rc, out = self.rodar("podcast", "--semana", "lixo", "--dry-run")
        self.assertEqual(rc, 2)
        self.assertIn("Semana inválida", out)
        with mock.patch.object(semanal, "cmd_semanal", side_effect=AssertionError("não deveria chamar")):
            for dias in ("0", "-2"):
                rc, out = self.rodar("semanal", "--dias", dias, "--dry-run")
                self.assertEqual(rc, 2, out)
                self.assertIn("--dias", out)


if __name__ == "__main__":
    unittest.main()
