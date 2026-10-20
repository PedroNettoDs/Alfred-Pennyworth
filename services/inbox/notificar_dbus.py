#!/usr/bin/python3
"""
Alfred — notificação com botões no PC (org.freedesktop.Notifications) que espera o clique.

    echo '{"titulo": "...", "corpo": "...", "acoes": [["enviar", "Enviar"], ["nao", "Não enviar"]]}' \
        | /usr/bin/python3 -I notificar_dbus.py

Imprime a chave do botão clicado ("" = notificação fechada sem clique) e sai 0; sai 2 se não houver
D-Bus de sessão, PyGObject ou servidor com "actions" (quem chama usa o fallback kdialog/zenity).

Roda no Python do SISTEMA (PyGObject/Gio), não no venv. Por que não notify-send / gdbus call:
  - no Ubuntu o perfil AppArmor "notify-send" bloqueia GetCapabilities para o plasmashell → o notify-send
    responde "Actions are not supported" e mostra a notificação sem botões;
  - o Plasma 6 fecha na hora (motivo 1) as notificações com ações cujo remetente saiu do barramento — o
    `gdbus call` sai logo após o Notify. Aqui a mesma conexão envia e fica ouvindo até o clique.
"""
import json
import sys

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except Exception:
    sys.exit(2)

BUS = IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


def main() -> int:
    req = json.load(sys.stdin)
    try:
        con = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        caps = con.call_sync(BUS, PATH, IFACE, "GetCapabilities", None, GLib.VariantType("(as)"),
                             Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
    except Exception:
        return 2
    if "actions" not in caps:
        return 2

    loop = GLib.MainLoop()
    estado = {"id": None, "resposta": ""}

    def sinal(_con, _remetente, _path, _iface, nome, params):
        args = params.unpack()
        if args[0] != estado["id"]:
            return
        if nome == "ActionInvoked":
            estado["resposta"] = args[1]
            loop.quit()
        elif nome == "NotificationClosed" and args[1] != 1:   # 1 = expirou/foi pro histórico: o clique ainda vale
            loop.quit()

    con.signal_subscribe(BUS, IFACE, None, PATH, None, Gio.DBusSignalFlags.NONE, sinal)
    corpo = req.get("corpo", "")
    if "body-markup" in caps:
        corpo = GLib.markup_escape_text(corpo)   # "<", "&" na mensagem viravam "invalid XML" no Plasma
    acoes = [str(x) for par in req.get("acoes", []) for x in par]
    hints = {"urgency": GLib.Variant("y", 2)}   # crítica + expira 0: fica até Pedro clicar ou fechar
    try:
        estado["id"] = con.call_sync(
            BUS, PATH, IFACE, "Notify",
            GLib.Variant("(susssasa{sv}i)", ("Alfred", 0, req.get("icone", "mail-message-new"),
                                             req.get("titulo", "Alfred"), corpo, acoes, hints, 0)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
    except Exception:
        return 2
    loop.run()
    print(estado["resposta"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
