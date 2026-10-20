#!/usr/bin/env python3
"""
Gera os payloads de exemplo da Evolution API (v2, evento messages.upsert) em tests/payloads/.

    venv/bin/python tests/gerar_payloads.py            # reescreve os JSON (usa o .ogg já gravado)
    venv/bin/python tests/gerar_payloads.py --audio    # regrava o .ogg com edge-tts antes (precisa de rede + ffmpeg)

O áudio é um recado curto ("Oi Pedro, aqui é o Carlos da Fatec...") sintetizado com edge-tts e
convertido para OGG/Opus — o mesmo formato que o WhatsApp entrega (audio/ogg; codecs=opus, ptt=true).
"""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).parent
FIX = AQUI / "fixtures"
OUT = AQUI / "payloads"
OGG = FIX / "audio_carlos_fatec.ogg"
TEXTO_AUDIO = "Oi Pedro, aqui é o Carlos da Fatec. Você consegue me mandar o orçamento do servidor até sexta-feira?"

CARLOS = "5514988887777"          # contato (profissional)
AMIGA  = "5514977776666"          # contato (pessoal)
PEDRO  = "5514999990000"          # WHATSAPP_NUMERO_PEDRO nos testes
ALFRED = "5514900000000"          # número conectado na instância (instância separada)
GRUPO  = "120363012345678901@g.us"


def ts(hora: str) -> int:
    """Epoch (segundos) de 2026-10-19 às HH:MM no fuso local — a Evolution manda messageTimestamp assim."""
    return int(datetime.strptime(f"2026-10-19 {hora}", "%Y-%m-%d %H:%M").astimezone().timestamp())


def envelope(data: dict, sender: str = ALFRED) -> dict:
    return {
        "event": "messages.upsert",
        "instance": "alfred",
        "data": data,
        "destination": "http://172.17.0.1:7072/whatsapp/webhook",
        "date_time": "2026-10-19T09:30:00.000Z",
        "sender": f"{sender}@s.whatsapp.net",
        "server_url": "http://localhost:8080",
        "apikey": "CHAVE-DE-TESTE",
    }


def bytes_json(n: int, semente: int = 7) -> dict:
    """Uint8Array serializado como a Evolution manda no webhook: {"0": 12, "1": 200, ...}."""
    return {str(i): (semente * 31 + i * 17) % 256 for i in range(n)}


def v237(data: dict, sender: str = PEDRO) -> dict:
    """Formato REAL da Evolution API v2.3.7 (capturado em 2026-10-19, instância no próprio número de Pedro):
    remoteJidAlt + addressingMode "lid" na key, `conversation` em vez de extendedTextMessage e o contextInfo
    (citação/menções) em data.contextInfo — fora de `message`. Áudio chega SEM base64 (ver evolution_v237_audio)."""
    k = data["key"]
    if not k["remoteJid"].endswith("@g.us"):
        k.setdefault("remoteJidAlt", k["remoteJid"])
    k.setdefault("participant", "")
    k["addressingMode"] = "lid"
    data.setdefault("status", "SERVER_ACK" if k.get("fromMe") else "DELIVERY_ACK")
    data["message"].setdefault("messageContextInfo", {
        "threadId": [], "deviceListMetadataVersion": 2, "messageSecret": bytes_json(32),
        "deviceListMetadata": {"senderKeyIndexes": [], "recipientKeyIndexes": [], "recipientKeyHash": bytes_json(10),
                               "recipientTimestamp": {"low": ts("09:00"), "high": 0, "unsigned": True}}})
    data.setdefault("contextInfo", {"mentionedJid": [], "groupMentions": [], "statusAttributions": []})
    data.setdefault("instanceId", "8acc06c7-6d9b-4aee-9331-1f9d2924e74b")
    p = envelope(data, sender=sender)
    p["destination"] = "http://172.17.0.1:7072/whatsapp/webhook-async"
    return p


def gravar_audio():
    import edge_tts
    mp3 = FIX / "audio_carlos_fatec.mp3"
    asyncio.run(edge_tts.Communicate(TEXTO_AUDIO, "pt-BR-AntonioNeural").save(str(mp3)))
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg não encontrado — necessário para converter em OGG/Opus")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", "-b:a", "24k",
                    "-ac", "1", "-ar", "48000", str(OGG)], check=True)
    mp3.unlink()


