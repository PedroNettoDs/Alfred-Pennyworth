"""
Testes da agenda (compromissos → calendário do Thunderbird), do vigia/limpeza de áudios de resumo
e das notas que Pedro manda para si mesmo no WhatsApp.

    cd services/inbox && venv/bin/python -m unittest tests.test_agenda -v

Offline: o LLM é mockado, o vault é temporário, nada chega à Evolution API nem ao Thunderbird.
"""

import json
import os
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

os.environ.setdefault("SHELL_EXECUTOR_TOKEN", "token-de-teste")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

import core      # noqa: E402
import agenda    # noqa: E402
import whatsapp  # noqa: E402
from _shared import lib_whatsapp  # noqa: E402


def _amanha(h=15, m=0):
    d = core._now() + timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="alfred-agenda-test-"))
        (cls.tmp / "vault").mkdir()
        cls.env = mock.patch.dict(os.environ, {"VAULT_ALFRED": str(cls.tmp / "vault"), "INBOX_AUTOPLAY": "false",
                                               "EVOLUTION_URL": "", "WHATSAPP_DRY_RUN": "true",
                                               "WHATSAPP_NUMERO_PEDRO": "5514999990000"})
        cls.env.start()
        cls.notificados = []
        cls.patches = [
            mock.patch.object(core, "_notify", lambda *a, **k: None),
            mock.patch.object(core, "_iniciar_aguardador", lambda c, i: cls.notificados.append((c, i))),
            mock.patch.object(lib_whatsapp, "ENVIADOS_FILE", cls.tmp / "enviados.jsonl"),
            mock.patch.object(lib_whatsapp, "IDS_ENVIADOS_FILE", cls.tmp / "ids_enviados.json"),
            mock.patch.object(whatsapp, "SEEN_FILE", cls.tmp / "seen.json"),
            mock.patch.object(whatsapp, "FILA_FILE", cls.tmp / "fila.json"),
        ]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for d in ("agenda", "pendencias"):
            shutil.rmtree(self.tmp / "vault" / d, ignore_errors=True)
        for f in ("tasks.yml",):
            (self.tmp / "vault" / f).unlink(missing_ok=True)
        for f in self.tmp.glob("*.json*"):
            f.unlink()
        self.notificados.clear()


class TestAgendaFila(Base):
    def test_adicionar_dedup_e_notifica(self):
        a = agenda.adicionar("Reunião com Attano Tech", _amanha(14), local="Bauru")
        self.assertTrue(a["_novo"])
        self.assertEqual(a["status"], "pendente")
        self.assertEqual(self.notificados, [("_aguardar-agenda", a["id"])])
        b = agenda.adicionar("reunião com attano tech!", _amanha(16))
        self.assertFalse(b["_novo"])
        self.assertEqual(b["id"], a["id"])
        self.assertEqual(len(agenda.listar()), 1)

    def test_tarefa_cria_item_em_tasks(self):
        a = agenda.adicionar("Pagar boleto da Vivo", _amanha(10), tipo="tarefa", notificar=False)
        self.assertIsNotNone(a["task_id"])
        self.assertIn("Pagar boleto da Vivo", (self.tmp / "vault" / "tasks.yml").read_text(encoding="utf-8"))

    def test_fila_resultado_e_desfazer(self):
        a = agenda.adicionar("Consulta no dentista", _amanha(9), notificar=False)
        fila = agenda.fila_extensao()
        self.assertEqual([(i["id"], i["acao"]) for i in fila], [(a["id"], "criar")])
        self.assertIn("BEGIN:VEVENT", fila[0]["ics"])
        self.assertEqual(agenda.obter(a["id"])["status"], "em_execucao")
        self.assertEqual(agenda.fila_extensao(), [])            # não entrega duas vezes
        agenda.registrar_resultado(a["id"], "criar", True, calendario="Agenda")
        self.assertEqual(agenda.obter(a["id"])["status"], "no_calendario")
        agenda.desfazer(a["id"])
        fila = agenda.fila_extensao()
        self.assertEqual([(i["id"], i["acao"]) for i in fila], [(a["id"], "remover")])
        agenda.registrar_resultado(a["id"], "remover", True)
        self.assertEqual(agenda.obter(a["id"])["status"], "desfeito")

    def test_desfazer_antes_de_ir_para_o_calendario_cancela(self):
        a = agenda.adicionar("Prova de cálculo", _amanha(19), notificar=False)
        self.assertEqual(agenda.desfazer(a["id"])["status"], "cancelado")
        self.assertEqual(agenda.fila_extensao(), [])

    def test_erro_conta_tentativas(self):
        a = agenda.adicionar("Aula aberta", _amanha(20), notificar=False)
        for _ in range(agenda.MAX_TENTATIVAS):
            agenda.fila_extensao()
            agenda.registrar_resultado(a["id"], "criar", False, erro="calendário somente leitura")
        self.assertEqual(agenda.obter(a["id"])["tentativas"], agenda.MAX_TENTATIVAS)
        self.assertEqual(agenda.fila_extensao(), [])


