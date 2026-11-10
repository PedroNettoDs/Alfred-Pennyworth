"""
Alfred — transcrição de áudio 100% local.

    from _shared.lib_audio import transcrever_audio, para_wav, duracao_segundos

Ordem de tentativa:
  1. Ollama com modelo de entrada de áudio (gemma4). O áudio vai em base64 no campo
     `images` do /api/generate — o Ollama usa o mesmo campo para imagem e áudio.
     Só aceita WAV/MP3 (OGG/Opus do WhatsApp falha com "Failed to load image or audio
     file"), por isso tudo passa antes pelo ffmpeg → WAV mono 16 kHz.
     O gemma4 ouve ~30 s por chamada → áudios longos são fatiados em segmentos.
  2. faster-whisper, se o pacote estiver instalado (pip install faster-whisper).
  3. "" — o chamador decide o que fazer sem transcrição.

Com motor="whisper" a ordem inverte: o faster-whisper vai primeiro, em CPU, fora da fila do Ollama (é o que o
microfone do Painel usa); o gemma4 fica de reserva para quando o pacote não está instalado.

Nenhuma função levanta exceção: erros são logados e voltam no dict de resultado.
"""

import base64
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Union

from _shared.lib_alfred import log, ollama_generate

SEGMENTO_S = 30            # gemma4 só processa ~30 s de áudio por chamada
MAX_SEGUNDOS_DEFAULT = 600 # 10 min — acima disso o áudio é cortado (e avisado)
ESPERA_S = 240             # por chamada ao Ollama
ESPERA_INTERATIVA_S = 30   # com tokens_por_segundo (alguém esperando na tela): ver transcrever_audio

PROMPT_TRANSCRICAO = (
    "Transcreva o áudio em português brasileiro, literalmente, palavra por palavra. "
    "Responda SOMENTE com a transcrição: sem introdução, sem comentários, sem aspas, "
    "sem marcações. Se não houver fala alguma, responda exatamente: [sem fala]"
)

_EXT_POR_MIME = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/amr": ".amr", "video/mp4": ".mp4",
}


def ffmpeg_disponivel() -> bool:
    return bool(shutil.which("ffmpeg"))


def modelo_transcricao() -> str:
    return os.getenv("MODEL_TRANSCRIBE", "").strip() or os.getenv("MODEL_CHAT", "").strip() or "gemma4:12b"


def extensao_por_mime(mimetype: str, padrao: str = ".ogg") -> str:
    base = (mimetype or "").split(";")[0].strip().lower()
    return _EXT_POR_MIME.get(base, padrao)


