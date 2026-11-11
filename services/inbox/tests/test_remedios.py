"""
Testes do lembrete de remédio (remedios.py): dose de N em N horas → post-it (recado do Pestinha, com o horário em
que foi criado) → sem "tomei" em 10 min, o número do Alfred pergunta no WhatsApp → "sim" (e a hora) ou "não"
(espera o "sim" para recomeçar a contagem).

    cd services/inbox
    venv/bin/python -m unittest tests.test_remedios -v

Offline: researcher e LLM simulados, vault temporário, envios simulados (o ambiente de tests.test_whatsapp).
"""

from datetime import timedelta
from unittest import mock

from tests.test_whatsapp_assistente import BaseAssistente, FIM, PEDRO, turno_falso
from tests.test_whatsapp import core, main, whatsapp
from _shared import lib_ferramentas as lf
import pesquisa
import remedios

H8 = timedelta(hours=8)


def minutos(n):
    return timedelta(minutes=n)


class Base(BaseAssistente):
    def setUp(self):
        super().setUp()
        remedios.arquivo().unlink(missing_ok=True)
        self.t0 = core._now().replace(hour=14, minute=5, second=0, microsecond=0)

    def criar(self, nome="Amoxicilina", horas=8, **kw):
        return remedios.adicionar(nome, horas, agora=self.t0, **kw)

    def perguntado(self, nome="Amoxicilina"):
        """Remédio com a dose aberta às 14:05 e a pergunta do WhatsApp já enviada (14:15)."""
        r = self.criar(nome)
        remedios.passar(self.t0)
        remedios.passar(self.t0 + minutos(10))
        return r


class TestDoses(Base):
    def test_dose_vencida_vira_um_postit_com_o_horario_em_que_foi_criado(self):
        r = self.criar()
        self.assertEqual(remedios.recados(), [])                       # nada antes do laço abrir a dose
        remedios.passar(self.t0)
        (recado,) = remedios.recados()
        self.assertEqual((recado["tipo"], recado["texto"]), ("remedio", "Tomar Amoxicilina"))
        self.assertEqual(recado["quando"], self.t0.timestamp())
        self.assertIn("14:05", recado["detalhe"])
        self.assertIn("8 h", recado["detalhe"])
        self.assertTrue(recado["id"].startswith(f"remedio:{r['id']}:"))
        remedios.passar(self.t0 + minutos(5))
        self.assertEqual(remedios.recados(), [recado])                 # a mesma dose não vira outro post-it
        self.assertEqual(self.para(PEDRO), [])

    def test_tomei_no_postit_recomeca_a_contagem(self):
        self.criar()
        remedios.passar(self.t0)
        (recado,) = remedios.recados()
        self.assertTrue(remedios.recado_feito(recado["id"], agora=self.t0 + minutos(3)))
        self.assertEqual(remedios.recados(), [])
        self.assertFalse(remedios.recado_feito(recado["id"]))          # a dose já foi fechada: o pedido repetido não conta
        remedios.passar(self.t0 + H8)                                  # 8 h depois do horário antigo: ainda não
        self.assertEqual(remedios.recados(), [])
        remedios.passar(self.t0 + minutos(3) + H8)                     # 8 h depois do "tomei"
        (novo,) = remedios.recados()
        self.assertNotEqual(novo["id"], recado["id"])
        self.assertEqual(self.para(PEDRO), [])                         # marcou no computador: o WhatsApp não pergunta

    def test_sem_tomei_em_10_min_o_numero_do_alfred_pergunta_uma_vez(self):
        self.criar()
        remedios.passar(self.t0)
        remedios.passar(self.t0 + minutos(9))
        self.assertEqual(self.para(PEDRO), [])
        remedios.passar(self.t0 + minutos(10))
        remedios.passar(self.t0 + minutos(11))
        (envio,) = self.para(PEDRO)
        self.assertEqual(envio["instancia"], "alfred-assistente")
        self.assertIn("Amoxicilina", envio["conteudo"])
        self.assertIn("*sim* ou *não*", envio["conteudo"])
        self.assertEqual(len(remedios.recados()), 1)                   # o post-it continua até o "tomei"

    def test_sim_pergunta_a_hora_e_a_contagem_parte_dela(self):
        r = self.perguntado()
        resposta = remedios.resposta_whatsapp("Sim!", agora=self.t0 + minutos(20))
        self.assertIn("Que horas", resposta)
        self.assertEqual(remedios.recados(), [])
        self.assertEqual(remedios.obter(r["id"])["proxima_em"], (self.t0 + minutos(20) + H8).isoformat())   # por ora
        resposta = remedios.resposta_whatsapp("umas 14h", agora=self.t0 + minutos(21))
        self.assertIn("14:00", resposta)
        self.assertIn("22:00", resposta)
        self.assertEqual(remedios.obter(r["id"])["proxima_em"], (self.t0 - minutos(5) + H8).isoformat())
        self.assertIsNone(remedios.resposta_whatsapp("14h", agora=self.t0 + minutos(22)))   # a hora já foi dita

    def test_nao_fica_esperando_o_sim_para_recomecar(self):
        r = self.perguntado()
        resposta = remedios.resposta_whatsapp("não", agora=self.t0 + minutos(15))
        self.assertIn("*sim*", resposta)
        self.assertEqual(len(remedios.recados()), 1)
        remedios.passar(self.t0 + timedelta(hours=2))
        self.assertEqual(len(self.para(PEDRO)), 1)                     # não pergunta de novo
        quando = self.t0 + timedelta(hours=3)
        resposta = remedios.resposta_whatsapp("sim", agora=quando)
        self.assertNotIn("Que horas", resposta)                        # o "sim" depois do "não" é o horário
        self.assertIn(f"{quando + H8:%H:%M}", resposta)
        self.assertEqual(remedios.recados(), [])
        self.assertEqual(remedios.obter(r["id"])["proxima_em"], (quando + H8).isoformat())

    def test_o_que_nao_e_resposta_da_dose_segue_para_a_conversa(self):
        self.assertIsNone(remedios.resposta_whatsapp("sim", agora=self.t0))          # nada foi perguntado
        self.criar()
        remedios.passar(self.t0)
        self.assertIsNone(remedios.resposta_whatsapp("sim", agora=self.t0 + minutos(5)))   # o WhatsApp ainda não perguntou
        remedios.passar(self.t0 + minutos(10))
        self.assertIsNone(remedios.resposta_whatsapp("como está a agenda?", agora=self.t0 + minutos(12)))
        self.assertEqual(len(remedios.recados()), 1)

    def test_hora_tomada(self):
        agora = self.t0.replace(hour=14, minute=30)
        casos = {"agora": agora, "Agorinha.": agora, "há 20 min": agora - minutos(20), "faz meia hora": agora - minutos(30),
                 "faz 1 hora": agora - minutos(60), "14:10": agora.replace(minute=10), "14h10": agora.replace(minute=10),
                 "às 13h": agora.replace(hour=13, minute=0), "umas 2": agora.replace(hour=14, minute=0),
                 "23h": (agora - timedelta(days=1)).replace(hour=23, minute=0),
                 "marca reunião às 15h": None, "25h": None, "": None}
        for texto, esperado in casos.items():
            self.assertEqual(remedios.hora_tomada(texto, agora), esperado, texto)

    def test_mesmo_nome_nao_duplica_e_parar_encerra(self):
        a = self.criar("Amoxicilina", 8)
        b = self.criar("amoxicilina ", 6)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual([(r["nome"], r["intervalo_horas"]) for r in remedios.listar()], [("Amoxicilina", 6.0)])
        remedios.passar(self.t0)
        self.assertEqual(remedios.parar("AMOXICILINA")["nome"], "Amoxicilina")
        self.assertEqual((remedios.listar(), remedios.recados()), ([], []))
        self.assertEqual(remedios.passar(self.t0 + H8), [])
        self.assertIsNone(remedios.parar("Dipirona"))
        with self.assertRaises(ValueError):
            self.criar("Dipirona", 0)


