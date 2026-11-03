"""
Testes do blog (briefings/jornal/podcast → Blogspot por e-mail, BLOG.md): fila, HTML e troca da propriedade.

    cd services/inbox && venv/bin/python -m unittest tests.test_blog -v

Offline: vault temporário; nada chega ao Thunderbird nem ao Blogger.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

os.environ.setdefault("SHELL_EXECUTOR_TOKEN", "token-de-teste")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

import blog  # noqa: E402

DESTINO = "fulano.segredo@blogger.com"

BRIEFING = """---
title: "AWS esgota capacidade: <script>alert(1)</script>"
date: 2026-11-02
time: "07:00"
type: briefing
Blogspot: none
tags: [briefing, matinal_tech]
---

# AWS esgota capacidade

> matinal_tech · 02/11/2026 às 07:00

## Manchete do dia
A **AWS** informa <b>algo</b> — ver [InfoQ](https://www.infoq.com/x).
"""

PODCAST = """---
title: "Resumo do Alfred"
semana: 2026-10-30_2026-11-01
type: podcast
gerado_em: 2026-11-01T19:01:47-03:00
Blogspot: none
tags: [podcast, resumo-do-alfred]
---

# Resumo do Alfred

![[2026-10-30_2026-11-01_resumo-do-alfred.mp3]]

## Edições do jornal usadas

- [[2026-10-31_acoes-da-vale]]
- [[outra|Outra edição]]
"""

JORNAL_POSTADO = BRIEFING.replace("Blogspot: none", "Blogspot: postado 2026-10-31").replace("2026-11-02", "2026-10-31")
SEM_PROP = BRIEFING.replace("Blogspot: none\n", "")


class Blog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-blog-test-"))
        self.vault, self.noticias = self.tmp / "alfred", self.tmp / "noticias"
        self.env = mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.vault), "VAULT_NOTICIAS": str(self.noticias),
                                                "BLOGGER_EMAIL": DESTINO})
        self.env.start()
        b, j, p = self.vault / "briefings", self.noticias / "jornal", self.noticias / "podcast" / "episodios"
        for d in (b, j, p):
            d.mkdir(parents=True)
        (b / "2026-11-02_0700_matinal_tech_aws.md").write_text(BRIEFING, encoding="utf-8")
        (b / "2026-11-02_0700_matinal_tech_aws.mp3").write_bytes(b"ID3")
        (b / "2026-10-01_antigo.md").write_text(SEM_PROP, encoding="utf-8")
        (j / "2026-10-31_acoes-da-vale.md").write_text(JORNAL_POSTADO, encoding="utf-8")
        (p / "2026-10-30_2026-11-01_resumo-do-alfred.md").write_text(PODCAST, encoding="utf-8")
        self.briefing = b / "2026-11-02_0700_matinal_tech_aws.md"

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fila_so_none_do_mais_antigo_ao_mais_novo(self):
        f = blog.fila()
        self.assertEqual(f["destino"], DESTINO)
        self.assertEqual([i["id"] for i in f["itens"]],
                         ["podcast/2026-10-30_2026-11-01_resumo-do-alfred.md", "briefings/2026-11-02_0700_matinal_tech_aws.md"])
        pod, bri = f["itens"]
        self.assertIsNone(pod["audio"])   # o .mp3 do podcast não existe neste vault
        self.assertEqual(bri["audio"]["nome"], "2026-11-02_0700_matinal_tech_aws.mp3")
        self.assertTrue(bri["resultado"].startswith("/blog/briefings/") and bri["resultado"].endswith("/resultado"))

    def test_html_escapa_e_limpa_obsidian(self):
        pod, bri = blog.fila()["itens"]
        self.assertEqual(bri["assunto"], "AWS esgota capacidade: <script>alert(1)</script>")   # assunto é texto puro
        self.assertNotIn("<b>", bri["html"])
        self.assertIn("&lt;b&gt;algo&lt;/b&gt;", bri["html"])
        self.assertIn("<strong>AWS</strong>", bri["html"])
        self.assertIn('<a href="https://www.infoq.com/x"', bri["html"])
        self.assertNotIn("AWS esgota capacidade</h", bri["html"])   # o "# título" não se repete no corpo
        self.assertTrue(bri["html"].endswith("<p>#end</p>"))
        self.assertNotIn("![[", pod["html"])
        self.assertNotIn("[[", pod["html"])
        self.assertIn("2026-10-31_acoes-da-vale", pod["html"])
        self.assertIn("Outra edição", pod["html"])

    def test_sem_blogger_email_fila_vazia_com_aviso(self):
        with mock.patch.dict(os.environ, {"BLOGGER_EMAIL": ""}):
            f = blog.fila()
        self.assertEqual((f["destino"], f["itens"]), ("", []))
        self.assertIn("BLOGGER_EMAIL", f["aviso"])

    def test_resultado_ok_vira_postado_data_e_sai_da_fila(self):
        r = blog.registrar_resultado("briefings", self.briefing.name, True)
        hoje = date.today().isoformat()
        self.assertEqual(r[blog.PROP], f"postado {hoje}")
        texto = self.briefing.read_text(encoding="utf-8")
        self.assertIn(f"\nBlogspot: postado {hoje}\n", texto)
        self.assertEqual(texto.replace(f"Blogspot: postado {hoje}", "Blogspot: none"), BRIEFING)   # resto intacto
        self.assertEqual(len(blog.fila()["itens"]), 1)
        # POST repetido (a extensão reenviou a confirmação) não muda nada
        self.assertFalse(blog.registrar_resultado("briefings", self.briefing.name, False, "x")["alterado"])

    def test_resultado_erro_fica_registrado_com_yaml_valido(self):
        r = blog.registrar_resultado("briefings", self.briefing.name, False, 'SMTP: recusou "anexo" #grande')
        self.assertEqual(r[blog.PROP], f'erro {date.today().isoformat()}: SMTP: recusou "anexo" #grande')
        self.assertEqual(blog.fila()["itens"][-1]["id"].split("/")[0], "podcast")   # saiu da fila

    def test_caminho_invalido_ou_sem_propriedade(self):
        for pasta, arq in (("briefings", "../alfred/x.md"), ("outra", self.briefing.name),
                           ("briefings", "nao-existe.md"), ("briefings", "x.txt"), ("briefings", "2026-10-01_antigo.md")):
            with self.assertRaises(KeyError, msg=(pasta, arq)):
                blog.registrar_resultado(pasta, arq, True)
        self.assertEqual(blog.audio("briefings", self.briefing.name).suffix, ".mp3")
        with self.assertRaises(KeyError):
            blog.audio("jornal", "2026-10-31_acoes-da-vale.md")

    def test_rotas_da_api(self):
        from fastapi.testclient import TestClient
        import main
        cli = TestClient(main.app)
        auth = {"Authorization": f"Bearer {main.TOKEN}"}
        self.assertEqual(cli.get("/blog/fila").status_code, 401)
        fila = cli.get("/blog/fila", headers=auth).json()
        bri = fila["itens"][-1]
        self.assertEqual(cli.get(bri["audio"]["rota"], headers=auth).content, b"ID3")
        r = cli.post(bri["resultado"], json={"ok": True}, headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["alterado"])
        self.assertEqual(cli.post("/blog/briefings/nao-existe.md/resultado", json={"ok": True}, headers=auth).status_code, 404)


if __name__ == "__main__":
    unittest.main()
