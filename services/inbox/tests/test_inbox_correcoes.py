"""
Regressões dos bugs achados nos testes funcionais com o LLM real (e-mail, jornal, pendências, agenda,
limpeza, podcast).

    cd services/inbox && venv/bin/python -m unittest tests.test_inbox_correcoes -v

Offline: LLM/TTS/notificações mockados, vault e logs temporários, nada toca o Thunderbird real.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

import yaml

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

os.environ.setdefault("SHELL_EXECUTOR_TOKEN", "token-de-teste")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

import core      # noqa: E402
import agenda    # noqa: E402
import limpeza   # noqa: E402
import semanal   # noqa: E402
from _shared import lib_mail  # noqa: E402


def _msg(mid, subject="Assunto", text="corpo", ics=None, links=None, frm="a@exemplo.com"):
    return {"message_id": mid, "from_addr": frm, "from_domain": frm.split("@")[1], "from_name": "Remetente",
            "subject": subject, "date_iso": "2026-10-26T08:00-03:00", "text": text, "links": links or [],
            "ics": ics or [], "is_bulk": False, "attachments": [], "read": False, "folder": "INBOX", "account": "x"}


def _parse(raw: str) -> dict:
    return lib_mail.parse_message(BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8")))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="alfred-inbox-correcoes-"))
        (cls.tmp / "vault").mkdir()
        cls.env = mock.patch.dict(os.environ, {"VAULT_ALFRED": str(cls.tmp / "vault"), "VAULT_NOTICIAS": "",
                                               "INBOX_AUTOPLAY": "false", "EVOLUTION_URL": "",
                                               "WHATSAPP_DRY_RUN": "true"})
        cls.env.start()
        cls.patches = [
            mock.patch.object(core, "_notify", lambda *a, **k: None),
            mock.patch.object(core, "_iniciar_aguardador", lambda *a, **k: None),
            mock.patch.object(core, "_tem_gui", lambda: False),
            mock.patch.object(core, "SEEN_FILE", cls.tmp / "inbox_seen.json"),
            mock.patch.object(limpeza, "REGISTRO_FILE", cls.tmp / "inbox_categorias.json"),
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
        v = self.tmp / "vault"
        for d in v.iterdir():
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()
        for f in self.tmp.glob("*.json"):
            f.unlink()


# ── 1. perfil do Thunderbird ≥ 158 em ~/.config/thunderbird ────────────────

class TestPerfilXDG(unittest.TestCase):
    def _criar(self, base: Path) -> Path:
        perfil = base / "abc123.default"
        perfil.mkdir(parents=True)
        (base / "profiles.ini").write_text("[Profile0]\nName=default\nIsRelative=1\nPath=abc123.default\n"
                                           "Default=1\n", encoding="utf-8")
        return perfil

    def test_config_thunderbird_do_home(self):
        with tempfile.TemporaryDirectory() as home:
            perfil = self._criar(Path(home) / ".config" / "thunderbird")
            with mock.patch.dict(os.environ, {"HOME": home, "THUNDERBIRD_PROFILE": "", "XDG_CONFIG_HOME": ""}):
                self.assertEqual(lib_mail.find_thunderbird_profile(), perfil)

    def test_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as xdg:
            perfil = self._criar(Path(xdg) / "thunderbird")
            with mock.patch.dict(os.environ, {"HOME": home, "THUNDERBIRD_PROFILE": "", "XDG_CONFIG_HOME": xdg}):
                self.assertEqual(lib_mail.find_thunderbird_profile(), perfil)


# ── 2. pendência de WhatsApp ("email": None) não derruba o ciclo ───────────

class TestJaNaFila(unittest.TestCase):
    def test_pendencia_whatsapp_sem_email(self):
        pend = [{"id": "wa1", "tipo": "whatsapp", "status": "aguardando", "titulo": "Carlos", "email": None,
                 "evento": None}]
        self.assertIsNone(core._ja_na_fila(pend, _msg("m1@x"), {"titulo": "Convite", "link": "https://a.b"}))
        pend.append({"id": "e1", "status": "aguardando", "titulo": "Convite", "email": {"message_id": "m0"},
                     "evento": {"link": ""}})
        self.assertEqual(core._ja_na_fila(pend, _msg("m1@x"), {"titulo": "Convite", "link": ""})["id"], "e1")


# ── 3. ciclo só marca como visto o que foi tratado ─────────────────────────

class TestCicloSeen(Base):
    CFG = {"perfil": "teste", "remetentes_noticia": ["news.com"], "jornal": {"min_noticias": 2},
           "pendencias": {"lembrar_a_cada_horas": 0}}

    def _ciclo(self, msgs, **kw):
        with mock.patch.object(core, "carregar_cfg", return_value=("teste", self.CFG)), \
                mock.patch.object(core, "coletar", return_value=msgs), \
                mock.patch.object(core, "ollama_generate_json", return_value=None), \
                mock.patch.object(core, "ollama_generate", return_value=None), \
                mock.patch.object(core, "tts_save", return_value=False):
            return core.ciclo(notificar=False, **kw)

    def _ics(self):
        return [{"summary": "DevFest Bauru", "dtstart": "2026-11-21T10:00-03:00", "dtend": None, "location": "",
                 "url": "", "organizer": "", "method": "REQUEST", "status": ""}]

    def test_classificacao_falhou_nao_marca(self):
        r = self._ciclo([_msg("conv@sbc", "Convite: ERAD 12/12/2026")])
        self.assertTrue(r["classificacao"][0].get("falhou"))
        self.assertNotIn("conv@sbc", core.load_seen())
        self.assertNotIn("conv@sbc", limpeza.carregar_registro())

    def test_so_jornal_nao_marca_convite_e_extracao_falha_nao_marca_newsletter(self):
        msgs = [_msg("devfest@gdg", "Convite: DevFest", ics=self._ics()),
                _msg("infoq@news.com", "InfoQ semanal", frm="x@news.com")]
        r = self._ciclo(msgs, so_jornal=True)
        self.assertEqual(r["contagem"], {"evento": 1, "noticia": 1})
        seen = core.load_seen()
        self.assertNotIn("devfest@gdg", seen)
        self.assertNotIn("infoq@news.com", seen)       # extração falhou (LLM fora)
        self.assertIn("devfest@gdg", limpeza.carregar_registro())   # categoria certa continua registrada

    def test_so_pendencias_nao_marca_newsletter(self):
        msgs = [_msg("devfest@gdg", "Convite: DevFest", ics=self._ics()),
                _msg("infoq@news.com", "InfoQ semanal", frm="x@news.com")]
        r = self._ciclo(msgs, so_pendencias=True)
        self.assertEqual(len(r["pendencias_novas"]), 1)
        seen = core.load_seen()
        self.assertIn("devfest@gdg", seen)
        self.assertNotIn("infoq@news.com", seen)


# ── 4. YAML ilegível: cópia do original antes de recomeçar ─────────────────

class TestYamlCorrompido(Base):
    def test_pendencias(self):
        f = core.pend_file()
        ruim = "pendencias:\n- id: a\n  titulo: [quebrado\n"
        f.write_text(ruim, encoding="utf-8")
        self.assertEqual(core._load_pend(), {"pendencias": []})
        core._load_pend()   # de novo: não duplica a cópia
        copias = list(f.parent.glob("pendencias.yml.corrompido-*"))
        self.assertEqual(len(copias), 1)
        self.assertEqual(copias[0].read_text(encoding="utf-8"), ruim)

    def test_tasks(self):
        f = core.vault() / "tasks.yml"
        ruim = "tasks:\n  - id: 1\n    title: \"sem fechar\n"
        f.write_text(ruim, encoding="utf-8")
        tid = core._add_task("Nova", None, [])
        # continua depois do maior id do arquivo ilegível: pendências antigas guardam task_id 1
        self.assertEqual(tid, 2)
        copias = list(f.parent.glob("tasks.yml.corrompido-*"))
        self.assertEqual([c.read_text(encoding="utf-8") for c in copias], [ruim])
        self.assertEqual(len(yaml.safe_load(f.read_text(encoding="utf-8"))["tasks"]), 1)

    def test_sem_copia_nao_grava(self):
        f = core.pend_file()
        f.write_text("pendencias: [", encoding="utf-8")
        with mock.patch.object(core.shutil, "copy2", side_effect=OSError("disco cheio")):
            with self.assertRaises(RuntimeError):
                core._load_pend()
        self.assertEqual(f.read_text(encoding="utf-8"), "pendencias: [")


# ── 5. responder de novo / trocar a resposta ───────────────────────────────

class TestReresponder(Base):
    PID = "20261026-workshop-k8s"

    def setUp(self):
        super().setUp()
        amanha = (core._now() + timedelta(days=2)).strftime("%Y-%m-%dT19:00")
        (core.pend_dir() / f"{self.PID}.md").write_text(f"---\nid: {self.PID}\nstatus: aguardando\n---\n# W\n",
                                                        encoding="utf-8")
        core._save_pend({"pendencias": [{
            "id": self.PID, "tipo": "evento", "status": "aguardando", "titulo": "Workshop K8s",
            "evento": {"data_inicio": amanha, "data_fim": None, "local": "Fatec", "online": False,
                       "link": "https://forms.gle/x", "resumo": "workshop"},
            "email": {"subject": "Convite", "from": "a@b.com"},
            "arquivos": {"md": f"pendencias/{self.PID}.md", "mp3": None}}]})

    def _tasks(self):
        return yaml.safe_load((core.vault() / "tasks.yml").read_text(encoding="utf-8"))["tasks"]

    def test_vou_vou_nao_vou(self):
        e1 = core.responder(self.PID, "vou")
        self.assertTrue(e1["_task_criada"])
        e2 = core.responder(self.PID, "vou")
        self.assertFalse(e2["_task_criada"])
        self.assertEqual(len(self._tasks()), 1)
        itens = [i for i in agenda.listar() if (i.get("origem") or {}).get("pendencia_id") == self.PID]
        self.assertEqual(len(itens), 1)

        e3 = core.responder(self.PID, "nao_vou")
        self.assertFalse(e3["_task_criada"])
        self.assertEqual(e3["status"], "nao_vou")
        t = self._tasks()[0]
        self.assertEqual(t["status"], "cancelled")
        self.assertIsNotNone(t["completed_at"])
        self.assertEqual(agenda.obter(itens[0]["id"])["status"], "cancelado")
        md = (core.pend_dir() / f"{self.PID}.md").read_text(encoding="utf-8")
        self.assertIn("\nstatus: nao_vou\n", md)
        self.assertEqual(md.count("## Resposta"), 2)
        self.assertEqual(core.obter_pendencia(self.PID)["status"], "nao_vou")
        self.assertNotIn("_task_criada", core.obter_pendencia(self.PID))


# ── 6. agenda: desfazer idempotente e "criar ok" depois do desfazer ────────

class TestAgendaDesfazer(Base):
    def _item(self):
        ini = (core._now() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        return agenda.adicionar("Python Bauru", ini, notificar=False)["id"]

    def test_desfazer_duas_vezes(self):
        aid = self._item()
        agenda.registrar_resultado(aid, "criar", True)
        self.assertEqual(agenda.desfazer(aid)["status"], "desfazer")
        self.assertEqual(agenda.desfazer(aid)["status"], "desfazer")
        self.assertEqual([i["acao"] for i in agenda.fila_extensao()], ["remover"])

    def test_criar_ok_depois_de_desfazer(self):
        aid = self._item()
        self.assertEqual(agenda.fila_extensao()[0]["acao"], "criar")   # em_execucao
        self.assertEqual(agenda.desfazer(aid)["status"], "desfazer")
        self.assertEqual(agenda.registrar_resultado(aid, "criar", True)["status"], "desfazer")
        self.assertEqual([i["acao"] for i in agenda.fila_extensao()], ["remover"])

    def test_criar_falhou_depois_de_cancelar(self):
        aid = self._item()
        self.assertEqual(agenda.desfazer(aid)["status"], "cancelado")
        self.assertEqual(agenda.registrar_resultado(aid, "criar", False, erro="x")["status"], "cancelado")
        self.assertEqual(agenda.fila_extensao(), [])


# ── 7. limpeza lê os manifestos onde o podcast grava ───────────────────────

class TestManifestosDir(Base):
    def test_vault_noticias_vazio(self):
        self.assertEqual(limpeza.manifestos_dir(), core.vault_noticias() / "podcast" / "manifestos")
        self.assertEqual(limpeza.manifestos_dir(), semanal.podcast_dir("manifestos"))


# ── 8. URLs do text/plain viram links ──────────────────────────────────────

class TestLinksTextoPuro(unittest.TestCase):
    def test_parse_message(self):
        m = _parse("From: Fatec <eventos@fatec.sp.gov.br>\nTo: p@x.com\nSubject: Workshop de Kubernetes\n"
                   "Message-ID: <w1@fatec>\nContent-Type: text/plain; charset=utf-8\n\n"
                   "Workshop de Kubernetes na prática.\nInscreva-se: https://forms.gle/k8sWorkshopFatec2026.\n"
                   "Leia mais\nhttps://github.blog/news-insights/copilot-free-ends/?utm_source=tldr\n")
        self.assertEqual(m["links"], [("Inscreva-se", "https://forms.gle/k8sWorkshopFatec2026"),
                                      ("Leia mais", "https://github.blog/news-insights/copilot-free-ends/")])
        _txt, permitidas = core._links_texto(m)
        self.assertIn("https://forms.gle/k8sWorkshopFatec2026", permitidas)
        self.assertEqual(core._link_de_acao(m, permitidas), "https://forms.gle/k8sWorkshopFatec2026")


# ── 9. .ics de cancelamento / resposta não vira "vou ou não vou" ───────────

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//teste//EN
{metodo}BEGIN:VEVENT
UID:abc@x
DTSTART:20261110T130000Z
DTEND:20261110T140000Z
SUMMARY:Reunião
{status}END:VEVENT
END:VCALENDAR
"""


