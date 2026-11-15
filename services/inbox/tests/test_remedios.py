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

import httpx

from tests.test_whatsapp_assistente import BaseAssistente, FIM, PEDRO, turno_falso
from tests.test_whatsapp import core, main, whatsapp
from _shared import lib_db
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
        # nenhum teste fala com o researcher de verdade: o que não for simulado recebe 503
        fora = mock.patch.object(pesquisa, "_transporte", httpx.MockTransport(lambda req: httpx.Response(503)))
        fora.start()
        self.addCleanup(fora.stop)
        self.t0 = core._now().replace(hour=14, minute=5, second=0, microsecond=0)

    def criar(self, nome="Amoxicilina", horas=8, **kw):
        return remedios.adicionar(nome, horas, agora=self.t0, **kw)   # kw: primeira, ultima, dias

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
                 "01:30": agora.replace(hour=1, minute=30), "11:55": agora.replace(hour=11, minute=55),   # com minutos: ao pé da letra
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

    def test_dose_ja_tomada_a_proxima_e_ela_mais_o_intervalo(self):
        tomou = self.t0 - timedelta(hours=2)                           # "tomei o primeiro às 12:05"
        r = self.criar("Celecoxibe", 12, ultima=tomou)
        self.assertEqual(r["proxima_em"], (tomou + timedelta(hours=12)).isoformat())
        self.assertEqual(r["ultima"]["tomada_em"], tomou.isoformat())
        self.assertEqual(remedios.passar(self.t0 + timedelta(hours=9)), [])    # nada antes das 12 h
        self.assertEqual(remedios.passar(self.t0 + timedelta(hours=10)), [("dose", r["id"])])

    def test_depois_dos_dias_do_tratamento_o_lembrete_sai_sozinho(self):
        r = self.criar("Celecoxibe", 12, dias=1)
        self.assertEqual(r["ate"], (self.t0 + timedelta(days=1)).isoformat())
        for horas in (0, 12):                                          # as duas doses do dia
            quando = self.t0 + timedelta(hours=horas)
            remedios.passar(quando)
            self.assertTrue(remedios.recado_feito(remedios.recados()[0]["id"], agora=quando))
        self.assertEqual(remedios.passar(self.t0 + timedelta(hours=24)), [("fim", r["id"])])
        self.assertEqual((remedios.listar(), remedios.recados()), ([], []))
        with self.assertRaises(ValueError):
            self.criar("Dipirona", 8, dias=0)


JOB = "0123456789ab"
SINTESE = "Não associe a bebidas alcoólicas. Contraindicado para alérgicos a penicilina."
CUIDADOS = {"cuidados": ["Não beber álcool", " Não tomar se for alérgico a penicilina ", ""]}


