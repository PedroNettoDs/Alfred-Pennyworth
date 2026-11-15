"""
Testes do Painel alcançado pelo aplicativo do celular: aparelho da tailnet (PAINEL_HOSTS_APP), login só com o token
semanal (app_token.py), sessão própria (a1) que acaba com o token, e o portão que deixa a tailnet só no Painel.

    cd services/inbox
    venv/bin/python -m unittest tests.test_painel_app -v

Offline, no ambiente de tests.test_painel. O cliente de teste se apresenta como o celular (IP da tailnet) chamando
o IP do computador na tailnet.
"""

import asyncio
import json
import os
import time
from datetime import timedelta
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_painel import BASE, TMP, TOKEN, Base, agenda, core, main, painel, whatsapp
from _shared import lib_db
import app_token
import painel_app
import remedios

APP = "http://100.95.104.56:7073"     # o computador na tailnet (PAINEL_HOSTS_APP)
CELULAR = "100.117.66.32"
MESTRE = {"Authorization": f"Bearer {TOKEN}"}


class BaseApp(Base):
    def setUp(self):
        super().setUp()
        (TMP / "app_token.json").unlink(missing_ok=True)
        for p in (mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": "100.95.104.56,batcomputador"}),
                  mock.patch.object(app_token, "ARQUIVO", TMP / "app_token.json")):
            p.start()
            self.addCleanup(p.stop)
        self.semanal = app_token.renovar()["token"]

    def celular(self, ip=CELULAR, base=APP, **kw) -> TestClient:
        return TestClient(main.app, base_url=base, client=(ip, 50000), **kw)

    def login(self, cli, token, origem=APP):
        return self.guardar(cli.post("/painel/login", data={"token": token}, headers={"Origin": origem},
                                     follow_redirects=False))

    def entrar_app(self) -> TestClient:
        cli = self.celular()
        self.assertEqual(self.login(cli, self.semanal).status_code, 303)
        return cli


class TestRedeDoApp(BaseApp):
    def test_desligado_a_tailnet_e_recusada(self):
        with mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": ""}):
            r = self.guardar(self.celular().get("/painel/login"))
            self.assertEqual((r.status_code, r.json()["erro"]), (403, painel.MSG_LOOPBACK))
            self.assertEqual(self.login(self.celular(), self.semanal).status_code, 403)

    def test_host_estranho_ou_ip_de_fora_da_tailnet_recusados(self):
        for ip, base in ((CELULAR, "http://evil.example:7073"),      # DNS rebinding: Host que não é o do computador
                         (CELULAR, "http://127.0.0.1:7073"),         # a tailnet não se passa por local
                         ("192.168.1.2", APP), ("172.17.0.2", APP),  # LAN e container não são a tailnet
                         ("127.0.0.1", APP)):                        # nem um proxy local com o Host da tailnet
            r = self.guardar(self.celular(ip, base).get("/painel/login"))
            self.assertEqual(r.status_code, 403, (ip, base))
        for base in (APP, "http://batcomputador:7073", "http://BatComputador"):
            self.assertEqual(self.guardar(self.celular(base=base).get("/painel/login")).status_code, 200, base)

    def test_tailnet_so_alcanca_o_painel(self):
        cli = self.celular()
        for url in ("/health", "/vivo", "/pendencias", "/whatsapp/status", "/agenda/alfred.ics", "/painelx", "/docs"):
            self.assertEqual(self.guardar(cli.get(url, headers=MESTRE)).status_code, 403, url)
        self.assertEqual(self.guardar(cli.post("/ciclo", headers=MESTRE)).status_code, 403)
        self.assertEqual(self.guardar(cli.get("/painel", follow_redirects=False)).status_code, 303)   # → login
        # quem não é da tailnet continua como antes (container com o token mestre)
        self.assertEqual(self.guardar(self.cliente("172.17.0.2").get("/pendencias", headers=MESTRE)).status_code, 200)

    def test_audio_da_pendencia_pela_tailnet_so_com_a_sessao(self):
        r = self.guardar(self.celular().get("/pendencias/x/audio", headers=MESTRE))
        self.assertEqual(r.status_code, 401)                 # o token mestre não vale de fora da máquina
        r = self.guardar(self.entrar_app().get("/pendencias/x/audio", headers=MESTRE))
        self.assertEqual(r.status_code, 404)                 # a sessão do aplicativo passa; a pendência é que não existe