class TestICS(Base):
    def test_vevent_com_hora_alarme_e_dobra(self):
        a = agenda.adicionar("Reunião " + "muito longa " * 10, _amanha(14, 30), local="Rua X, 10; sala 2",
                             descricao="linha 1\nlinha 2", notificar=False)
        ics = agenda.vcalendar([a])
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertRegex(ics, r"DTSTART:\d{8}T\d{6}Z")
        self.assertIn("TRIGGER:-PT30M", ics)
        self.assertIn("LOCATION:Rua X\\, 10\\; sala 2", ics)
        self.assertIn("linha 1\\nlinha 2", ics)
        for linha in ics.split("\r\n"):
            self.assertLessEqual(len(linha.encode("utf-8")), 75)
        inicio_utc = _amanha(14, 30).astimezone(__import__("datetime").timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.assertIn(f"DTSTART:{inicio_utc}", ics)

    def test_dia_inteiro(self):
        a = agenda.adicionar("Aniversário da Ana", _amanha(), dia_inteiro=True, notificar=False)
        ics = agenda.vcalendar([a])
        self.assertIn(f"DTSTART;VALUE=DATE:{_amanha():%Y%m%d}", ics)
        self.assertNotIn("VALARM", ics)

    def test_feed_so_pendentes(self):
        a = agenda.adicionar("A", _amanha(8), notificar=False)
        b = agenda.adicionar("B", _amanha(9), notificar=False)
        agenda.fila_extensao()
        agenda.registrar_resultado(b["id"], "criar", True)
        feed = agenda.feed_ics()
        self.assertIn(a["uid"], feed)
        self.assertNotIn(b["uid"], feed)


class TestDeteccao(Base):
    def _det(self, resposta, texto="reunião amanhã às 14h"):
        with mock.patch.object(agenda, "ollama_generate_json", lambda *a, **k: resposta):
            return agenda.detectar_compromisso(texto)

    def test_prefiltro_evita_llm(self):
        chamado = []
        with mock.patch.object(agenda, "ollama_generate_json", lambda *a, **k: chamado.append(1)):
            self.assertIsNone(agenda.detectar_compromisso("valeu, obrigado!"))
        self.assertEqual(chamado, [])

    def test_compromisso_valido(self):
        d = _amanha()
        c = self._det({"existe": True, "tipo": "evento", "titulo": "Reunião com Carlos", "data": d.strftime("%Y-%m-%d"),
                       "hora": "14:00", "hora_fim": "15:30", "local": "Fatec", "confianca": 0.9, "trecho": "x"})
        self.assertEqual(c["inicio"].hour, 14)
        self.assertFalse(c["dia_inteiro"])
        self.assertEqual((c["fim"] - c["inicio"]).seconds, 90 * 60)

    def test_descarta_passado_baixa_confianca_e_sem_data(self):
        ontem = (core._now() - timedelta(days=1)).strftime("%Y-%m-%d")
        base = {"existe": True, "tipo": "evento", "titulo": "X", "hora": "", "hora_fim": "", "local": "", "trecho": ""}
        self.assertIsNone(self._det({**base, "data": ontem, "confianca": 0.9}))
        self.assertIsNone(self._det({**base, "data": _amanha().strftime("%Y-%m-%d"), "confianca": 0.4}))
        self.assertIsNone(self._det({**base, "data": "", "confianca": 0.9}))
        self.assertIsNone(self._det({**base, "existe": False, "data": _amanha().strftime("%Y-%m-%d"), "confianca": 0.9}))


class TestEmailVou(Base):
    def test_vou_com_data_vai_para_agenda(self):
        core.pend_dir()
        data = {"pendencias": [{
            "id": "20261020-meetup", "tipo": "evento", "status": "aguardando", "titulo": "Meetup DevOps",
            "evento": {"data_inicio": _amanha(19).strftime("%Y-%m-%dT%H:%M"), "data_fim": None, "local": "SP",
                       "online": False, "link": "https://exemplo.com", "resumo": "meetup"},
            "email": {"subject": "Convite", "from": "a@b.com"}, "arquivos": {"md": None, "mp3": None}}]}
        core._save_pend(data)
        core.responder("20261020-meetup", "vou")
        itens = agenda.listar()
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["origem"]["canal"], "email")
        self.assertEqual(itens[0]["local"], "SP")


