/* Experiment API "alfredCalendar": acesso mínimo ao calendário do Thunderbird (cal.manager).
 * O Thunderbird não tem API pública de calendário para extensões; este código roda no processo
 * principal com os módulos internos do Thunderbird 128+ (testado contra os módulos do 157). */
"use strict";

var { ExtensionCommon } = ChromeUtils.importESModule("resource://gre/modules/ExtensionCommon.sys.mjs");
var { cal } = ChromeUtils.importESModule("resource:///modules/calendar/calUtils.sys.mjs");
var { CalEvent } = ChromeUtils.importESModule("resource:///modules/CalEvent.sys.mjs");

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
          await calendar.addItem(item);
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