class TestCuidados(Base):
    """Contraindicações e o que não fazer: pesquisa profunda do researcher (lê as páginas), em segundo plano."""

    def iniciar(self, nome="Amoxicilina", *respostas):
        r = self.criar(nome)
        busca = mock.Mock(side_effect=list(respostas) or [{"status": "running", "job_id": JOB, "slug": "amoxicilina"}])
        with mock.patch.object(pesquisa, "pesquisar", busca):
            estado = remedios.iniciar_pesquisa(r["id"], agora=self.t0)
        return r, estado, busca

    def passar(self, quando, job, llm=None):
        """Uma volta do laço com o researcher respondendo `job` (dict, ou exceção) ao andamento da pesquisa.
        → (o que a volta fez com as pesquisas, o mock do andamento, o mock do LLM)."""
        andamento = mock.Mock(side_effect=job) if isinstance(job, Exception) else mock.Mock(return_value=job)
        llm = llm or mock.Mock(return_value=CUIDADOS)
        with mock.patch.object(pesquisa, "andamento", andamento), mock.patch.object(remedios, "ollama_generate_json", llm):
            feitos = [f for f in remedios.passar(quando) if f[0] == "cuidados"]
        return feitos, andamento, llm

    def test_pesquisa_profunda_roda_em_segundo_plano_e_os_cuidados_chegam_depois(self):
        r, estado, busca = self.iniciar()
        self.assertEqual((estado["status"], estado["job_id"]), ("andamento", JOB))
        self.assertIn("Amoxicilina", busca.call_args.args[0])
        self.assertIn("contraindicações", busca.call_args.args[0])
        self.assertEqual(busca.call_args.kwargs, {"profundo": True})   # o modo que lê as páginas (bulas)
        remedios.passar(self.t0)
        self.assertIn("pesquisando", remedios.recados()[0]["detalhe"])
        feitos, andamento, llm = self.passar(self.t0 + minutos(1), {"status": "running", "etapa": "leitura"})
        self.assertEqual(feitos, [])
        andamento.assert_called_once_with(JOB)
        llm.assert_not_called()
        pronto = {"status": "done", "resultado": {"slug": "amoxicilina", "synthesis": SINTESE}}
        feitos, _, llm = self.passar(self.t0 + minutos(5), pronto)
        self.assertEqual(feitos, [("cuidados", r["id"])])
        self.assertIn("bebidas alcoólicas", llm.call_args.args[0])     # o resumo sai do que a pesquisa achou
        salvo = remedios.obter(r["id"])
        self.assertEqual(salvo["pesquisa"], {"status": "pronta", "slug": "amoxicilina"})
        self.assertEqual(salvo["cuidados"], ["Não beber álcool", "Não tomar se for alérgico a penicilina"])
        self.assertIn("• Não beber álcool", remedios.recados()[0]["detalhe"])
        (aviso,) = self.para(PEDRO)                                    # os cuidados chegam pelo número do Alfred
        self.assertEqual(aviso["instancia"], "alfred-assistente")
        self.assertIn("Cuidados do *Amoxicilina*", aviso["conteudo"])
        self.assertIn("• Não tomar se for alérgico a penicilina", aviso["conteudo"])
        feitos, andamento, _ = self.passar(self.t0 + minutos(6), pronto)
        self.assertEqual(feitos, [])                                   # pesquisa pronta não é olhada de novo
        andamento.assert_not_called()
        remedios.passar(self.t0 + minutos(10))                         # a pergunta da dose leva os cuidados
        self.assertIn("Já tomou?", self.para(PEDRO)[-1]["conteudo"])
        self.assertIn("Não beber álcool", self.para(PEDRO)[-1]["conteudo"])

    def test_pesquisa_de_outro_remedio_no_cache_e_refeita_para_este(self):
        de_outro = {"slug": "celecoxibe", "cached": True, "cache_type": "semantic", "matched_slug": "amoxicilina",
                    "synthesis": "Amoxicilina: não associe a bebidas alcoólicas."}
        r, estado, busca = self.iniciar("Celecoxibe", de_outro, {"status": "running", "job_id": JOB, "slug": "celecoxibe"})
        self.assertEqual([c.kwargs for c in busca.call_args_list],
                         [{"profundo": True}, {"profundo": True, "refresh": True}])
        self.assertEqual((estado["status"], estado["job_id"]), ("andamento", JOB))
        self.assertEqual(remedios.obter(r["id"])["cuidados"], [])      # os cuidados de outro remédio não entram

    def test_pesquisa_profunda_recente_do_mesmo_remedio_e_reaproveitada_na_hora(self):
        r = self.criar()
        busca = mock.Mock(return_value={"slug": "amoxicilina", "cached": True, "cache_type": "exact", "synthesis": SINTESE})
        with mock.patch.object(pesquisa, "pesquisar", busca), \
                mock.patch.object(remedios, "ollama_generate_json", lambda *a, **k: CUIDADOS):
            estado = remedios.iniciar_pesquisa(r["id"])
        busca.assert_called_once()
        self.assertEqual(estado, {"status": "pronta", "slug": "amoxicilina"})
        self.assertEqual(remedios.obter(r["id"])["cuidados"][0], "Não beber álcool")
        self.assertEqual(self.para(PEDRO), [])                         # quem pediu já recebe os cuidados na resposta

    def test_researcher_fora_do_ar_nao_impede_o_lembrete(self):
        r = self.criar()
        with mock.patch.object(pesquisa, "pesquisar", side_effect=pesquisa.ErroPesquisa("researcher fora do ar")):
            self.assertEqual(remedios.iniciar_pesquisa(r["id"]), {"status": "falhou"})
        remedios.passar(self.t0)
        self.assertIn("bula", remedios.recados()[0]["detalhe"])
        self.assertEqual(self.para(PEDRO), [])

    def test_pesquisa_que_falha_ou_nao_rende_cuidados_avisa_pedro(self):
        casos = {"erro do job": {"status": "error", "erro": "Nenhuma página pôde ser lida"},
                 "job interrompido": {"status": "interrompido"},
                 "job desconhecido": KeyError(JOB),
                 "relatório sem cuidados": {"status": "done", "resultado": {"slug": "x", "synthesis": "Nada sobre isso."}}}
        for caso, job in casos.items():
            with self.subTest(caso):
                remedios.arquivo().unlink(missing_ok=True)
                r, _, _ = self.iniciar()
                feitos, _, _ = self.passar(self.t0 + minutos(5), job, llm=mock.Mock(return_value={"cuidados": []}))
                self.assertEqual(feitos, [("cuidados", r["id"])])
                self.assertEqual(remedios.obter(r["id"])["pesquisa"]["status"], "falhou")
                self.assertIn("Não consegui pesquisar", self.para(PEDRO)[-1]["conteudo"])

    def test_researcher_caido_no_meio_espera_e_desiste_depois_do_limite(self):
        r, _, _ = self.iniciar()
        inicio = core._parse_dt(remedios.obter(r["id"])["pesquisa"]["desde"])
        fora = pesquisa.ErroPesquisa("researcher fora do ar")
        feitos, _, _ = self.passar(inicio + minutos(5), fora)
        self.assertEqual((feitos, remedios.obter(r["id"])["pesquisa"]["status"]), ([], "andamento"))
        feitos, _, _ = self.passar(inicio + minutos(remedios.PESQUISA_MAX_MIN + 1), fora)
        self.assertEqual((feitos, remedios.obter(r["id"])["pesquisa"]["status"]), ([("cuidados", r["id"])], "falhou"))


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

    def test_dose_ja_tomada_e_duracao_quem_faz_a_conta_e_a_ferramenta(self):
        tomou = (core._now() - timedelta(hours=1)).replace(second=0, microsecond=0)
        with mock.patch.object(pesquisa, "pesquisar", return_value={"status": "running", "job_id": JOB}):
            r = lf.executar("criar_lembrete_remedio", {"nome": "Celecoxibe", "intervalo_horas": 12,
                                                        "ultima_dose": f"{tomou:%H:%M}", "dias": 5})
        self.assertIn("segundo plano", r)                              # a pesquisa das bulas não segura a resposta
        rem = remedios.achar("Celecoxibe")
        self.assertEqual(rem["pesquisa"]["job_id"], JOB)
        self.assertEqual(rem["proxima_em"], (tomou + timedelta(hours=12)).isoformat())
        self.assertEqual(rem["ate"], (tomou + timedelta(days=5)).isoformat())
        self.assertIn(f"Próxima dose: {tomou + timedelta(hours=12):%d/%m %H:%M}", r)
        self.assertIn("por 5 dias", r)
        self.assertIn("até", lf.executar("listar_remedios"))
        esq = next(e for e in lf.esquemas() if e["function"]["name"] == "criar_lembrete_remedio")
        self.assertIn("ultima_dose", esq["function"]["parameters"]["properties"])
        with mock.patch.object(pesquisa, "pesquisar") as busca:
            r = lf.executar("criar_lembrete_remedio", {"nome": "Dipirona", "intervalo_horas": 6, "ultima_dose": "ontem cedo"})
        busca.assert_not_called()
        self.assertIn("Não entendi", r)
        self.assertIsNone(remedios.achar("Dipirona"))

    def test_intervalo_invalido_nao_cria(self):
        self.assertIn("intervalo", lf.executar("criar_lembrete_remedio", {"nome": "Dipirona", "intervalo_horas": 0}))
        self.assertEqual(remedios.listar(), [])