class TestIcsMetodo(unittest.TestCase):
    def _m(self, metodo="", status="", ct_method=""):
        ics = ICS.format(metodo=f"METHOD:{metodo}\n" if metodo else "", status=f"STATUS:{status}\n" if status else "")
        ct = "text/calendar; charset=utf-8" + (f"; method={ct_method}" if ct_method else "")
        return _parse("From: chefe@empresa.com\nTo: p@x.com\nSubject: Reunião\nMessage-ID: <r1@x>\n"
                      "MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=XX\n\n--XX\n"
                      "Content-Type: text/plain\n\nveja\n--XX\n"
                      f"Content-Type: {ct}\n\n{ics}\n--XX--\n")

    def test_metodos(self):
        cases = [(("REQUEST", "", ""), "evento"), (("", "", ""), "evento"), (("PUBLISH", "", ""), "evento"),
                 (("CANCEL", "CANCELLED", ""), "outro"), (("REQUEST", "CANCELLED", ""), "outro"),
                 (("REPLY", "", ""), "outro"), (("", "", "CANCEL"), "outro")]
        for args, esperado in cases:
            m = self._m(*args)
            self.assertEqual(len(m["ics"]), 1, args)
            self.assertEqual(core.classificar(m, {})["categoria"], esperado, args)

    def test_fallback_regex(self):
        raw = ICS.format(metodo="METHOD:CANCEL\n", status="STATUS:CANCELLED\n")
        real_import = __import__

        def sem_icalendar(name, *a, **k):
            if name == "icalendar":
                raise ImportError
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", sem_icalendar):
            ev = lib_mail.parse_ics(raw)[0]
        self.assertEqual((ev["method"], ev["status"]), ("CANCEL", "CANCELLED"))


