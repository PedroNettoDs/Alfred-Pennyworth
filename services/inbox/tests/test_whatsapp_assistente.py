"""
Testes das sessões do WhatsApp e do papel "assistente" (o número do Alfred).

    cd services/inbox
    venv/bin/python -m unittest tests.test_whatsapp_assistente -v

Mesmo ambiente de tests.test_whatsapp (LLM/TTS mockados, envios simulados, vault temporário). A sessão
assistente é a conta alfred_whatsapp de perfis.yml (instância alfred-assistente).
"""

import itertools
import queue
import threading
import time
from unittest import mock

from tests.test_whatsapp import Base, TMP, payload, core, whatsapp, lib_whatsapp
from _shared import lib_memoria
import agenda
import conversa

PEDRO = "5514999990000"          # WHATSAPP_NUMERO_PEDRO do ambiente de teste
ALFRED = "5514911112222"         # número pareado na sessão assistente
MARINA = "5511977776666"
SITE = "https://exemplo.test"
_ids = itertools.count(1)


def msg(texto, numero=MARINA, nome="Marina Souza", from_me=False, citando=""):
    message = {"conversation": texto}
    if citando:
        message = {"extendedTextMessage": {"text": texto, "contextInfo": {
            "stanzaId": "X", "quotedMessage": {"conversation": citando}}}}
    return {"event": "messages.upsert", "instance": "alfred-assistente", "apikey": "CHAVE-DE-TESTE",
            "sender": f"{ALFRED}@s.whatsapp.net",
            "data": {"key": {"remoteJid": f"{numero}@s.whatsapp.net", "fromMe": from_me, "id": f"ASSIST{next(_ids):04d}"},
                     "pushName": nome, "message": message, "messageTimestamp": int(time.time())}}


def turno_falso(*eventos):
    """Um Turno do conversa.py já com os eventos na fila (o motor da conversa não roda nos testes)."""
    t = mock.Mock(id="t1", conversa_id="c1", fila=queue.Queue())
    for ev in eventos:
        t.fila.put(ev)
    return t


FIM = {"tipo": "fim", "texto": "Pronto, senhor.", "parado": False}
ERRO = {"tipo": "erro", "erro": "Ollama fora do ar"}


def _fake_llm(prompt, **kw):
    if "SEU número de WhatsApp" not in prompt:
        return {"rascunho": "Recebi, te retorno.", "compromisso": {"titulo": "", "data": "", "hora": ""}}
    conversa = prompt.split("ÚLTIMAS FALAS da conversa")[-1]
    if "orçamento" in conversa:
        return {"seria": True, "previsao": "hoje depois das 16h", "resumo": "Marina, da Acme, pede orçamento de um site",
                "motivo": "pedido concreto de trabalho", "urgencia": "media"}
    return {"seria": False, "previsao": "", "resumo": "saudação", "motivo": "só cumprimentou", "urgencia": "baixa"}