class TestSessaoDoApp(BaseApp):
    def test_token_mestre_recusado_pela_tailnet(self):
        cli = self.celular()
        r = self.login(cli, TOKEN)
        self.assertEqual(r.status_code, 401)
        self.assertIn("Token inválido", r.text)
        self.assertIn("token semanal", r.text)               # a tela pede o token certo
        self.assertIsNone(cli.cookies.get("alfred_painel"))

    def test_token_semanal_recusado_em_127(self):
        cli = self.cliente()
        self.assertEqual(self.login(cli, self.semanal, BASE).status_code, 401)
        self.assertIsNone(cli.cookies.get("alfred_painel"))
        self.entrar()                                        # o mestre continua entrando em casa

    def test_token_semanal_entra_e_o_cookie_vence_com_o_token(self):
        self.semanal = app_token.renovar(time.time() - 6 * 86400)["token"]   # falta 1 dia para vencer
        cli = self.celular()
        r = self.login(cli, self.semanal)
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        self.assertTrue(cookie.startswith("alfred_painel=a1."), cookie[:20])
        max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
        self.assertTrue(86300 < max_age <= 86400, max_age)   # o que resta do token, não os 7 dias da sessão
        self.assertIn("HttpOnly", cookie)
        r = self.guardar(cli.get("/painel/api/contadores"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["x-csrf-token"])           # de onde o aplicativo tira o CSRF
        self.assertEqual(self.guardar(cli.get("/painel")).status_code, 200)

    def test_sessao_local_nao_vale_na_tailnet_nem_o_contrario(self):
        local = self.entrar().cookies.get("alfred_painel")
        do_app = self.entrar_app().cookies.get("alfred_painel")
        self.assertTrue(local.startswith("v1.") and do_app.startswith("a1."))
        r = self.guardar(self.celular(cookies={"alfred_painel": local}).get("/painel/api/contadores"))
        self.assertEqual(r.status_code, 401)
        r = self.guardar(self.cliente(cookies={"alfred_painel": do_app}).get("/painel/api/contadores"))
        self.assertEqual(r.status_code, 401)
        # trocar só o prefixo não adianta: a assinatura do aplicativo leva o token da semana
        forjado = "a1." + local.split(".", 1)[1]
        r = self.guardar(self.celular(cookies={"alfred_painel": forjado}).get("/painel/api/contadores"))
        self.assertEqual(r.status_code, 401)

    def test_gerar_outro_token_derruba_o_aplicativo(self):
        cli = self.entrar_app()
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).status_code, 200)
        novo = app_token.renovar()["token"]
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).status_code, 401)
        self.assertEqual(self.login(self.celular(), self.semanal).status_code, 401)   # o antigo não entra mais
        self.assertEqual(self.login(self.celular(), novo).status_code, 303)

    def test_token_vencido_derruba_o_aplicativo(self):
        cli = self.entrar_app()
        with mock.patch.object(app_token, "atual", lambda *a, **k: None):   # o que app_token.atual devolve ao vencer
            self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).status_code, 401)

    def test_post_do_aplicativo_exige_csrf_e_a_origem_da_tailnet(self):
        cli = self.entrar_app()
        csrf = self.guardar(cli.get("/painel/api/contadores")).headers["x-csrf-token"]
        self.assertEqual(self.acao(cli, "/painel/sair", csrf=False, origem=APP).status_code, 403)
        self.assertEqual(self.acao(cli, "/painel/sair", csrf=csrf, origem=BASE).status_code, 403)
        self.assertEqual(self.acao(cli, "/painel/sair", csrf=csrf, origem="http://evil.example").status_code, 403)
        self.assertEqual(self.acao(cli, "/painel/sair", csrf=csrf, origem=APP).status_code, 200)
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).status_code, 401)   # saiu

    def tearDown(self):
        for r in self.respostas:   # nem o token mestre nem o semanal aparecem no que o Painel devolve
            for segredo in (TOKEN, self.semanal):
                self.assertNotIn(segredo, r.text)
                self.assertNotIn(segredo, str(r.headers))