class TestWhatsApp(Base):
    def _payload(self, texto, from_me, numero="5514988887777", dono="5514999990000", mid="MSG1"):
        return {"event": "messages.upsert", "instance": "alfred", "sender": f"{dono}@s.whatsapp.net",
                "data": {"key": {"remoteJid": f"{numero}@s.whatsapp.net", "fromMe": from_me, "id": mid},
                         "pushName": "Pedro" if from_me else "Carlos", "message": {"conversation": texto},
                         "messageType": "conversation", "messageTimestamp": int(datetime.now().timestamp())}}

    def _det_ok(self, *a, **k):
        return {"existe": True, "tipo": "lembrete", "titulo": "Pagar o boleto", "data": _amanha().strftime("%Y-%m-%d"),
                "hora": "10:00", "hora_fim": "", "local": "", "confianca": 0.95, "trecho": "pagar o boleto"}

    def test_nota_para_mim_vira_agenda_e_confirma(self):
        with mock.patch.object(agenda, "ollama_generate_json", self._det_ok):
            r = whatsapp.processar_mensagem_whatsapp(
                self._payload("me lembra de pagar o boleto amanhã às 10h", True, numero="5514999990000"),
                notificar=True, enviar=True)
        self.assertEqual(r["acao"], "agenda")
        self.assertEqual(len(agenda.listar()), 1)
        self.assertIn("Anotado na agenda", r["notificacao"]["texto"])
        self.assertTrue(r.get("envio_confirmacao", {}).get("simulado"))

    def test_eco_do_alfred_na_conversa_voce_ignorado(self):
        chamado = []
        with mock.patch.object(agenda, "ollama_generate_json", lambda *a, **k: chamado.append(1)):
            r = whatsapp.processar_mensagem_whatsapp(
                self._payload("🎩 *Alfred* — mensagem de Carlos: reunião amanhã às 14h\nref: 20261020-x",
                              True, numero="5514999990000"), enviar=False)
        self.assertNotEqual(r["acao"], "agenda")
        self.assertEqual(chamado, [])
        self.assertEqual(agenda.listar(), [])

    def test_id_enviado_pelo_alfred_ignorado(self):
        lib_whatsapp._marcar_envio("ID-DO-ALFRED")
        self.assertTrue(lib_whatsapp.enviado_pelo_alfred("ID-DO-ALFRED"))
        msg = {"message_id": "ID-DO-ALFRED", "texto": "lembrar amanhã", "tipo_msg": "texto"}
        self.assertTrue(whatsapp._eco_do_alfred(msg))


class TestLimparAudios(Base):
    def test_apaga_so_audios_de_whatsapp_antigos(self):
        pd = core.pend_dir()
        velho, novo, email = pd / "wa-velho.mp3", pd / "wa-novo.mp3", pd / "email.mp3"
        for f in (velho, novo, email):
            f.write_bytes(b"ID3" + b"0" * 100)
        tres_dias = (datetime.now() - timedelta(days=3)).timestamp()
        os.utime(velho, (tres_dias, tres_dias))
        os.utime(email, (tres_dias, tres_dias))
        agora = core._now().isoformat(timespec="minutes")
        antes = (core._now() - timedelta(days=3)).isoformat(timespec="minutes")
        core._save_pend({"pendencias": [
            {"id": "wa-velho", "tipo": "whatsapp", "status": "respondida", "criado_em": antes,
             "arquivos": {"md": None, "mp3": "pendencias/wa-velho.mp3"}},
            {"id": "wa-novo", "tipo": "whatsapp", "status": "aguardando", "criado_em": agora,
             "arquivos": {"md": None, "mp3": "pendencias/wa-novo.mp3"}},
            {"id": "email", "tipo": "evento", "status": "aguardando", "criado_em": antes,
             "arquivos": {"md": None, "mp3": "pendencias/email.mp3"}},
        ]})
        sim = whatsapp.limpar_audios(dias=2, simular=True)
        self.assertEqual(sim["removidos"], ["wa-velho"])
        self.assertTrue(velho.exists())
        r = whatsapp.limpar_audios(dias=2)
        self.assertEqual(r["removidos"], ["wa-velho"])
        self.assertFalse(velho.exists())
        self.assertTrue(novo.exists())
        self.assertTrue(email.exists())
        p = core.obter_pendencia("wa-velho")
        self.assertIsNone(p["arquivos"]["mp3"])
        self.assertIn("audio_removido_em", p)


if __name__ == "__main__":
    unittest.main()