class TestCuidados(Base):
    def pesquisar(self, sintese="Não associe a bebidas alcoólicas. Contraindicado para alérgicos a penicilina."):
        r = self.criar()
        llm = mock.Mock(return_value={"cuidados": ["Não beber álcool", " Não tomar se for alérgico a penicilina ", ""]})
        busca = mock.Mock(return_value={"slug": "amoxicilina", "cached": False})
        with mock.patch.object(pesquisa, "pesquisar", busca), mock.patch.object(remedios, "ollama_generate_json", llm), \
                mock.patch.object(pesquisa, "obter", lambda slug: {"sintese": sintese}):
            cuidados = remedios.pesquisar_cuidados(r["id"])
        return r, cuidados, busca, llm

    def test_cuidados_da_pesquisa_vao_para_o_postit_e_para_o_whatsapp(self):
        r, cuidados, busca, llm = self.pesquisar()
        self.assertEqual(cuidados, ["Não beber álcool", "Não tomar se for alérgico a penicilina"])
        tema, profundo = busca.call_args.args[0], busca.call_args.kwargs.get("profundo")
        self.assertIn("Amoxicilina", tema)
        self.assertIn("contraindicações", tema)
        self.assertIs(profundo, False)
        self.assertIn("bebidas alcoólicas", llm.call_args.args[0])     # o resumo sai do que a pesquisa achou
        self.assertEqual(remedios.obter(r["id"])["pesquisa"], {"status": "pronta", "slug": "amoxicilina"})
        remedios.passar(self.t0)
        self.assertIn("• Não beber álcool", remedios.recados()[0]["detalhe"])
        remedios.passar(self.t0 + minutos(10))
        self.assertIn("Não beber álcool", self.para(PEDRO)[-1]["conteudo"])

    def test_pesquisa_fora_do_ar_nao_impede_o_lembrete(self):
        r = self.criar()
        with mock.patch.object(pesquisa, "pesquisar", side_effect=pesquisa.ErroPesquisa("researcher fora do ar")):
            self.assertEqual(remedios.pesquisar_cuidados(r["id"]), [])
        self.assertEqual(remedios.obter(r["id"])["pesquisa"]["status"], "falhou")
        remedios.passar(self.t0)
        self.assertIn("bula", remedios.recados()[0]["detalhe"])


