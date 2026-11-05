"""
Testes das promessas (o que Pedro disse que ia fazer → agenda; prazo vencido sem sinal → "Atrasado" na agenda)
e do histórico das mensagens que Pedro envia no WhatsApp.

    cd services/inbox && venv/bin/python -m unittest tests.test_promessas -v

Offline: o LLM é simulado, vault e alfred.db são temporários, nada chega à Evolution API nem ao Thunderbird.
"""

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

import core       # noqa: E402
import agenda     # noqa: E402
import promessas  # noqa: E402
import whatsapp   # noqa: E402
from _shared import lib_db, lib_whatsapp  # noqa: E402

JID = "5514988887777@s.whatsapp.net"


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


class LLM:
    """Substitui ollama_generate_json: devolve as respostas em ordem e guarda os prompts."""
    def __init__(self, *respostas):
        self.respostas, self.prompts = list(respostas), []

    def __call__(self, prompt, *a, **k):
        self.prompts.append(prompt)
        return self.respostas.pop(0) if self.respostas else None


def _promessa(o_que="Mandar o orçamento do servidor", trecho="amanhã te mando o orçamento", data="", hora="",
              para="Carlos", conf=0.9):
    return {"o_que": o_que, "para_quem": para, "data": data, "hora": hora, "confianca": conf, "trecho": trecho}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="alfred-promessas-test-"))
        (cls.tmp / "vault").mkdir()
        cls.env = mock.patch.dict(os.environ, {"VAULT_ALFRED": str(cls.tmp / "vault"), "ALFRED_DB": str(cls.tmp / "alfred.db"),
                                               "INBOX_AUTOPLAY": "false", "EVOLUTION_URL": "", "WHATSAPP_DRY_RUN": "true",
                                               "WHATSAPP_NUMERO_PEDRO": "5514999990000"})
        cls.env.start()
        cls.patches = [
            mock.patch.object(core, "_notify", lambda *a, **k: None),
            mock.patch.object(agenda, "ollama_generate_json", lambda *a, **k: None),   # Resumo do Alfred
            mock.patch.object(core, "_iniciar_aguardador", lambda c, i: None),
            mock.patch.object(lib_whatsapp, "ENVIADOS_FILE", cls.tmp / "enviados.jsonl"),
            mock.patch.object(lib_whatsapp, "IDS_ENVIADOS_FILE", cls.tmp / "ids_enviados.json"),
            mock.patch.object(whatsapp, "SEEN_FILE", cls.tmp / "seen.json"),
            mock.patch.object(whatsapp, "FILA_FILE", cls.tmp / "fila.json"),
            mock.patch.object(promessas, "SEEN_FILE", cls.tmp / "promessas_seen.json"),
            mock.patch.object(promessas, "_cfg", lambda: {
                "ativo": True, "canais": ["whatsapp", "email"], "pastas_enviados": ["Sent"], "janela_horas": 48.0,
                "prazo_padrao_dias": 3.0, "confianca_minima": 0.7}),
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
        (self.tmp / "vault" / "tasks.yml").unlink(missing_ok=True)
        for f in list(self.tmp.glob("*.json*")) + list(self.tmp.glob("alfred.db*")):
            f.unlink()
        self.agora = core._now().replace(second=0, microsecond=0)

    def _wa(self, mid, texto, from_me, quando, jid=JID):
        lib_db.registrar_wa(mid, jid, "Pedro" if from_me else "Carlos", texto, from_me=from_me, ts=_iso(quando))

    def _varrer(self, llm):
        resumo = {"novas": [], "falhas": 0}
        seen = core.load_seen(promessas.SEEN_FILE)
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            promessas.varrer_whatsapp(seen, self.agora - timedelta(hours=48), resumo, notificar=False)
        core.save_seen(seen, promessas.SEEN_FILE)
        return resumo

    def _registrar_vencida(self, dias_atras=5, **kw):
        """Promessa feita há `dias_atras` dias com prazo de 1 dia — já vencida, sem item na agenda."""
        dito = self.agora - timedelta(days=dias_atras)
        achada = {"o_que": "Mandar o orçamento do servidor", "para_quem": "Carlos", "trecho": "te mando amanhã",
                  "prazo": dito + timedelta(days=1), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        origem = {"canal": "whatsapp", "contato": "Carlos", "remote_jid": JID, "message_id": "P1", **kw}
        return promessas.registrar(achada, origem, dito, notificar=False), dito


class TestHistoricoDoQueEnvio(Base):
    def _payload(self, message, from_me=True, numero="5514988887777", mid="MSG1", tipo="conversation"):
        return {"event": "messages.upsert", "instance": "alfred", "sender": "5514999990000@s.whatsapp.net",
                "data": {"key": {"remoteJid": f"{numero}@s.whatsapp.net", "fromMe": from_me, "id": mid},
                         "pushName": "Pedro", "message": message, "messageType": tipo,
                         "messageTimestamp": int(datetime.now().timestamp())}}

    def test_fromme_para_contato_vai_para_o_historico(self):
        whatsapp.processar_mensagem_whatsapp(self._payload({"conversation": "Pode deixar, sexta te mando"}), enviar=False)
        enviadas = lib_db.mensagens_wa(from_me=True)
        self.assertEqual([(m["remote_jid"], m["texto"]) for m in enviadas], [(JID, "Pode deixar, sexta te mando")])

    def test_midia_sem_legenda_fica_registrada(self):
        whatsapp.processar_mensagem_whatsapp(self._payload({"imageMessage": {}}, mid="IMG1", tipo="imageMessage"),
                                             enviar=False)
        self.assertEqual([m["texto"] for m in lib_db.mensagens_wa(from_me=True)], ["(imagem enviada)"])

    def test_conversa_voce_nao_entra_como_mensagem_a_contato(self):
        whatsapp.processar_mensagem_whatsapp(self._payload({"conversation": "oi"}, numero="5514999990000"), enviar=False)
        self.assertEqual(lib_db.mensagens_wa(from_me=True), [])


class TestDeteccao(Base):
    def _det(self, resposta, falas=("Pode deixar, amanhã te mando o orçamento",), ref=None):
        with mock.patch.object(promessas, "ollama_generate_json", LLM(resposta)):
            return promessas.detectar_promessas(list(falas), "» Pedro: ...", "teste", ref or self.agora)

    def test_prefiltro_evita_llm(self):
        llm = LLM()
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            self.assertEqual(promessas.detectar_promessas(["valeu, obrigado!"], "", "teste", self.agora), [])
        self.assertEqual(llm.prompts, [])
        for texto in ("Pode deixar, amanhã te mando", "Eu te passo o guia", "Combinado", "Pode ser amanhã",
                      "Fico de ver isso até sexta", "Já já faço"):
            self.assertTrue(promessas.prefiltro(texto), texto)
        for texto in ("kkkkk", "Hoje é sua", "valeu!"):
            self.assertFalse(promessas.prefiltro(texto), texto)

    def test_com_data_e_hora(self):
        amanha = self.agora + timedelta(days=1)
        [p] = self._det({"promessas": [_promessa(data=amanha.strftime("%Y-%m-%d"), hora="15:00")]})
        self.assertEqual((p["prazo"].date(), p["prazo"].hour), (amanha.date(), 15))
        self.assertFalse(p["dia_inteiro"])
        self.assertFalse(p["prazo_assumido"])

    def test_sem_data_assume_prazo_padrao(self):
        [p] = self._det({"promessas": [_promessa()]})
        self.assertTrue(p["prazo_assumido"])
        self.assertTrue(p["dia_inteiro"])
        self.assertEqual(p["prazo"].date(), (self.agora + timedelta(days=3)).date())

    def test_data_antes_da_fala_vira_prazo_padrao(self):
        ontem = (self.agora - timedelta(days=1)).strftime("%Y-%m-%d")
        [p] = self._det({"promessas": [_promessa(data=ontem)]})
        self.assertTrue(p["prazo_assumido"])

    def test_trecho_do_contato_nao_e_promessa_de_pedro(self):
        self.assertEqual(self._det({"promessas": [_promessa(trecho="me manda o relatório até sexta?")]},
                                   falas=["pode deixar, vou ver"]), [])

    def test_baixa_confianca_descartada(self):
        self.assertEqual(self._det({"promessas": [_promessa(conf=0.4)]}), [])

    def test_llm_fora_do_ar_devolve_none(self):
        self.assertIsNone(self._det(None))


class TestVarreduraWhatsApp(Base):
    def test_promessa_vai_para_agenda_com_contexto(self):
        self._wa("C1", "Pedro, me manda o orçamento do servidor?", False, self.agora - timedelta(hours=2))
        self._wa("P1", "Pode deixar, amanhã te mando o orçamento", True, self.agora - timedelta(hours=1))
        amanha = (self.agora + timedelta(days=1)).strftime("%Y-%m-%d")
        llm = LLM({"promessas": [_promessa(trecho="amanhã te mando o orçamento", data=amanha)]})
        r = self._varrer(llm)
        self.assertEqual(len(r["novas"]), 1)
        # o contato entra como contexto, só a fala de Pedro vai marcada com »
        self.assertIn("  [", llm.prompts[0].split("CONVERSA:")[1].splitlines()[1])
        self.assertIn("Carlos: Pedro, me manda o orçamento", llm.prompts[0])
        self.assertIn("» [", llm.prompts[0])
        [p] = promessas.listar()
        self.assertEqual((p["status"], p["para_quem"], p["origem"]["message_id"]), ("aberta", "Carlos", "P1"))
        ag = agenda.obter(p["agenda_id"])
        self.assertEqual((ag["tipo"], ag["origem"]["canal"], str(ag["inicio"])), ("tarefa", "promessa", amanha))
        self.assertIn("Mandar o orçamento do servidor (Carlos)", (self.tmp / "vault" / "tasks.yml").read_text(encoding="utf-8"))
        # segunda varredura: a fala já foi lida — nem chama o LLM
        llm2 = LLM({"promessas": [_promessa()]})
        self.assertEqual(self._varrer(llm2)["novas"], [])
        self.assertEqual(llm2.prompts, [])
        self.assertEqual(len(promessas.listar()), 1)

    def test_llm_fora_do_ar_tenta_de_novo_no_proximo_ciclo(self):
        self._wa("P1", "Pode deixar, amanhã te mando o orçamento", True, self.agora - timedelta(hours=1))
        self.assertEqual(self._varrer(LLM(None))["falhas"], 1)
        self.assertEqual(len(self._varrer(LLM({"promessas": [_promessa()]}))["novas"]), 1)

    def test_mesma_promessa_repetida_nao_duplica(self):
        self._wa("P1", "amanhã te mando o orçamento", True, self.agora - timedelta(hours=3))
        self._varrer(LLM({"promessas": [_promessa()]}))
        self._wa("P2", "vou mandar o orçamento hoje ainda", True, self.agora - timedelta(hours=1))
        r = self._varrer(LLM({"promessas": [_promessa(trecho="vou mandar o orçamento hoje ainda")]}))
        self.assertEqual(r["novas"], [])
        self.assertEqual(len(promessas.listar()), 1)


class TestVerificacao(Base):
    def test_sem_mensagem_depois_vira_atrasada_sem_llm(self):
        p, _ = self._registrar_vencida()
        self.assertIsNone(p["agenda_id"])            # prazo já tinha passado: nada no prazo
        llm = LLM()
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            r = promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(r["atrasadas"], [p["id"]])
        self.assertEqual(llm.prompts, [])
        p = promessas.obter(p["id"])
        self.assertEqual(p["status"], "atrasada")
        ag = agenda.obter(p["agenda_atraso_id"])
        self.assertTrue(ag["titulo"].startswith("Atrasado: Mandar o orçamento do servidor (Carlos)"))
        self.assertEqual(datetime.fromisoformat(ag["inicio"]).hour, promessas.HORA_PADRAO)
        self.assertGreater(datetime.fromisoformat(ag["inicio"]), self.agora)
        self.assertTrue(ag["origem"]["atraso"])
        # atrasada não é verificada de novo (nem duplica o item da agenda)
        promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(len(agenda.listar()), 1)

    def test_mensagem_depois_e_llm_sim_cumprida(self):
        p, dito = self._registrar_vencida()
        self._wa("P2", "segue o orçamento (documento enviada)", True, dito + timedelta(hours=20))
        llm = LLM({"cumprida": "sim", "motivo": "Pedro mandou o orçamento"})
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            r = promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(r["cumpridas"], [p["id"]])
        self.assertIn("segue o orçamento", llm.prompts[0])
        self.assertEqual(promessas.obter(p["id"])["status"], "cumprida")
        self.assertEqual(agenda.listar(), [])

    def test_so_o_contato_falou_depois_vira_atrasada(self):
        p, dito = self._registrar_vencida()
        self._wa("C2", "e aí, conseguiu o orçamento?", False, dito + timedelta(days=2))
        llm = LLM()
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(promessas.obter(p["id"])["status"], "atrasada")
        self.assertEqual(llm.prompts, [])

    def test_incerto_vira_atrasada(self):
        p, dito = self._registrar_vencida()
        self._wa("P2", "tô vendo aqui", True, dito + timedelta(hours=5))
        with mock.patch.object(promessas, "ollama_generate_json", LLM({"cumprida": "incerto", "motivo": "x"})):
            promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(promessas.obter(p["id"])["status"], "atrasada")

    def test_llm_fora_do_ar_fica_aberta(self):
        p, dito = self._registrar_vencida()
        self._wa("P2", "tô vendo aqui", True, dito + timedelta(hours=5))
        with mock.patch.object(promessas, "ollama_generate_json", LLM(None)):
            r = promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(promessas.obter(p["id"])["status"], "aberta")

    def test_desfazer_na_agenda_descarta(self):
        achada = {"o_que": "Ligar para a Fatec", "para_quem": "Ana", "trecho": "amanhã eu ligo",
                  "prazo": self.agora + timedelta(days=1), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        p = promessas.registrar(achada, {"canal": "whatsapp", "contato": "Ana", "remote_jid": JID}, self.agora,
                                notificar=False)
        agenda.desfazer(p["agenda_id"])
        r = promessas.verificar(self.agora + timedelta(days=3), msgs_email=[], notificar=False)
        self.assertEqual(r["descartadas"], [p["id"]])
        self.assertEqual(promessas.obter(p["id"])["status"], "descartada")
        self.assertEqual([i["status"] for i in agenda.listar()], ["cancelado"])

    def test_antes_do_prazo_nada_muda(self):
        achada = {"o_que": "Ligar para a Fatec", "para_quem": "Ana", "trecho": "sexta eu ligo",
                  "prazo": self.agora + timedelta(days=2), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        p = promessas.registrar(achada, {"canal": "whatsapp", "contato": "Ana", "remote_jid": JID}, self.agora,
                                notificar=False)
        r = promessas.verificar(self.agora, msgs_email=[], notificar=False)
        self.assertEqual((r["atrasadas"], r["cumpridas"]), ([], []))
        self.assertEqual(promessas.obter(p["id"])["status"], "aberta")

    def _registrada(self):
        achada = {"o_que": "Ligar para a Fatec", "para_quem": "Ana", "trecho": "sexta eu ligo",
                  "prazo": self.agora + timedelta(days=2), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        return promessas.registrar(achada, {"canal": "whatsapp", "contato": "Ana", "remote_jid": JID}, self.agora,
                                   notificar=False)

    def test_feita_pelo_terminal_marca_feito_na_agenda(self):
        p = self._registrada()
        self.assertEqual(promessas.marcar(p["id"], "cumprida")["status"], "cumprida")
        item = agenda.obter(p["agenda_id"])
        self.assertEqual((item["status"], item["atualizar"]), ("feito", True))   # ☑ vai para o calendário

    def test_descartar_tira_o_lembrete_futuro(self):
        p = self._registrada()
        promessas.marcar(p["id"], "descartada")
        self.assertEqual(agenda.obter(p["agenda_id"])["status"], "cancelado")

    def test_feito_no_painel_cumpre_a_promessa(self):
        p = self._registrada()
        agenda.concluir(p["agenda_id"], via="painel")
        self.assertEqual(promessas.obter(p["id"])["status"], "cumprida")
        self.assertEqual(agenda.obter(p["agenda_id"])["status"], "feito")


class TestEmail(Base):
    def _email(self, mid, texto, quando, para=("carlos@attano.com",), assunto="Re: orçamento", anexos=()):
        return {"message_id": mid, "to": list(para), "subject": assunto, "text": texto, "date": quando,
                "is_bulk": False, "attachments": list(anexos)}

    def test_email_enviado_com_citacao(self):
        m = self._email("E1", "Oi Carlos, mando o orçamento até sexta.\n\nEm ter., 3 de nov., Carlos escreveu:\n"
                              "> Pedro, consegue me mandar o orçamento?", self.agora - timedelta(hours=1))
        llm = LLM({"promessas": [_promessa(trecho="mando o orçamento até sexta")]})
        resumo, seen = {"novas": [], "falhas": 0}, {}
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            promessas.varrer_email([m], seen, resumo, notificar=False)
        self.assertIn("MENSAGEM RESPONDIDA", llm.prompts[0])
        self.assertIn("» [", llm.prompts[0])
        [p] = promessas.listar()
        self.assertEqual((p["origem"]["canal"], p["origem"]["para"]), ("email", ["carlos@attano.com"]))
        self.assertIn("E1", seen)

    def test_email_depois_ao_mesmo_destinatario_cumpre(self):
        dito = self.agora - timedelta(days=5)
        achada = {"o_que": "Mandar o orçamento", "para_quem": "Carlos", "trecho": "mando até sexta",
                  "prazo": dito + timedelta(days=1), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        p = promessas.registrar(achada, {"canal": "email", "contato": "Carlos", "para": ["carlos@attano.com"],
                                         "message_id": "E1"}, dito, notificar=False)
        depois = self._email("E2", "Segue o orçamento.", dito + timedelta(days=1), anexos=["orcamento.pdf"])
        outro = self._email("E3", "Oi Ana", dito + timedelta(days=1), para=("ana@x.com",))
        llm = LLM({"cumprida": "sim", "motivo": "e-mail com orcamento.pdf"})
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            promessas.verificar(self.agora, msgs_email=[depois, outro], notificar=False)
        self.assertIn("orcamento.pdf", llm.prompts[0])
        self.assertNotIn("Oi Ana", llm.prompts[0])
        self.assertEqual(promessas.obter(p["id"])["status"], "cumprida")

    def test_sem_pasta_enviados_promessa_de_email_espera(self):
        dito = self.agora - timedelta(days=5)
        achada = {"o_que": "Mandar o orçamento", "para_quem": "Carlos", "trecho": "mando até sexta",
                  "prazo": dito + timedelta(days=1), "dia_inteiro": True, "prazo_assumido": False, "confianca": 0.9}
        p = promessas.registrar(achada, {"canal": "email", "contato": "Carlos", "para": ["carlos@attano.com"]},
                                dito, notificar=False)
        promessas.verificar(self.agora, msgs_email=None, notificar=False)
        self.assertEqual(promessas.obter(p["id"])["status"], "aberta")


class TestRetroativoECiclo(Base):
    def _pendencia(self, pid, resposta, respondido_em, status="respondida_manual"):
        return {"id": pid, "tipo": "whatsapp", "status": status, "resposta_texto": resposta,
                "respondido_em": _iso(respondido_em),
                "whatsapp": {"contato": "Carlos", "remote_jid": JID, "texto": "me manda o orçamento?",
                             "mensagens": [{"texto": "me manda o orçamento?"}], "envio": {"message_id": "R1"}}}

    def test_respostas_ja_gravadas_nas_pendencias(self):
        core.pend_dir()
        core._save_pend({"pendencias": [
            self._pendencia("p-nova", "amanhã te mando o orçamento", self.agora - timedelta(days=2)),
            self._pendencia("p-velha", "amanhã te mando o orçamento", self.agora - timedelta(days=30)),
            self._pendencia("p-audio", "(audio enviado direto pelo celular)", self.agora - timedelta(days=1)),
        ]})
        llm = LLM({"promessas": [_promessa()]})
        resumo, seen = {"novas": [], "falhas": 0}, {}
        with mock.patch.object(promessas, "ollama_generate_json", llm):
            promessas.varrer_pendencias(seen, self.agora - timedelta(days=14), resumo, notificar=False)
        self.assertEqual(len(llm.prompts), 1)
        self.assertIn("Carlos: me manda o orçamento?", llm.prompts[0])
        [p] = promessas.listar()
        self.assertEqual(p["origem"]["pendencia_id"], "p-nova")
        self.assertEqual(list(seen), ["pendencia:p-nova"])

    def test_ciclo_le_enviados_varre_e_verifica(self):
        self._wa("P1", "amanhã te mando o orçamento", True, self.agora - timedelta(hours=1))
        coletar = mock.Mock(return_value=[])
        with mock.patch.object(core, "coletar", coletar), \
                mock.patch.object(promessas, "ollama_generate_json", LLM({"promessas": [_promessa()]})):
            r = promessas.ciclo({"conta": "outlook"}, notificar=False)
        self.assertEqual(len(r["novas"]), 1)
        self.assertEqual(coletar.call_args.args[0]["pastas"], ["Sent"])
        self.assertIn("verificacao", r)
        self.assertTrue((self.tmp / "promessas_seen.json").exists())

    def test_ciclo_desligado(self):
        with mock.patch.object(promessas, "_cfg", lambda: {"ativo": False}):
            self.assertEqual(promessas.ciclo({}), {"ativo": False})


if __name__ == "__main__":
    unittest.main()