class TestAvisos(Base):
    """A dose no feed de avisos (lib_db.avisos), que o aplicativo do celular lê."""

    def test_dose_pergunta_e_fim_do_tratamento(self):
        marco = (lib_db.avisos(limite=1) or [{"id": 0}])[-1]["id"]
        r = self.criar(dias=0.2)                                # tratamento de 4,8 h: só cabe uma dose de 8 em 8
        remedios.passar(self.t0)
        (a,) = lib_db.avisos(desde=marco)
        self.assertEqual((a["tipo"], a["titulo"], a["ref"]), ("remedio", "Tomar Amoxicilina", remedios.recados()[0]["id"]))
        self.assertIn("14:05", a["texto"])
        remedios.passar(self.t0 + minutos(5))                   # a mesma dose não avisa de novo
        self.assertEqual(len(lib_db.avisos(desde=marco)), 1)
        remedios.passar(self.t0 + minutos(10))                  # sem o "tomei": a pergunta do WhatsApp é outro aviso
        self.assertEqual([x["tipo"] for x in lib_db.avisos(desde=marco)], ["remedio", "whatsapp"])
        self.assertIn("Amoxicilina", lib_db.avisos(desde=marco)[1]["texto"])
        remedios.tomei(r["id"], self.t0 + minutos(11))
        remedios.passar(self.t0 + minutos(11) + H8)             # a próxima dose já cai fora do tratamento
        self.assertEqual(lib_db.avisos(desde=marco)[-1]["titulo"], "Fim do tratamento: Amoxicilina")
