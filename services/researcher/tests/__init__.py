"""A porta da GPU (lib_porta) marca numa pasta temporária e nenhum teste fala com o laya-serve de verdade:
com o checkpoint "porta" no ar, um teste poderia ficar esperando a placa."""
import os
import tempfile

os.environ.setdefault("ALFRED_PORTA_DIR", tempfile.mkdtemp(prefix="alfred-porta-testes-"))
os.environ.setdefault("LAYA_URL", "http://127.0.0.1:9")
