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