# ── 10. evento só com data expira depois do fim do dia ─────────────────────

class TestExpirar(Base):
    def test_dia_inteiro(self):
        hoje, ontem = core._now().date(), core._now().date() - timedelta(days=1)
        core._save_pend({"pendencias": [
            {"id": "t-hoje", "status": "aguardando", "titulo": "a", "evento": {"data_inicio": hoje.isoformat()}},
            {"id": "t-ontem", "status": "aguardando", "titulo": "b", "evento": {"data_inicio": ontem.isoformat()}},
            {"id": "t-ontem-ate-hoje", "status": "aguardando", "titulo": "c",
             "evento": {"data_inicio": ontem.isoformat(), "data_fim": hoje.isoformat()}},
            {"id": "t-wa", "status": "aguardando", "titulo": "d", "evento": None},
        ]})
        r = core.expirar_e_lembrar({"pendencias": {"lembrar_a_cada_horas": 0}})
        self.assertEqual(r["expiradas"], ["t-ontem"])

    def test_com_hora(self):
        agora = core._now()
        ev = {"data_inicio": (agora - timedelta(hours=3)).isoformat(timespec="minutes"),
              "data_fim": (agora + timedelta(hours=1)).isoformat(timespec="minutes")}
        self.assertGreater(core._expira_em(ev), agora)              # ainda em andamento
        ev["data_fim"] = None
        self.assertLess(core._expira_em(ev), agora)                 # sem fim: início + 2 h