def duracao_segundos(path: Path) -> Optional[float]:
    """Duração via ffprobe (None se indisponível)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def para_wav(src: Path, dst: Path, sample_rate: int = 16000, max_segundos: Optional[float] = None,
             silencio_final: float = 0.0) -> bool:
    """Converte qualquer áudio para WAV mono PCM 16-bit (o formato que o gemma4 espera).
    silencio_final: segundos de silêncio acrescentados no fim (ver transcrever_audio)."""
    if not ffmpeg_disponivel():
        log("[audio] ffmpeg não encontrado — não dá pra converter o áudio")
        return False
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if max_segundos:
        cmd += ["-t", str(int(max_segundos))]
    if silencio_final:
        cmd += ["-af", f"apad=pad_dur={silencio_final}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log(f"[audio] ffmpeg falhou: {(r.stderr or '').strip()[:200]}")
            return False
        return Path(dst).exists() and Path(dst).stat().st_size > 44
    except Exception as e:
        log(f"[audio] ffmpeg erro: {e}")
        return False


def _segmentar(wav: Path, pasta: Path, segundos: int = SEGMENTO_S) -> list[Path]:
    """Fatia o WAV em pedaços de N segundos (gemma4 ignora o que passa de ~30 s)."""
    dur = duracao_segundos(wav) or 0
    if dur <= segundos + 1:
        return [wav]
    padrao = pasta / "seg_%03d.wav"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-f", "segment",
             "-segment_time", str(segundos), "-c", "copy", str(padrao)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            log(f"[audio] segmentação falhou: {(r.stderr or '')[:200]} — usando o áudio inteiro")
            return [wav]
    except Exception as e:
        log(f"[audio] segmentação erro: {e}")
        return [wav]
    partes = sorted(pasta.glob("seg_*.wav"))
    return partes or [wav]


def _transcrever_ollama(wav: Path, model: Optional[str] = None, num_predict: int = 700,
                        num_ctx: Optional[int] = None, espera: int = ESPERA_S) -> str:
    b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
    opcoes = {"temperature": 0.0, "num_predict": num_predict}
    if num_ctx:
        opcoes["num_ctx"] = num_ctx
    out = ollama_generate(
        PROMPT_TRANSCRICAO, model=model or modelo_transcricao(), timeout=espera,
        images=[b64], think=False, options=opcoes,
    )
    out = out.strip().strip('"').strip()
    if not out or out.lower().startswith("[sem fala]"):
        return ""
    return out


_WHISPER = None                    # o modelo, carregado uma vez por processo (~1 s; recarregar a cada áudio custava isso)
_WHISPER_LOCK = threading.Lock()   # uma transcrição por vez: duas juntas só dividiriam os mesmos núcleos


def _modelo_whisper():
    """O faster-whisper carregado (None se o pacote não está instalado). WHISPER_MODEL (padrão small) e
    WHISPER_DEVICE (padrão cpu: na placa ele voltaria para a fila que o motor separado existe para evitar)."""
    global _WHISPER
    if _WHISPER is None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError:
            return None
        nome = os.getenv("WHISPER_MODEL", "").strip() or "small"
        dispositivo = os.getenv("WHISPER_DEVICE", "").strip() or "cpu"
        opcoes = {"device": dispositivo, "compute_type": "int8" if dispositivo == "cpu" else "float16",
                  "cpu_threads": max(1, (os.cpu_count() or 2) // 2)}
        try:
            _WHISPER = WhisperModel(nome, local_files_only=True, **opcoes)    # sem consultar a rede a cada carga
        except Exception:
            log(f"[audio] baixando o modelo Whisper '{nome}' (só na primeira vez)")
            _WHISPER = WhisperModel(nome, **opcoes)
    return _WHISPER


def _transcrever_whisper(wav: Path, dica: str = "") -> Optional[str]:
    """Texto do WAV (mono 16 kHz, o que o para_wav gera); "" se não há fala; None se o Whisper não está
    instalado ou falhou. `dica`: nomes e termos que ele deve reconhecer ("Alfred, Fatec")."""
    try:
        with _WHISPER_LOCK:
            modelo = _modelo_whisper()
            if modelo is None:
                return None
            import numpy as np    # vem com o faster-whisper; aqui dentro para o lib_audio importar sem ele
            with wave.open(str(wav), "rb") as w:      # lido direto: dispensa o decodificador do faster-whisper
                audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
            segs, _info = modelo.transcribe(audio, language="pt", beam_size=1, vad_filter=True,
                                            condition_on_previous_text=False, initial_prompt=dica or None)
            return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:
        log(f"[audio] faster-whisper falhou: {e}")
        return None


def transcrever_audio(
    fonte: Union[Path, str, bytes],
    mimetype: str = "",
    max_segundos: int = MAX_SEGUNDOS_DEFAULT,
    model: Optional[str] = None,
    tokens_por_segundo: Optional[float] = None,
    num_ctx: Optional[int] = None,
    silencio_final: float = 0.0,
    motor: str = "ollama",
    dica: str = "",
) -> dict:
    """
    Transcreve um áudio (caminho, bytes ou string base64).

    Retorna {"texto": str, "metodo": "ollama" | "whisper" | "", "duracao_s": float|None,
             "segmentos": int, "cortado": bool, "erro": str|None, "ocupado": bool}.

    tokens_por_segundo: para quem espera a resposta na hora (o microfone do Painel). Às vezes o gemma4 dispara:
    gera tokens em branco até o limite (700 tokens, ~10 s) e devolve vazio; chamado de novo com o mesmo áudio,
    acerta (medido em 10/11/2026). Com este parâmetro o limite de cada segmento fica proporcional à duração
    (40 + tokens_por_segundo × segundos) e um segmento vazio é tentado mais uma vez. None mantém o de sempre.
    Também troca a espera pelo Ollama de 240 s para ESPERA_INTERATIVA_S: a placa tem uma fila só, e com outro
    modelo em uso (a síntese de uma pesquisa leva minutos) a transcrição ficava parada esse tempo todo. Passou
    do prazo → "ocupado": True, sem segunda tentativa.

    num_ctx: o contexto pedido ao Ollama (None = OLLAMA_NUM_CTX). O Ollama recarrega o modelo quando o num_ctx muda
    (10 a 25 s medidos): quem transcreve no meio de uma conversa passa o mesmo num_ctx do chat.

    silencio_final: segundos de silêncio no fim do áudio. Um áudio cortado no meio da fala (a prévia de uma
    gravação em andamento) faz o gemma4 responder vazio ou "[sem fala]": 6 falhas em 16 cortes medidos; com
    0,8 s de silêncio no fim, 16 acertos em 16.

    motor: "whisper" tenta primeiro o faster-whisper (CPU, fora da fila do Ollama); só se ele não estiver
    instalado ou falhar é que o gemma4 entra. Medido em 10/11/2026 com 8 frases (83 s): small em CPU, ~2 s por
    frase e "Alfred" certo em todas; gemma4, 0,7 a 2,5 s, mas "Aufridge" em 4 de 6 e vazio na frase técnica.
    dica: nomes e termos para o Whisper reconhecer.
    """
    res = {"texto": "", "metodo": "", "duracao_s": None, "segmentos": 0, "cortado": False, "erro": None,
           "ocupado": False}
    with tempfile.TemporaryDirectory(prefix="alfred-audio-") as tmp:
        pasta = Path(tmp)
        # 1. materializa a fonte num arquivo
        if isinstance(fonte, bytes):
            src = pasta / f"entrada{extensao_por_mime(mimetype)}"
            src.write_bytes(fonte)
        elif isinstance(fonte, (str, Path)) and Path(str(fonte)).is_file():
            src = Path(str(fonte))
        elif isinstance(fonte, str) and len(fonte) > 100:
            try:
                raw = fonte.split(",", 1)[1] if fonte.startswith("data:") else fonte
                src = pasta / f"entrada{extensao_por_mime(mimetype)}"
                src.write_bytes(base64.b64decode(raw))
            except Exception as e:
                res["erro"] = f"base64 inválido: {e}"
                return res
        else:
            res["erro"] = "fonte de áudio vazia ou inexistente"
            return res

        dur = duracao_segundos(src)
        res["duracao_s"] = round(dur, 1) if dur else None
        if dur and max_segundos and dur > max_segundos:
            res["cortado"] = True
            log(f"[audio] áudio de {dur:.0f}s — transcrevendo só os primeiros {max_segundos}s")

        # 2. WAV mono 16 kHz
        wav = pasta / "entrada.wav"
        if not para_wav(src, wav, max_segundos=max_segundos if res["cortado"] else None,
                        silencio_final=silencio_final):
            res["erro"] = "não foi possível converter o áudio (ffmpeg)"
            return res

        # 3. Whisper primeiro, quando pedido: se ele respondeu (mesmo "sem fala"), o gemma4 não é consultado
        if motor == "whisper":
            t = _transcrever_whisper(wav, dica)
            if t is not None:
                res["texto"], res["metodo"], res["segmentos"] = t, "whisper", 1
                return res

        # 4. gemma4 via Ollama, por segmento
        partes = _segmentar(wav, pasta)
        res["segmentos"] = len(partes)
        textos = []
        for i, parte in enumerate(partes, 1):
            limite = 700
            if tokens_por_segundo:
                limite = 40 + int(tokens_por_segundo * (duracao_segundos(parte) or SEGMENTO_S))
            espera = ESPERA_INTERATIVA_S if tokens_por_segundo else ESPERA_S
            inicio = time.monotonic()
            t = _transcrever_ollama(parte, model, limite, num_ctx, espera)
            if not t and tokens_por_segundo:
                if time.monotonic() - inicio >= espera - 1:      # nem respondeu: a placa está com outra tarefa
                    res["ocupado"] = True
                    res["erro"] = f"o Ollama não respondeu em {espera} s (ocupado com outro modelo)"
                    log(f"[audio] {res['erro']}")
                    return res
                t = _transcrever_ollama(parte, model, limite, num_ctx, espera)
            if t:
                textos.append(t)
            elif i == 1:
                break  # primeiro segmento falhou → modelo sem áudio? vai pro fallback
        if textos:
            res["texto"] = " ".join(textos).strip()
            res["metodo"] = "ollama"
            return res

        # 5. fallback faster-whisper (já tentado acima quando motor="whisper")
        t = None if motor == "whisper" else _transcrever_whisper(wav, dica)
        if t:
            res["texto"] = t
            res["metodo"] = "whisper"
            return res

        res["erro"] = ("nenhum transcritor respondeu — confira se MODEL_TRANSCRIBE/MODEL_CHAT tem capacidade "
                       "'audio' no Ollama (gemma4) ou instale faster-whisper")
        log(f"[audio] {res['erro']}")
        return res


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 lib_audio.py <arquivo de áudio>")
        sys.exit(1)
    import json
    print(json.dumps(transcrever_audio(Path(sys.argv[1])), ensure_ascii=False, indent=2))
