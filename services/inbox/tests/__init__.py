"""Os testes publicam a atividade (lib_atividade) numa pasta temporária, nunca em logs/atividade: senão o que
os testes simulam (ciclos, pendências) apareceria na fila de verdade e na tela, pelo Pestinha."""
import os
import tempfile

os.environ.setdefault("ALFRED_ATIVIDADE_DIR", tempfile.mkdtemp(prefix="alfred-atividade-testes-"))

# A porta da GPU (lib_porta) também: marcas numa pasta temporária. E nenhum teste fala com o laya-serve de
# verdade: com o checkpoint "porta" no ar, um teste que simula Pedro falando faria os seguintes esperarem.
os.environ.setdefault("ALFRED_PORTA_DIR", tempfile.mkdtemp(prefix="alfred-porta-testes-"))
os.environ.setdefault("LAYA_URL", "http://127.0.0.1:9")

# O aplicativo do celular fica desligado nos testes, mesmo com PAINEL_HOSTS_APP no .env de verdade (o main.py
# carrega o .env sem sobrescrever o que já está no ambiente): senão abrir /painel/aplicativo num teste poderia
# gerar um token em logs/app_token.json e desconectar o aplicativo de Pedro. tests.test_painel_app liga por conta.
os.environ.setdefault("PAINEL_HOSTS_APP", "")

# A descarga do Ollama (lib_porta) fica desligada nos testes: senão um teste que passa pela porta tiraria da
# memória o modelo de verdade. tests.test_porta liga por conta.
os.environ.setdefault("OLLAMA_DESCARREGAR_S", "-1")