# ── 11/12. jornal: URL solta inventada sai; título com aspas não quebra o YAML ─

class TestJornal(Base):
    def test_url_solta(self):
        ok = {"https://github.blog/x"}
        txt = "## Leitura recomendada\nhttps://tldr.com/rust-kernel-drivers\nVeja https://github.blog/x.\n"
        out = core._tirar_urls_soltas(txt, ok)
        self.assertNotIn("tldr.com", out)
        self.assertIn("Veja https://github.blog/x.", out)
        self.assertEqual(out.splitlines()[:2], ["## Leitura recomendada", "Veja https://github.blog/x."])

    def test_gerar_jornal_titulo_aspas_e_url(self):
        itens = [{"title": f"Notícia {i}", "url": f"https://n.com/{i}", "snippet": "s", "source": "TLDR",
                  "published": "", "email_id": "m1"} for i in range(3)]
        emails = [_msg("m1", "TLDR", frm="dan@tldr.tech")]
        titulo = 'Google lança "Gemini 4" e OpenAI responde'
        with mock.patch.object(core, "cluster_items", side_effect=lambda x: x), \
                mock.patch.object(core, "rank_clusters", return_value=[0, 1, 2]), \
                mock.patch.object(core, "build_clustered_news_text", return_value=("d", "")), \
                mock.patch.object(core, "ollama_generate",
                                  return_value="## Destaques\nTexto https://n.com/1\nhttps://inventada.com/x\n"), \
                mock.patch.object(core, "generate_title_via_llm", return_value=titulo):
            r = core.gerar_jornal(itens, emails, {"jornal": {"formato": ["markdown"]}}, "teste")
        md = Path(r["md"]).read_text(encoding="utf-8")
        fm = yaml.safe_load(md.split("---")[1])
        self.assertEqual(fm["title"], titulo)
        self.assertIn("Texto https://n.com/1", md)
        self.assertNotIn("inventada.com", md)

    def test_semanal_titulo_aspas(self):
        titulo = 'Semana do "Gemini 4"'
        itens = [{"title": f"N{i}", "url": "", "snippet": "", "source": "A", "published": "", "email_id": None}
                 for i in range(3)]
        temas = [{"tema": "IA", "score": 1.0, "dias": ["2026-10-26"], "fontes": ["A"], "itens": itens}]
        with mock.patch.object(core, "carregar_cfg", return_value=("teste", {"podcast": {"min_noticias": 1}})), \
                mock.patch.object(semanal, "edicoes_da_semana",
                                  return_value=[{"arquivo": Path("x.md"), "noticias": itens}]), \
                mock.patch.object(semanal, "selecionar_temas", return_value=temas), \
                mock.patch.object(semanal, "roteiro_moldura", return_value=([("ALFRED", "oi")], [])), \
                mock.patch.object(semanal, "roteiro_segmento", return_value=[]), \
                mock.patch.object(semanal, "generate_title_via_llm", return_value=titulo), \
                mock.patch.object(semanal, "resolver_emails", return_value=([], [])):
            r = semanal.gerar_semanal(com_audio=False, dias=3, hoje=date(2026, 10, 26))
        md = Path(r["md"]).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(md.split("---")[1])["title"], titulo)


