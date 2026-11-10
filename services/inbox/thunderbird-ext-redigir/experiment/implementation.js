/* Experiment API "alfredAgendaLeitura": só leitura dos calendários do Thunderbird (não há API pública para isso). */
"use strict";

var { ExtensionCommon } = ChromeUtils.importESModule("resource://gre/modules/ExtensionCommon.sys.mjs");
var { cal } = ChromeUtils.importESModule("resource:///modules/calendar/calUtils.sys.mjs");

const LIMITE_POR_CALENDARIO = 300;

function paraDateTime(iso) {
  return cal.dtz.jsDateToDateTime(new Date(iso), cal.dtz.defaultTimezone);
}

// Dia inteiro sai como data pura (AAAA-MM-DD): convertida para UTC cairia no dia errado.
function paraISO(dt) {
  if (!dt) return null;
  if (dt.isDate) {
    return `${dt.year}-${String(dt.month + 1).padStart(2, "0")}-${String(dt.day).padStart(2, "0")}`;
  }
  return cal.dtz.dateTimeToJsDate(dt).toISOString();
}

this.alfredAgendaLeitura = class extends ExtensionCommon.ExtensionAPI {
  getAPI(context) {
    return {
      alfredAgendaLeitura: {
        async listarEventos(inicioISO, fimISO) {
          const filtro = Ci.calICalendar.ITEM_FILTER_TYPE_EVENT | Ci.calICalendar.ITEM_FILTER_CLASS_OCCURRENCES;
          const inicio = paraDateTime(inicioISO);
          const fim = paraDateTime(fimISO);
          const eventos = [];
          for (const calendario of cal.manager.getCalendars()) {
            if (calendario.getProperty("disabled")) continue;
            const itens = await calendario.getItemsAsArray(filtro, LIMITE_POR_CALENDARIO, inicio, fim);
            for (const item of itens) {
              if (item.status === "CANCELLED") continue;
              eventos.push({
                titulo: item.title || "",
                inicio: paraISO(item.startDate),
                fim: paraISO(item.endDate),
                dia_inteiro: !!item.startDate.isDate,
                local: item.getProperty("LOCATION") || "",
              });
            }
          }
          return eventos;
        },
      },
    };
  }
};
