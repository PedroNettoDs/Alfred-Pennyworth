"""
Testes offline do podcast semanal (sem Ollama, edge-tts nem Thunderbird).

    venv/bin/python -m unittest tests.test_semanal -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ.setdefault("VAULT_ALFRED", tempfile.mkdtemp(prefix="alfred_vault_"))

import semanal  # noqa: E402

EDICAO_MD = """---
title: "Teste"
date: 2026-10-19
type: jornal
---

# Teste

## Destaques
1. **[Cisco U. | IA na rede](https://x.example/a)**
A Cisco lançou trilhas de IA.
*(Learn with Cisco)*

---

## Newsletters desta edição
- **Learn with Cisco** — New learning opportunities! (2026-10-03T13:32)
- **AWS Builder Center** — Travado em algo? (2026-10-03T08:58)

## Notícias extraídas

1. [Cisco U. | IA na rede](https://x.example/a) — *Learn with Cisco*
2. Python Basics for Automation — *Learn with Cisco*
"""


class TestSemana(unittest.TestCase):
    def test_intervalo(self):
        self.assertEqual(semanal.intervalo_semana("2026-W43"), (date(2026, 10, 19), date(2026, 10, 25)))
        self.assertEqual(semanal.semana_atual(date(2026, 10, 25)), "2026-W43")
        self.assertEqual(semanal.semana_atual(date(2026, 10, 19)), "2026-W42")  # segunda → semana anterior
        with self.assertRaises(ValueError):
            semanal.intervalo_semana("semana 43")


class TestDialogo(unittest.TestCase):
    def test_parse(self):
        txt = "Claro, aqui está:\n**ALFRED:** Bom dia.\nLÚCIA: Olá, veja https://a.b/c\ncontinua aqui\n(risos)\n"
        falas = semanal.parse_dialogo(txt)
        self.assertEqual(falas[0], ("ALFRED", "Bom dia."))
        self.assertEqual(falas[1][0], "LUCIA")
        self.assertIn("notas do episódio", falas[1][1])
        self.assertTrue(falas[1][1].endswith("continua aqui"))
        self.assertEqual(len(falas), 2)


class TestEdicao(unittest.TestCase):
    def test_ler_md_antigo(self):
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "2026-10-19_teste.md"
            md.write_text(EDICAO_MD, encoding="utf-8")
            ed = semanal.ler_edicao(md)
        self.assertEqual(ed["data"], "2026-10-19")
        self.assertEqual(len(ed["emails"]), 2)
        self.assertIsNone(ed["emails"][0]["message_id"])
        self.assertEqual(ed["noticias"][0]["url"], "https://x.example/a")
        self.assertEqual(ed["noticias"][0]["snippet"], "A Cisco lançou trilhas de IA.")
        self.assertEqual(ed["noticias"][1]["source"], "Learn with Cisco")

    def test_resolver_emails_com_json(self):
        tema = {"itens": [{"email_id": "m1@x", "_dia": "2026-10-19", "source": "A"}]}
        ed = {"data": "2026-10-19", "emails": [
            {"message_id": "m1@x", "from": "A <a@x>", "subject": "S", "date": "2026-10-19T08:00",
             "pasta": "INBOX", "conta": "c"},
            {"message_id": "m2@x", "from": "B <b@x>", "subject": "T", "date": "2026-10-19T09:00",
             "pasta": "INBOX", "conta": "c"}]}
        emails, nao = semanal.resolver_emails([tema], [ed], {"backend": "imap"})
        self.assertEqual([e["message_id"] for e in emails], ["m1@x"])
        self.assertEqual(set(emails[0]), {"message_id", "from", "subject", "date", "pasta", "conta"})
        self.assertEqual(nao, [])


if __name__ == "__main__":
    unittest.main()


BRIEFING_MD = """---
title: "Briefing"
date: 2026-10-20
type: briefing
---

# Briefing

## Destaques
**[Rust 2.0 anunciado](https://rust.example/2)**
A equipe do Rust anunciou a versão 2.0.
*([Lobsters](https://rust.example/2))*

---

## Fontes citadas

1. [Rust 2.0 anunciado](https://rust.example/2) — *Lobsters*
2. [Kubernetes 1.35](https://k8s.example/135) — *Hacker News*
"""


class TestPeriodo(unittest.TestCase):
    """Podcast a cada N dias (20/10/2026): período, 'se devido' e briefings por RSS como fonte."""

    def setUp(self):
        import json
        import shutil
        from unittest import mock
        self.json, self.shutil = json, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred_podcast_"))
        self.env = mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.tmp / "alfred"),
                                                "VAULT_NOTICIAS": str(self.tmp / "noticias")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_periodo_atual(self):
        pid, ini, fim = semanal.periodo_atual(3, date(2026, 10, 20))
        self.assertEqual((pid, ini, fim), ("2026-10-18_2026-10-20", date(2026, 10, 18), date(2026, 10, 20)))
        self.assertEqual(semanal.intervalo_dias({"intervalo_dias": 3}), 3)
        self.assertEqual(semanal.intervalo_dias({}), 3)
        self.assertEqual(semanal.intervalo_dias({"intervalo_dias": 0}), 1)

    def _manifesto(self, gerado_em: str, nome: str = "x"):
        d = self.tmp / "noticias" / "podcast" / "manifestos"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{nome}.json").write_text(self.json.dumps({"semana": nome, "gerado_em": gerado_em, "emails": []}),
                                        encoding="utf-8")

    def test_episodio_devido(self):
        from datetime import datetime
        agora = datetime.fromisoformat("2026-10-20T19:00:00-03:00")
        self.assertTrue(semanal.episodio_devido(3, agora)[0])                 # nenhum episódio ainda
        self._manifesto("2026-10-18T19:00:05-03:00", "a")
        self.assertFalse(semanal.episodio_devido(3, agora)[0])                # há 2 dias
        self._manifesto("2026-10-17T19:00:05-03:00", "b")                     # mais antigo não importa
        self.assertFalse(semanal.episodio_devido(3, agora)[0])
        self.assertTrue(semanal.episodio_devido(3, datetime.fromisoformat("2026-10-21T19:00:00-03:00"))[0])

    def test_briefing_rss_vira_edicao(self):
        b = self.tmp / "alfred" / "briefings"
        b.mkdir(parents=True)
        (b / "2026-10-20_rust-2.md").write_text(BRIEFING_MD, encoding="utf-8")
        (b / "2026-10-10_antigo.md").write_text(BRIEFING_MD, encoding="utf-8")
        self.assertEqual(semanal.edicoes_da_semana(date(2026, 10, 18), date(2026, 10, 20)), [])
        eds = semanal.edicoes_da_semana(date(2026, 10, 18), date(2026, 10, 20), incluir_briefings=True)
        self.assertEqual(len(eds), 1)
        titulos = [n["title"] for n in eds[0]["noticias"]]
        self.assertEqual(titulos, ["Rust 2.0 anunciado", "Kubernetes 1.35"])
        self.assertEqual(eds[0]["noticias"][1]["source"], "Hacker News")
        self.assertEqual(eds[0]["emails"], [])