class TestWhatsApp(Base):
    def test_resposta_de_pedro_no_numero_do_alfred_nao_vai_para_a_conversa(self):
        r = self.criar()
        agora = core._now()
        remedios.passar(self.t0)
        with mock.patch.object(remedios, "ESPERA_MIN", 0):
            remedios.passar(self.t0)
        d = self.receber("Sim", numero=PEDRO, nome="Pedro")
        self.assertEqual(d["acao"], "remedio", d)
        self.assertIn("Que horas", self.para(PEDRO)[-1]["conteudo"])
        self.receber("agora", numero=PEDRO, nome="Pedro")
        self.assertIn("Anotado", self.para(PEDRO)[-1]["conteudo"])
        self.motor.assert_not_called()
        self.assertGreaterEqual(core._parse_dt(remedios.obter(r["id"])["proxima_em"]), agora + H8 - minutos(1))
        self.motor.side_effect = lambda *a, **k: turno_falso(FIM)
        d = self.receber("como está a agenda?", numero=PEDRO, nome="Pedro")
        self.assertEqual(d["acao"], "pedido", d)
        self.motor.assert_called_once()

    def test_confirmacao_pendente_da_conversa_vem_antes_do_remedio(self):
        self.perguntado()
        whatsapp._APROVACAO_WA["id"] = "ap1"
        self.addCleanup(whatsapp._APROVACAO_WA.clear)
        with mock.patch("conversa.aprovar", return_value=True) as aprovar:
            d = self.receber("sim", numero=PEDRO, nome="Pedro")
        aprovar.assert_called_once_with("ap1")
        self.assertEqual(d["acao"], "aprovacao", d)
        self.assertEqual(len(remedios.recados()), 1)                   # a dose continua esperando a resposta


class TestPestinha(Base):
    def test_recados_do_pet_trazem_remedio_primeiro_e_o_horario_de_cada_um(self):
        self.receber("oi")                                             # só a saudação
        self.receber("preciso de um orçamento de um site para a Acme")   # recado no número do Alfred
        self.assertEqual(len(whatsapp.recados()), 1)
        self.criar()
        remedios.passar(self.t0)
        dose, recado = main._recados_para_o_pet()
        self.assertEqual((dose["tipo"], dose["quando"]), ("remedio", self.t0.timestamp()))
        self.assertEqual(recado["quando"], core._parse_dt(whatsapp.recados()[0]["quando"]).timestamp())
        self.assertNotIn("tipo", recado)                               # o Despachante põe "recado"

    def test_tomei_do_postit_fecha_a_dose_e_nao_mexe_nas_pendencias(self):
        self.criar()
        remedios.passar(self.t0)
        with mock.patch.object(whatsapp, "responder_texto") as responder:
            main._recado_feito(remedios.recados()[0]["id"])
        responder.assert_not_called()
        self.assertEqual(remedios.recados(), [])


class TestFerramentas(Base):
    def test_criar_listar_tomar_e_parar_pela_conversa(self):
        busca = mock.Mock(return_value={"slug": "amoxicilina", "synthesis": "Evite álcool."})
        with mock.patch.object(pesquisa, "pesquisar", busca), \
                mock.patch.object(remedios, "ollama_generate_json", lambda *a, **k: {"cuidados": ["Não beber álcool"]}):
            r = lf.executar("criar_lembrete_remedio", {"nome": "Amoxicilina", "intervalo_horas": 8})
        self.assertIn("Amoxicilina", r)
        self.assertIn("8 h", r)
        self.assertIn("Não beber álcool", r)
        self.assertFalse(lf.perigosa("criar_lembrete_remedio"))
        self.assertIn("Não beber álcool", lf.executar("listar_remedios"))
        remedios.passar()
        self.assertEqual(len(remedios.recados()), 1)
        self.assertIn("Próxima dose", lf.executar("tomei_remedio", {"nome": "amoxicilina"}))
        self.assertEqual(remedios.recados(), [])
        self.assertIn("parado", lf.executar("parar_remedio", {"nome": "Amoxicilina"}))
        self.assertIn("Nenhum", lf.executar("listar_remedios"))
        self.assertIn("Não há lembrete", lf.executar("parar_remedio", {"nome": "Amoxicilina"}))

    def test_intervalo_invalido_nao_cria(self):
        self.assertIn("intervalo", lf.executar("criar_lembrete_remedio", {"nome": "Dipirona", "intervalo_horas": 0}))
        self.assertEqual(remedios.listar(), [])