class BaseAssistente(Base):
    def setUp(self):
        super().setUp()
        for f in (TMP / "whatsapp_sessoes.json", TMP / "whatsapp_atendimentos.json", agenda.agenda_file()):
            if f.exists():
                f.unlink()
        cfg_base = whatsapp.carregar_cfg   # já devolve atraso_minutos de teste

        def cfg(nome=None):
            n, c = cfg_base(nome)
            return n, {**c, "assistente": {**(c.get("assistente") or {}), "site": SITE}}

        self.llm = mock.Mock(side_effect=_fake_llm)
        self.motor = mock.Mock(side_effect=lambda *a, **k: turno_falso(ERRO))   # conversa.enviar
        for p in (mock.patch.object(whatsapp, "carregar_cfg", cfg),
                  mock.patch.object(whatsapp, "PEDIDO_EM_THREAD", False),
                  mock.patch.object(whatsapp, "_conversa_do_whatsapp", lambda: "c1"),
                  mock.patch.object(conversa, "enviar", self.motor),
                  mock.patch.object(whatsapp, "ollama_generate_json", self.llm),
                  mock.patch.object(lib_memoria, "contexto", lambda *a, **k: ""),
                  mock.patch.object(agenda, "resumir_item", lambda aid: None)):
            p.start()
            self.addCleanup(p.stop)

    def receber(self, *a, **k):
        r = self.client.post("/whatsapp/webhook", params={"notificar": "true"}, json=msg(*a, **k),
                             headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def para(self, numero):
        return [e for e in self.enviados() if e["numero"] == numero]


class TestSessoes(BaseAssistente):
    def test_uma_sessao_por_conta(self):
        s = {x["nome"]: x for x in whatsapp.sessoes()}
        self.assertEqual((s["pedro_whatsapp"]["instancia"], s["pedro_whatsapp"]["papel"]), ("alfred", "pessoal"))
        self.assertEqual((s["alfred_whatsapp"]["instancia"], s["alfred_whatsapp"]["papel"]), ("alfred-assistente", "assistente"))

    def test_escolher_a_assistente_tira_o_papel_da_outra(self):
        s = {x["nome"]: x["papel"] for x in whatsapp.definir_papel("pedro_whatsapp", "assistente")}
        self.assertEqual(s, {"pedro_whatsapp": "assistente", "alfred_whatsapp": "pessoal"})
        s = {x["nome"]: x["papel"] for x in whatsapp.definir_papel("pedro_whatsapp", "pessoal")}
        self.assertEqual(s, {"pedro_whatsapp": "pessoal", "alfred_whatsapp": "pessoal"})

    def test_papel_ou_sessao_invalidos(self):
        with self.assertRaises(ValueError):
            whatsapp.definir_papel("pedro_whatsapp", "chefe")
        with self.assertRaises(KeyError):
            whatsapp.definir_papel("nao_existe", "assistente")

    def test_sessao_pessoal_no_papel_assistente_recepciona(self):
        whatsapp.definir_papel("pedro_whatsapp", "assistente")
        d = self.client.post("/whatsapp/webhook", json=payload("evolution_spam.json"),
                             headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual(d["acao"], "saudacao", d)

    def test_qrcode_e_status_usam_a_instancia_da_sessao(self):
        caminhos = []
        with mock.patch.object(lib_whatsapp, "configurado", lambda: True), \
             mock.patch.object(lib_whatsapp, "_get", lambda path, timeout=30: caminhos.append(path) or
                               {"ok": True, "status": 200, "resposta": {"instance": {"state": "open"}}}):
            self.assertTrue(whatsapp.obter_qrcode("alfred_whatsapp")["conectado"])
            st = whatsapp.status()
        self.assertEqual(caminhos[0], "/instance/connectionState/alfred-assistente")
        self.assertEqual([(s["nome"], s["estado"]) for s in st["sessoes"]],
                         [("pedro_whatsapp", "open"), ("alfred_whatsapp", "open")])


class TestPedroFalaComOAlfred(BaseAssistente):
    """Pedro → número do Alfred: o pedido vai para o motor da conversa; com ele fora do ar, agenda direta."""

    def test_pedido_vai_ao_motor_e_a_resposta_volta_pelo_numero_do_alfred(self):
        self.motor.side_effect = lambda *a, **k: turno_falso(
            {"tipo": "ferramenta", "nome": "status_gpu", "estado": "ok"}, FIM)
        d = self.receber("como está a placa de vídeo?", numero=PEDRO, nome="Pedro")
        self.assertEqual((d["acao"], d["resposta"]), ("pedido", "Pronto, senhor."), d)
        (cid, texto), kw = self.motor.call_args
        self.assertEqual((cid, texto, kw), ("c1", "como está a placa de vídeo?", {}))   # o mesmo prompt do Painel
        conf = self.para(PEDRO)
        self.assertEqual([(e["conteudo"], e["instancia"]) for e in conf], [("Pronto, senhor.", "alfred-assistente")])
        self.assertEqual(agenda.listar(), [])
        self.llm.assert_not_called()

    def test_agenda_roda_sem_perguntar_e_comando_espera_o_sim(self):
        self.motor.side_effect = lambda *a, **k: turno_falso(
            {"tipo": "aprovar", "id": "ap1", "nome": "criar_compromisso", "argumentos": {"texto": "dentista sexta 9h"}}, FIM)
        with mock.patch.object(conversa, "aprovar") as aprovar:
            self.receber("marca dentista sexta 9h", numero=PEDRO, nome="Pedro")
        aprovar.assert_called_once_with("ap1")
        self.assertEqual([e["conteudo"] for e in self.para(PEDRO)], ["Pronto, senhor."])   # sem pergunta

        turno = turno_falso({"tipo": "aprovar", "id": "ap2", "nome": "executar_comando", "argumentos": {"comando": "df -h"}})
        self.motor.side_effect = lambda *a, **k: turno
        # fora do webhook de teste (que segura a trava de processar_mensagem_whatsapp), como a thread real
        m = whatsapp.normalizar_payload(msg("roda df -h", numero=PEDRO, nome="Pedro"))
        _, cfg = whatsapp.carregar_cfg("alfred_whatsapp")
        t = threading.Thread(target=whatsapp._pedido_de_pedro, args=(m, cfg, {}, True, True), daemon=True)
        t.start()
        self.addCleanup(turno.fila.put, FIM)   # se o teste falhar antes, a thread não fica presa
        for _ in range(300):
            if "responda" in self.para(PEDRO)[-1]["conteudo"]:
                break
            time.sleep(0.01)
        self.assertIn("responda *sim* ou *não*", self.para(PEDRO)[-1]["conteudo"])
        with mock.patch.object(conversa, "aprovar", return_value=True) as aprovar:
            d = self.receber("Sim.", numero=PEDRO, nome="Pedro")
        aprovar.assert_called_once_with("ap2")
        self.assertEqual(d["acao"], "aprovacao", d)
        turno.fila.put(FIM)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(whatsapp._APROVACAO_WA, {})

    def test_modelo_que_diz_que_fez_sem_ferramenta_e_cobrado(self):
        mentira = {"tipo": "fim", "texto": 'Tarefa "Falar com o Vinícius" adicionada à sua lista para hoje.', "parado": False}
        turnos = [turno_falso(mentira), turno_falso({"tipo": "ferramenta", "nome": "adicionar_tarefa", "estado": "ok"}, FIM)]
        self.motor.side_effect = lambda *a, **k: turnos.pop(0)
        d = self.receber("me lembre de falar com o Vinícius", numero=PEDRO, nome="Pedro")
        self.assertEqual((d["acao"], d["resposta"]), ("pedido", "Pronto, senhor."), d)
        self.assertEqual(self.motor.call_args.args[1], whatsapp.COBRANCA)
        self.assertEqual(agenda.listar(), [])

    def test_modelo_que_insiste_sem_ferramenta_cai_na_agenda(self):
        self.motor.side_effect = lambda *a, **k: turno_falso(
            {"tipo": "fim", "texto": 'Tarefa "Falar com o Vinícius" adicionada à sua lista para hoje.', "parado": False})
        with mock.patch.object(agenda, "detectar_compromisso", lambda *a, **k: None):
            d = self.receber("me lembre de falar com o Vinícius", numero=PEDRO, nome="Pedro")
        self.assertEqual(self.motor.call_count, 2)
        self.assertEqual(d["acao"], "agenda", d)
        self.assertEqual(len(agenda.listar()), 1)
        self.assertIn("Anotado na agenda", self.para(PEDRO)[-1]["conteudo"])

    def test_audio_e_transcrito_pelo_whisper(self):
        self.motor.side_effect = lambda *a, **k: turno_falso(FIM)
        m = {**whatsapp.normalizar_payload(msg("x", numero=PEDRO, nome="Pedro")), "tipo_msg": "audio", "texto": ""}
        _, cfg = whatsapp.carregar_cfg("alfred_whatsapp")
        tr = mock.Mock(return_value={"texto": "preciso lembrar de pagar o Alfred", "metodo": "whisper"})
        with mock.patch.object(whatsapp, "transcrever_mensagem", tr):
            whatsapp._pedido_de_pedro(m, cfg, {}, True, True)
        self.assertEqual(tr.call_args.kwargs["motor"], "whisper")
        self.assertIn("Alfred", tr.call_args.kwargs["dica"])
        self.assertEqual(self.motor.call_args.args[1], "preciso lembrar de pagar o Alfred")

    def test_pedido_durante_outro_avisa_que_esta_ocupado(self):
        self.motor.side_effect = RuntimeError("já há um turno em andamento nesta conversa")
        self.receber("e a agenda?", numero=PEDRO, nome="Pedro")
        self.assertIn("pedido anterior", self.para(PEDRO)[-1]["conteudo"])

    def test_instancia_nova_ganha_o_webhook_da_padrao(self):
        achados = {"alfred": {"url": "http://x/whatsapp/webhook-async", "headers": {"apikey": "K"}, "webhookBase64": True}}
        with mock.patch.object(lib_whatsapp, "obter_webhook", lambda inst=None: achados.get(inst or "alfred", {})), \
             mock.patch.object(lib_whatsapp, "instancia", lambda: "alfred"), \
             mock.patch.object(lib_whatsapp, "configurar_webhook", return_value={"ok": True}) as conf:
            whatsapp._garantir_webhook("alfred-assistente")
            whatsapp._garantir_webhook("alfred")          # a padrão não é mexida
        conf.assert_called_once_with("http://x/whatsapp/webhook-async", headers={"apikey": "K"}, base64_midia=True,
                                     inst="alfred-assistente")

    # ── motor da conversa fora do ar (o padrão de self.motor): o lembrete não se perde ──
    def test_lembrete_com_data_vai_para_a_agenda(self):
        comp = {"tipo": "lembrete", "titulo": "Ligar para o contador", "inicio": core._now().replace(hour=10, minute=0),
                "dia_inteiro": False, "fim": None, "local": "", "confianca": 0.9, "trecho": ""}
        with mock.patch.object(agenda, "detectar_compromisso", lambda *a, **k: comp):
            d = self.receber("me lembra de ligar para o contador hoje às 10h", numero=PEDRO, nome="Pedro")
        self.assertEqual(d["acao"], "agenda", d)
        self.assertEqual([i["titulo"] for i in agenda.listar()], ["Ligar para o contador"])
        conf = self.para(PEDRO)
        self.assertEqual(len(conf), 1)
        self.assertIn("Anotado na agenda", conf[0]["conteudo"])
        self.assertEqual(conf[0]["instancia"], "alfred-assistente")   # quem confirma é o número do Alfred
        self.llm.assert_not_called()                                  # Pedro nunca passa pela triagem de terceiros

    def test_pedido_sem_data_vira_lembrete_de_hoje(self):
        with mock.patch.object(agenda, "detectar_compromisso", lambda *a, **k: None):
            d = self.receber("preciso me lembrar de renovar o domínio", numero=PEDRO, nome="Pedro")
        self.assertEqual(d["acao"], "agenda", d)
        item = agenda.listar()[0]
        self.assertEqual((item["tipo"], item["dia_inteiro"], item["inicio"]),
                         ("lembrete", True, core._now().date().isoformat()))
        self.assertIn("renovar o domínio", item["titulo"])
        self.assertEqual(self.pendencias(), [])

    def test_resposta_citando_o_aviso_sai_pelo_numero_do_alfred(self):
        self.receber("Oi, sou a Marina da Acme, queria um orçamento de site")
        pid = self.pendencias()[0]["id"]
        d = self.receber("Oi Marina, te ligo às 16h.", numero=PEDRO, nome="Pedro", citando=f"🎩 ...\nref: {pid}")
        self.assertEqual(d["acao"], "resposta_registrada", d)
        ultimo = self.para(MARINA)[-1]
        self.assertEqual((ultimo["conteudo"], ultimo["instancia"]), ("Oi Marina, te ligo às 16h.", "alfred-assistente"))


class TestRecepcao(BaseAssistente):
    def test_primeira_mensagem_so_recebe_a_saudacao(self):
        d = self.receber("oi, tudo bem?")
        self.assertEqual(d["acao"], "saudacao", d)
        env = self.para(MARINA)
        self.assertEqual(len(env), 1)
        self.assertIn("assistente do Pedro", env[0]["conteudo"])
        self.assertTrue(env[0]["conteudo"].startswith("🎩 *Alfred:* "), env[0]["conteudo"])   # quem fala é o Alfred
        self.assertNotIn(SITE, env[0]["conteudo"])
        self.assertEqual(env[0]["instancia"], "alfred-assistente")
        self.assertEqual((self.pendencias(), self.para(PEDRO)), ([], []))

    def test_conversa_fiada_depois_da_saudacao_fica_sem_resposta(self):
        self.receber("oi")
        d = self.receber("e aí?")
        self.assertEqual(d["acao"], "ignorada", d)
        self.assertEqual(len(self.para(MARINA)), 1)
        self.assertEqual(self.pendencias(), [])

    def test_pergunta_sobre_pedro_recebe_apresentacao_sem_virar_bate_papo(self):
        fala = {"seria": False, "resposta": "Sou o assistente do Pedro. Ele desenvolve automações com IA local. "
                                            "Veja http://golpe.test", "previsao": "", "resumo": "curioso",
                "motivo": "perguntou quem é", "urgencia": "baixa"}
        self.llm.side_effect = lambda prompt, **kw: fala
        self.receber("oi, com quem eu falo?")                       # 1ª mensagem: só a saudação
        self.assertEqual(len(self.para(MARINA)), 1)
        d = self.receber("como assim assistente? o que o Pedro faz?")
        self.assertEqual(d["acao"], "apresentacao", d)
        ultimo = self.para(MARINA)[-1]["conteudo"]
        self.assertTrue(ultimo.startswith("🎩 *Alfred:* Sou o assistente do Pedro."), ultimo)
        self.assertNotIn("golpe.test", ultimo)                      # link do modelo não sai
        prompt = self.llm.call_args.args[0]                         # o modelo vê o que o Alfred já disse (não repetir)
        self.assertIn("ALFRED: Olá! Aqui é o Alfred", prompt)
        self.assertIn("CONTATO: como assim assistente?", prompt)
        self.assertEqual((self.pendencias(), self.para(PEDRO)), ([], []))
        self.assertNotIn("memória", prompt)                         # a memória de Pedro não vai ao prompt

    def test_conversa_travada_chama_pedro_em_vez_de_repetir(self):
        fala = {"seria": False, "resposta": "Só cuido dos recados do Pedro. Há algum assunto para ele?", "previsao": "",
                "resumo": "pessoa fala de batata", "motivo": "sem assunto", "urgencia": "baixa"}
        self.llm.side_effect = lambda prompt, **kw: dict(fala)
        self.receber("oi")
        self.receber("batata")                                      # 1ª resposta do Alfred
        d = self.receber("o assunto é batata, qual parte vc não entende?")   # ia repetir → chama Pedro
        self.assertEqual(d["acao"], "pendencia_criada", d)
        ultimo = self.para(MARINA)[-1]["conteudo"]
        self.assertIn(whatsapp.TRAVOU, ultimo)
        self.assertNotIn("Só cuido dos recados", ultimo)
        self.assertEqual(len(self.pendencias()), 1)
        self.assertIn("travou", self.pendencias()[0]["titulo"] + str(self.pendencias()[0]))
        self.assertTrue(self.para(PEDRO))                           # aviso no número de Pedro
        n = len(self.para(MARINA))
        self.receber("alô?")                                        # depois disso o Alfred só repassa
        self.assertEqual(len(self.para(MARINA)), n)

    def test_resumo_do_chat_volta_no_prompt_seguinte_e_o_historico_e_cortado(self):
        n = iter(range(100))

        def llm(prompt, **kw):
            i = next(n)
            return {"seria": False, "resposta": "abcdefghijklmnop"[i] * 30, "situacao": f"Marina quer falar de site ({i})",
                    "previsao": "", "resumo": "papo", "motivo": "sem assunto", "urgencia": "baixa"}
        self.llm.side_effect = llm
        self.receber("primeira fala bem antiga")
        for i in range(6):
            self.receber(f"fala {i}")
        prompt = self.llm.call_args.args[0]
        self.assertIn("Marina quer falar de site (5)", prompt)       # o resumo da vez anterior
        self.assertNotIn("primeira fala bem antiga", prompt)         # só as últimas FALAS_NO_PROMPT
        self.assertIn("CONTATO: fala 5", prompt)

    def test_teto_de_respostas_chama_pedro(self):
        n = iter(range(100))
        self.llm.side_effect = lambda prompt, **kw: {
            "seria": False, "resposta": 'abcdefghijklmnop'[next(n)] * 30, "previsao": "", "resumo": "papo",
            "motivo": "sem assunto", "urgencia": "baixa"}
        self.receber("oi")
        for _ in range(8):
            self.assertEqual(self.receber("e aí?")["acao"], "apresentacao")
        self.assertEqual(self.receber("e aí?")["acao"], "pendencia_criada")

    def test_conversa_seria_recebe_previsao_site_e_avisa_pedro(self):
        self.receber("oi")
        d = self.receber("Sou a Marina, da Acme. Queria um orçamento de um site institucional.")
        self.assertEqual(d["acao"], "pendencia_criada", d)
        env = self.para(MARINA)
        self.assertEqual(len(env), 2)                                  # saudação + previsão, nada mais
        self.assertIn("previsão de retorno é hoje depois das 16h", env[1]["conteudo"])
        self.assertIn(SITE, env[1]["conteudo"])
        self.assertNotIn("assistente do Pedro", env[1]["conteudo"])    # a saudação não se repete
        p = self.pendencias()[0]
        self.assertEqual(p["whatsapp"]["instancia"], "alfred-assistente")
        self.assertEqual(len(p["whatsapp"]["mensagens"]), 2)           # a pendência leva a conversa inteira
        self.assertIn("hoje depois das 16h", p["evento"]["resumo"])
        aviso = [e for e in self.para(PEDRO) if e["tipo"] == "texto"]
        self.assertEqual(len(aviso), 1)
        self.assertIn(f"ref: {p['id']}", aviso[0]["conteudo"])
        self.assertEqual(aviso[0]["instancia"], "alfred-assistente")

    def test_seria_logo_de_cara_saudacao_e_previsao_numa_mensagem(self):
        d = self.receber("Boa tarde, sou a Marina da Acme e preciso de um orçamento.")
        self.assertEqual(d["acao"], "pendencia_criada", d)
        env = self.para(MARINA)
        self.assertEqual(len(env), 1)
        self.assertIn("assistente do Pedro", env[0]["conteudo"])
        self.assertIn("hoje depois das 16h", env[0]["conteudo"])

    def test_recado_serio_entra_na_lista_de_recados_com_link_do_detalhe(self):
        self.assertEqual(whatsapp.recados(), [])
        self.receber("Oi, sou a Marina da Acme, queria um orçamento de site")
        (r,) = whatsapp.recados()
        pid = self.pendencias()[0]["id"]
        self.assertEqual((r["id"], r["contato"]), (pid, "Marina Souza"))
        self.assertIn("orçamento", r["texto"])
        import main
        (pet,) = main._recados_para_o_pet()
        self.assertEqual((pet["texto"], pet["link"]), (r["titulo"], f"http://127.0.0.1:7073/painel/pendencias/{pid}"))
        self.assertTrue(pet["detalhe"].startswith("Marina Souza\n") and pet["detalhe"].endswith(r["texto"]))

    def test_depois_da_previsao_o_alfred_so_repassa(self):
        self.receber("Sou a Marina da Acme, preciso de um orçamento.")
        self.llm.reset_mock()
        d = self.receber("Pode ser para a semana que vem.")
        self.assertEqual(d["acao"], "agrupada", d)
        self.assertEqual(len(self.para(MARINA)), 1)                    # silêncio com o contato
        self.assertEqual(len(self.pendencias()), 1)
        self.assertIn("mandou mais uma mensagem", self.para(PEDRO)[-1]["conteudo"])
        self.llm.assert_not_called()

    def test_silencio_vencido_recomeca_pela_saudacao(self):
        self.receber("oi")
        with mock.patch.object(core, "_now", lambda agora=core._now(): agora + whatsapp.timedelta(hours=13)):
            d = self.receber("oi de novo")
        self.assertEqual(d["acao"], "saudacao", d)
        self.assertEqual(len(self.para(MARINA)), 2)

    def test_modelo_fora_do_ar_avisa_pedro_sem_prometer_previsao(self):
        self.llm.side_effect = lambda prompt, **kw: None
        with mock.patch.object(whatsapp, "ollama_generate", lambda *a, **k: ""):
            d = self.receber("Preciso falar com o Pedro sobre o contrato.")
        self.assertEqual(d["acao"], "pendencia_criada", d)
        env = self.para(MARINA)
        self.assertEqual(len(env), 1)
        self.assertNotIn("previsão", env[0]["conteudo"])
        self.assertEqual(len(self.pendencias()), 1)

    def test_previsao_do_modelo_nao_leva_link_nem_texto_longo(self):
        self.llm.side_effect = lambda prompt, **kw: {"seria": True, "previsao": "veja http://golpe.test agora",
                                                     "resumo": "r", "motivo": "m", "urgencia": "alta"}
        self.receber("Preciso de um orçamento urgente.")
        txt = self.para(MARINA)[0]["conteudo"]
        self.assertNotIn("golpe.test", txt)
        self.assertIn("assim que possível", txt)

    def test_eco_grupo_e_repetida_sao_ignorados(self):
        self.assertEqual(self.receber("aviso", from_me=True)["motivo"], "mensagem enviada pelo número do Alfred")
        m = msg("oi")
        for esperado in ("saudacao", "duplicada"):
            d = self.client.post("/whatsapp/webhook", json=m, headers={"apikey": "CHAVE-DE-TESTE"}).json()
            self.assertEqual(d["acao"], esperado, d)
        self.assertEqual(len(self.para(MARINA)), 1)


class TestSessaoPessoalVeAOutra(BaseAssistente):
    def test_aviso_do_alfred_nao_vira_pendencia_na_sessao_pessoal(self):
        self.receber("oi")                                             # a sessão assistente aprende o próprio número
        p = payload("evolution_texto.json")
        p["data"]["key"]["remoteJid"] = f"{ALFRED}@s.whatsapp.net"
        d = self.client.post("/whatsapp/webhook", json=p, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual((d["acao"], d["motivo"]), ("ignorada", "conversa com outra sessão do Alfred (tratada por ela)"))
        self.assertEqual(self.pendencias(), [])