# ── 13/14. semana/dias inválidos, mensagens de erro, resumo do lote ────────

class TestErrosAPI(Base):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        import main
        main.TOKEN = "token-de-teste"
        self.cli = TestClient(main.app)
        self.h = {"Authorization": "Bearer token-de-teste"}

    def test_semana_e_dias_invalidos(self):
        with self.assertRaises(ValueError):
            semanal.intervalo_semana("2026-W60")
        with self.assertRaises(ValueError):
            semanal.gerar_semanal(dias=0)
        with mock.patch.object(core, "carregar_cfg", return_value=("teste", {})):
            for corpo in ({"dry_run": True, "semana": "lixo"}, {"dry_run": True, "dias": -2},
                          {"dry_run": True, "dias": 0}, {"dry_run": True, "semana": "2026-W00"}):
                r = self.cli.post("/semanal", json=corpo, headers=self.h)
                self.assertEqual(r.status_code, 400, corpo)
                self.assertIn("inválid", r.json()["detail"])

    def test_keyerror_sem_aspas(self):
        r = self.cli.post("/pendencias/naoexiste/responder", json={"resposta": "vou"}, headers=self.h)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "Pendência não encontrada: naoexiste")

    def test_resumo_lote_sem_e_mais(self):
        lote = {"total": 5, "itens": [{"motivo": "promoção", "date": "2026-10-26", "from": "a@b",
                                       "subject": "x"}] * 5}
        self.assertNotIn("e mais", limpeza.resumo_lote(lote, 0))
        self.assertIn("e mais 3", limpeza.resumo_lote(lote, 2))


if __name__ == "__main__":
    unittest.main()