class TestTelaAplicativo(BaseApp):
    def test_tela_mostra_endereco_token_validade_e_qr(self):
        cli = self.entrar()
        r = cli.get("/painel/aplicativo")                    # fora de self.respostas: esta tela mostra o token
        self.assertEqual(r.status_code, 200)
        for trecho in (APP, self.semanal, "Vale até", 'src="data:image/svg+xml', "Gerar novo token"):
            self.assertIn(trecho, r.text)
        self.assertIn(f"{time.localtime(app_token.atual()['expira_em']).tm_mday:02d}/", r.text)

    def test_sem_sessao_vai_para_o_login(self):
        r = self.guardar(self.cliente().get("/painel/aplicativo", follow_redirects=False))
        self.assertEqual(r.status_code, 303)

    def test_tela_e_botao_so_no_computador(self):
        cli = self.entrar_app()
        csrf = self.guardar(cli.get("/painel/api/contadores")).headers["x-csrf-token"]
        r = self.guardar(cli.get("/painel/aplicativo"))
        self.assertEqual((r.status_code, r.json()["erro"]), (403, painel.MSG_LOOPBACK))
        r = self.acao(cli, "/painel/api/aplicativo/token", csrf=csrf, origem=APP)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(app_token.atual()["token"], self.semanal)          # o aplicativo não troca o próprio token

    def test_gerar_novo_token(self):
        cli = self.entrar()
        self.assertEqual(self.acao(cli, "/painel/api/aplicativo/token", csrf=False).status_code, 403)
        r = self.acao(cli, "/painel/api/aplicativo/token")
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        novo = app_token.atual()
        self.assertNotEqual(novo["token"], self.semanal)
        self.assertIsNone(novo["enviado_em"])                               # o laço manda este ao WhatsApp
        self.assertNotIn(novo["token"], r.text)

    def test_recurso_desligado_explica_como_ligar(self):
        cli = self.entrar()
        with mock.patch.dict(os.environ, {"PAINEL_HOSTS_APP": ""}):
            r = self.guardar(cli.get("/painel/aplicativo"))
            self.assertEqual(r.status_code, 200)
            self.assertIn("PAINEL_HOSTS_APP", r.text)
            self.assertNotIn("data:image/svg", r.text)
            self.assertEqual(self.acao(cli, "/painel/api/aplicativo/token").status_code, 409)

    def test_api_recem_ligada_gera_o_primeiro_token(self):
        (TMP / "app_token.json").unlink()
        r = self.entrar().get("/painel/aplicativo")
        self.assertIn(app_token.atual()["token"], r.text)

    def test_sem_o_segno_mostra_so_o_texto(self):
        with mock.patch.dict("sys.modules", {"segno": None}):               # import segno → ImportError
            r = self.entrar().get("/painel/aplicativo")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.semanal, r.text)
        self.assertNotIn("data:image/svg", r.text)


class _Pedido:
    """O mínimo de um Request para dirigir painel_app._fluxo sem o TestClient (que trava num fluxo sem fim)."""
    caiu = False

    async def is_disconnected(self):
        return self.caiu