def main():
    if "--audio" in sys.argv or not OGG.exists():
        gravar_audio()
    OUT.mkdir(exist_ok=True)
    b64 = base64.b64encode(OGG.read_bytes()).decode("ascii")

    payloads = {
        # 1. texto de um contato profissional
        "evolution_texto.json": envelope({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60001"},
            "pushName": "Carlos Fatec",
            "message": {"conversation": "Pedro, bom dia! Conseguiu ver o orçamento do servidor novo do laboratório? "
                                        "Preciso fechar com a diretoria até sexta."},
            "messageType": "conversation",
            "messageTimestamp": ts("09:10"),
            "instanceId": "550e8400-e29b-41d4-a716-446655440000",
            "source": "android",
        }),
        # 2. áudio (ptt) com base64 no payload (webhook.base64 = true)
        "evolution_audio.json": envelope({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60002"},
            "pushName": "Carlos Fatec",
            "message": {
                "audioMessage": {
                    "url": "https://mmg.whatsapp.net/v/t62.7117-24/exemplo.enc",
                    "mimetype": "audio/ogg; codecs=opus",
                    "fileSha256": "AAAA", "fileLength": str(OGG.stat().st_size), "seconds": 8,
                    "ptt": True, "mediaKey": "BBBB", "fileEncSha256": "CCCC", "directPath": "/v/t62.7117-24/exemplo.enc",
                    "mediaKeyTimestamp": str(ts("09:11")), "waveform": "AAAAAAAA",
                },
                "base64": b64,
            },
            "messageType": "audioMessage",
            "messageTimestamp": ts("09:11"),
            "instanceId": "550e8400-e29b-41d4-a716-446655440000",
            "source": "android",
        }),
        # 3. mensagem de grupo (ignorada por padrão)
        "evolution_grupo.json": envelope({
            "key": {"remoteJid": GRUPO, "fromMe": False, "id": "3EB0A1B2C3D4E5F60003", "participant": f"{AMIGA}@s.whatsapp.net"},
            "pushName": "Juliana",
            "message": {"conversation": "Gente, alguém vai no churrasco sábado?"},
            "messageType": "conversation",
            "messageTimestamp": ts("09:12"),
            "source": "ios",
        }),
        # 4. Pedro (de outro número) responde CITANDO a pergunta do Alfred → responder_texto
        #    O texto citado é a mensagem que o Alfred mandou; termina com "ref: <id>".
        #    O id é preenchido pelo teste (placeholder __REF__).
        "evolution_resposta_pedro.json": envelope({
            "key": {"remoteJid": f"{PEDRO}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60004"},
            "pushName": "Pedro Netto",
            "message": {
                "extendedTextMessage": {
                    "text": "Carlos, bom dia! Mando o orçamento até quinta à tarde, pode contar.",
                    "contextInfo": {
                        "stanzaId": "SIMULADO-PERGUNTA",
                        "participant": f"{ALFRED}@s.whatsapp.net",
                        "quotedMessage": {"conversation": "🎩 *Alfred* — mensagem de *Carlos Fatec* no WhatsApp\n…\nref: __REF__"},
                    },
                }
            },
            "messageType": "extendedTextMessage",
            "messageTimestamp": ts("09:20"),
            "source": "android",
        }),
        # 5. Pedro responde "ignorar" citando
        "evolution_resposta_ignorar.json": envelope({
            "key": {"remoteJid": f"{PEDRO}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60005"},
            "pushName": "Pedro Netto",
            "message": {
                "extendedTextMessage": {
                    "text": "ignorar",
                    "contextInfo": {"stanzaId": "SIMULADO-PERGUNTA", "participant": f"{ALFRED}@s.whatsapp.net",
                                    "quotedMessage": {"conversation": "🎩 *Alfred* — …\nref: __REF__"}},
                }
            },
            "messageType": "extendedTextMessage",
            "messageTimestamp": ts("09:21"),
            "source": "android",
        }),
        # 6. instância no PRÓPRIO número de Pedro: ele responde o Carlos direto do celular (fromMe)
        "evolution_fromme_direto.json": envelope({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": True, "id": "3EB0A1B2C3D4E5F60006"},
            "pushName": "Pedro Netto",
            "message": {"conversation": "Carlos, já te mando, só fechando os números."},
            "messageType": "conversation",
            "messageTimestamp": ts("09:22"),
            "source": "android",
        }, sender=PEDRO),
        # 7. spam de número desconhecido
        "evolution_spam.json": envelope({
            "key": {"remoteJid": "5511900001111@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60007"},
            "pushName": "",
            "message": {"conversation": "🎁 PARABÉNS! Você foi sorteado. Clique no link para resgatar seu prêmio: http://bit.ly/xxxx"},
            "messageType": "conversation",
            "messageTimestamp": ts("09:25"),
            "source": "unknown",
        }),
        # 8. mensagem pessoal que não pede resposta
        "evolution_pessoal_sem_resposta.json": envelope({
            "key": {"remoteJid": f"{AMIGA}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1B2C3D4E5F60008"},
            "pushName": "Juliana",
            "message": {"conversation": "obrigada pela carona ontem!! 😊"},
            "messageType": "conversation",
            "messageTimestamp": ts("09:26"),
            "source": "ios",
        }),

        # ── Formato real da v2.3.7 (instância no próprio número de Pedro) ──────────
        # 9. texto de contato: contextInfo fora de `message`, sem citação
        "evolution_v237_texto.json": v237({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": False, "id": "A5E4CF8E7679BCE8A8A936A815FED0A3"},
            "pushName": "Carlos Fatec",
            "message": {"conversation": "Pedro, manda o orçamento do servidor até quinta?"},
            "contextInfo": {"mentionedJid": [], "groupMentions": [], "statusAttributions": [],
                            "entryPointConversionSource": "global_search_new_chat", "entryPointConversionApp": "whatsapp",
                            "entryPointConversionDelaySeconds": 12},
            "messageType": "conversation",
            "messageTimestamp": ts("09:30"),
            "source": "android",
        }),
        # 10. áudio SEM base64 (a v2.3.7 só inclui o base64 se o webhook já existia quando a instância conectou):
        #     o Python baixa com getBase64FromMediaMessage passando o audioMessage original (mediaKey em bytes JSON)
        "evolution_v237_audio_sem_base64.json": v237({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": False, "id": "A56164FA6A523054ADF7EEDAF6881119"},
            "pushName": "Carlos Fatec",
            "message": {"audioMessage": {
                "url": "https://mmg.whatsapp.net/v/t62.7117-24/exemplo.enc?ccb=11-4&oh=01_exemplo&oe=6A00_exemplo&mms3=true",
                "mimetype": "audio/ogg; codecs=opus", "fileSha256": bytes_json(32, 1),
                "fileLength": {"low": OGG.stat().st_size, "high": 0, "unsigned": True}, "seconds": 8, "ptt": True,
                "mediaKey": bytes_json(32, 2), "fileEncSha256": bytes_json(32, 3),
                "directPath": "/v/t62.7117-24/exemplo.enc?ccb=11-4&oh=01_exemplo&oe=6A00_exemplo",
                "mediaKeyTimestamp": {"low": ts("09:31"), "high": 0, "unsigned": False},
                "contextInfo": {"mentionedJid": [], "groupMentions": [], "statusAttributions": []},
                "waveform": bytes_json(64, 4)}},
            "messageType": "audioMessage",
            "messageTimestamp": ts("09:31"),
            "source": "android",
        }),
        # 11. Pedro responde CITANDO o resumo na conversa "Você" (fromMe, remoteJid = o próprio número)
        "evolution_v237_resposta_pedro.json": v237({
            "key": {"remoteJid": f"{PEDRO}@s.whatsapp.net", "fromMe": True, "id": "3EB01EAC3AD52E69729042"},
            "pushName": "Pedro Netto",
            "message": {"conversation": "Claro, estou na execução dele"},
            "contextInfo": {"mentionedJid": [], "groupMentions": [], "statusAttributions": [],
                            "stanzaId": "3EB05F4E8EF5B8E031816C", "participant": f"{PEDRO}@s.whatsapp.net",
                            "quotedMessage": {"conversation": "🎩 *Alfred* — mensagem de *Carlos Fatec* no WhatsApp (texto)\n…\nref: __REF__"},
                            "disappearingMode": {"initiator": 0, "trigger": 1}},
            "messageType": "conversation",
            "messageTimestamp": ts("09:35"),
            "source": "web",
        }),
        # 12. idem com "ignorar"
        "evolution_v237_resposta_ignorar.json": v237({
            "key": {"remoteJid": f"{PEDRO}@s.whatsapp.net", "fromMe": True, "id": "3EB0C0FFEE00000000000012"},
            "pushName": "Pedro Netto",
            "message": {"conversation": "ignorar"},
            "contextInfo": {"mentionedJid": [], "groupMentions": [], "statusAttributions": [],
                            "stanzaId": "3EB05F4E8EF5B8E031816C", "participant": f"{PEDRO}@s.whatsapp.net",
                            "quotedMessage": {"conversation": "🎩 *Alfred* — …\nref: __REF__"}},
            "messageType": "conversation",
            "messageTimestamp": ts("09:36"),
            "source": "android",
        }),
        # 13. Pedro responde o contato direto no celular — citando a mensagem DO CONTATO (sem ref)
        "evolution_v237_fromme_direto.json": v237({
            "key": {"remoteJid": f"{CARLOS}@s.whatsapp.net", "fromMe": True, "id": "AC1F8EAB4D0C31160708D8A7BE04C30C"},
            "pushName": "Pedro Netto",
            "message": {"conversation": "Tenho horário para as 16:00, pode ser?"},
            "contextInfo": {"mentionedJid": [], "groupMentions": [], "statusAttributions": [],
                            "stanzaId": "A5E4CF8E7679BCE8A8A936A815FED0A3", "participant": f"{CARLOS}@s.whatsapp.net",
                            "quotedMessage": {"conversation": "Pedro, manda o orçamento do servidor até quinta?"},
                            "quotedType": 0},
            "messageType": "conversation",
            "messageTimestamp": ts("09:37"),
            "source": "android",
        }),
        # 14. grupo: key.participant é um "@lid"; o número vem em key.participantAlt
        "evolution_v237_grupo.json": v237({
            "key": {"remoteJid": GRUPO, "fromMe": False, "id": "ACB980C382A582DDEB61E0476411110A",
                    "participant": "123456789012345@lid", "participantAlt": f"{AMIGA}@s.whatsapp.net"},
            "pushName": "Juliana",
            "message": {"conversation": "Vocês já chegaram na praça?"},
            "messageType": "conversation",
            "messageTimestamp": ts("09:38"),
            "source": "ios",
        }),
    }
    for nome, p in payloads.items():
        (OUT / nome).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {nome} ({(OUT / nome).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
