/* Experiment API "alfredCalendar": acesso mínimo ao calendário do Thunderbird (cal.manager).
 * O Thunderbird não tem API pública de calendário para extensões; este código roda no processo
 * principal com os módulos internos do Thunderbird 128+ (testado contra os módulos do 157). */
"use strict";

var { ExtensionCommon } = ChromeUtils.importESModule("resource://gre/modules/ExtensionCommon.sys.mjs");
var { cal } = ChromeUtils.importESModule("resource:///modules/calendar/calUtils.sys.mjs");
var { CalEvent } = ChromeUtils.importESModule("resource:///modules/CalEvent.sys.mjs");
var { setTimeout } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");

// No CalDAV do Google, addItem() manda o PUT e depois relê o evento pelo endereço onde o gravou; o
// Google guarda o evento com outro endereço (o id dele), a releitura não acha nada e a promise nunca
// resolve, embora o evento já esteja no servidor (CalDavCalendar.doAdoptItem → getUpdatedItem). Os
// caminhos de erro do PUT respondem na hora; sem resposta em ADD_ESPERA_MS, o PUT foi aceito:
// sincroniza o calendário e confirma pelo UID no cache, sem deixar o ciclo da extensão parado.
const ADD_ESPERA_MS = 15000;
const CONFIRMA_MS = 60000;
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

async function adicionar(calendar, item) {
  let falha = null;
  const feito = calendar.addItem(item).then(() => true, (e) => { falha = e; return true; });
  const respondeu = await Promise.race([feito, esperar(ADD_ESPERA_MS).then(() => false)]);
  if (falha) throw falha;
  if (respondeu) return;
  calendar.refresh();
  for (let t = 0; t < CONFIRMA_MS; t += 2000) {
    await esperar(2000);
    if (falha) throw falha;
    if (await calendar.getItem(item.id)) return;
  }
  throw new ExtensionCommon.ExtensionError("o servidor não confirmou o evento (tento de novo no próximo ciclo)");
}

function calendarioOuErro(id) {
  const calendar = cal.manager.getCalendarById(id);
  if (!calendar) throw new ExtensionCommon.ExtensionError(`calendário não encontrado: ${id}`);
  if (calendar.readOnly) throw new ExtensionCommon.ExtensionError(`calendário somente leitura: ${calendar.name}`);
  return calendar;
}

this.alfredCalendar = class extends ExtensionCommon.ExtensionAPI {
  getAPI(context) {
    return {
      alfredCalendar: {
        async listCalendars() {
          return cal.manager.getCalendars().map((c) => ({
            id: c.id,
            name: c.name,
            type: c.type,
            readOnly: !!c.readOnly,
            disabled: !!c.getProperty("disabled"),
          }));
        },

        async createEvent(calendarId, ics) {
          const calendar = calendarioOuErro(calendarId);
          const item = new CalEvent();
          item.icalString = ics;               // aceita VCALENDAR com um VEVENT (UID vem do Alfred)
          item.calendar = calendar.superCalendar;
          const existente = await calendar.getItem(item.id);
          if (existente) {
            await calendar.modifyItem(item, existente);
            return { id: item.id, acao: "atualizado" };
          }
          await adicionar(calendar, item);
          return { id: item.id, acao: "criado" };
        },

        async deleteEvent(calendarId, uid) {
          const calendar = calendarioOuErro(calendarId);
          const existente = await calendar.getItem(uid);
          if (!existente) return false;
          await calendar.deleteItem(existente);
          return true;
        },
      },
    };
  }
};