class TestJsonDoApp(BaseApp):
    """As rotas JSON que o aplicativo lê, com a sessão dele (pela tailnet)."""

    def setUp(self):
        super().setUp()
        self.cli = self.entrar_app()
        self.token_csrf = self.guardar(self.cli.get("/painel/api/contadores")).headers["x-csrf-token"]

    def post(self, url, corpo=None):
        return self.acao(self.cli, url, corpo, csrf=self.token_csrf, origem=APP)

    def test_avisos_ultimos_e_desde(self):
        ids = [lib_db.registrar_aviso("aviso", f"Aviso {n}", "texto", "/painel/agenda") for n in (1, 2, 3)]
        avisos = self.guardar(self.cli.get("/painel/api/avisos")).json()["avisos"]
        self.assertEqual([a["id"] for a in avisos], ids)
        self.assertEqual(set(avisos[0]), {"id", "quando", "tipo", "titulo", "texto", "link", "ref"})
        r = self.guardar(self.cli.get("/painel/api/avisos", params={"desde": ids[0]}))
        self.assertEqual([a["titulo"] for a in r.json()["avisos"]], ["Aviso 2", "Aviso 3"])
        r = self.guardar(self.cli.get("/painel/api/avisos", params={"limite": 1}))
        self.assertEqual([a["id"] for a in r.json()["avisos"]], ids[2:])
        self.assertEqual(self.guardar(self.cli.get("/painel/api/avisos", params={"desde": "abc"})).status_code, 422)

    def test_json_sem_sessao_401(self):
        cli = self.celular()
        for url in ("/painel/api/avisos", "/painel/api/avisos/fluxo", "/painel/api/lembretes"):
            self.assertEqual(self.guardar(cli.get(url)).status_code, 401, url)
        self.assertEqual(self.guardar(cli.post("/painel/api/doses/remedio:x:1/tomei")).status_code, 401)

    def test_fluxo_entrega_os_novos_e_acaba_com_a_sessao(self):
        lib_db.registrar_aviso("aviso", "Antigo")
        sessao = {"vale": True}

        async def roteiro():
            fluxo = painel_app._fluxo(_Pedido(), None)
            self.assertEqual(await anext(fluxo), ": ok\n\n")
            lib_db.registrar_aviso("remedio", "Tomar Amoxicilina", "Dose das 14:05")
            evento = await anext(fluxo)                       # sem `desde`, o "Antigo" não volta
            self.assertTrue(evento.startswith("data: ") and evento.endswith("\n\n"))
            aviso = json.loads(evento[6:])
            self.assertEqual((aviso["tipo"], aviso["titulo"]), ("remedio", "Tomar Amoxicilina"))
            sessao["vale"] = False                            # o token da semana venceu ou foi trocado
            with self.assertRaises(StopAsyncIteration):
                await anext(fluxo)
            sessao["vale"] = True
            do_inicio = painel_app._fluxo(_Pedido(), 0)       # com `desde`, o aplicativo recupera o que perdeu
            await anext(do_inicio)
            self.assertEqual(json.loads((await anext(do_inicio))[6:])["titulo"], "Antigo")
            self.assertEqual(json.loads((await anext(do_inicio))[6:])["id"], aviso["id"])
            caido = _Pedido()
            caido.caiu = True                                 # o celular perdeu a rede: o fluxo acaba
            outro = painel_app._fluxo(caido, 0)
            await anext(outro)
            with self.assertRaises(StopAsyncIteration):
                await anext(outro)

        with mock.patch.object(painel_app, "FLUXO_A_CADA_S", 0), \
                mock.patch.object(painel, "_nonce_da_sessao", lambda request: "n" if sessao["vale"] else None):
            asyncio.run(roteiro())

    def test_lembretes_junta_doses_recados_agenda_e_pendencias(self):
        pend = self.criar_wa()
        item = agenda.adicionar("Dentista", core._now() + timedelta(days=2), tipo="evento")
        agora = core._now()
        remedios.adicionar("Amoxicilina", 8, agora=agora)
        remedios.passar(agora)
        d = self.guardar(self.cli.get("/painel/api/lembretes")).json()
        self.assertEqual(set(d), {"doses", "recados", "agenda", "pendencias"})
        (dose,) = d["doses"]
        self.assertEqual((dose["tipo"], dose["texto"]), ("remedio", "Tomar Amoxicilina"))
        self.assertEqual([(i["id"], i["titulo"], i["pode_concluir"]) for i in d["agenda"]], [(item["id"], "Dentista", True)])
        self.assertFalse([k for k in d["agenda"][0] if k.startswith("_")])
        self.assertEqual([(p["id"], p["tipo"]) for p in d["pendencias"]], [(pend["id"], "whatsapp")])
        self.assertEqual(d["recados"], [])
        recado = {"id": pend["id"], "titulo": "Carlos", "contato": "Carlos", "texto": "Oi", "quando": "2026-11-12T10:00"}
        with mock.patch.object(whatsapp, "recados", lambda: [recado]):   # o recado não aparece duas vezes
            d = self.guardar(self.cli.get("/painel/api/lembretes")).json()
        self.assertEqual((d["recados"], d["pendencias"]), ([recado], []))

    def test_tomei_fecha_a_dose_e_repetido_da_409(self):
        agora = core._now()
        remedios.adicionar("Amoxicilina", 8, agora=agora)
        remedios.passar(agora)
        dose = self.guardar(self.cli.get("/painel/api/lembretes")).json()["doses"][0]["id"]
        r = self.acao(self.cli, f"/painel/api/doses/{dose}/tomei", csrf=False, origem=APP)
        self.assertEqual(r.status_code, 403)                                  # sem CSRF não fecha nada
        r = self.post(f"/painel/api/doses/{dose}/tomei")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Anotado: Amoxicilina", r.json()["mensagem"])
        self.assertEqual(remedios.recados(), [])
        self.assertEqual(remedios.listar()[0]["ultima"]["via"], "app")
        self.assertEqual(self.post(f"/painel/api/doses/{dose}/tomei").status_code, 409)
        self.assertEqual(self.post("/painel/api/doses/isto-nao-e-uma-dose/tomei").status_code, 409)

    def test_o_aplicativo_usa_as_acoes_que_o_painel_ja_tem(self):
        pend = self.criar_wa()
        item = agenda.adicionar("Dentista", core._now() + timedelta(days=2), tipo="evento")
        self.assertEqual(self.post(f"/painel/api/agenda/{item['id']}/feito").status_code, 200)
        self.assertEqual(self.post(f"/painel/api/pendencias/{pend['id']}/ignorar").status_code, 200)
        d = self.guardar(self.cli.get("/painel/api/lembretes")).json()
        self.assertEqual((d["agenda"], d["pendencias"]), ([], []))
