"use strict";

const languageNames = {
  "zh-TW": "繁體中文", "zh-CN": "简体中文", en: "English", fr: "Français", de: "Deutsch",
  "ja-JP": "日本語", "sr-Latn": "Srpski (latinica)",
};
const languagePreference = "mailharbor.language";
let uiLanguage = "zh-TW";
try {
  const saved = localStorage.getItem(languagePreference);
  if (Object.hasOwn(languageNames, saved)) uiLanguage = saved;
} catch { /* Browser storage may be disabled. Keep the language in memory. */ }

function t(source, ...values) {
  const phrase = Object.hasOwn(translations, source) ? translations[source][uiLanguage] ?? source : source;
  if (!values.length) return phrase;
  return phrase.replace(/\{(\d+)\}/g, (marker, index) => {
    const value = values[index];
    return typeof value === "number" ? numberFormat.format(value) : value ?? marker;
  });
}

// Capture only original, application-owned HTML before any asynchronous data is rendered.
// Keep references to those text nodes so switching languages can never rewrite mailbox content.
const staticCopy = [];
function captureStaticCopy() {
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const item = walker.currentNode;
    const key = item.data.trim();
    if (Object.hasOwn(translations, key)) {
      const original = item.data;
      staticCopy.push(() => { item.data = original.replace(key, () => t(key)); });
    }
  }
  for (const element of document.querySelectorAll("[placeholder], [aria-label], [title]")) {
    for (const attribute of ["placeholder", "aria-label", "title"]) {
      const key = element.getAttribute(attribute);
      if (Object.hasOwn(translations, key)) staticCopy.push(() => element.setAttribute(attribute, t(key)));
    }
  }
}

let numberFormat, dateFormat, longDateFormat;
function applyLanguage(language) {
  uiLanguage = language;
  document.documentElement.lang = language;
  const options = { timeZone: "Europe/Belgrade", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  numberFormat = new Intl.NumberFormat(language);
  dateFormat = new Intl.DateTimeFormat(language, options);
  longDateFormat = new Intl.DateTimeFormat(language, { ...options, year: "numeric" });
  for (const render of staticCopy) render();
  for (const id of ["login-language", "settings-language"]) document.getElementById(id).value = language;
}

function bindLanguageControls(onChange) {
  captureStaticCopy();
  for (const id of ["login-language", "settings-language"]) {
    const select = document.getElementById(id);
    for (const [code, name] of Object.entries(languageNames)) {
      const option = new Option(name, code);
      option.lang = code;
      select.add(option);
    }
    select.addEventListener("change", () => {
      const language = select.value;
      if (!Object.hasOwn(languageNames, language) || language === uiLanguage) return;
      const previous = uiLanguage;
      applyLanguage(language);
      try { localStorage.setItem(languagePreference, language); } catch { /* Switching still works in this tab. */ }
      onChange(previous);
    });
  }
  applyLanguage(uiLanguage);
}

// These elements hold app feedback. Preserve a displayed error when switching languages.
function translateFeedback(previous) {
  const reverse = new Map(Object.entries(translations).map(([source, row]) => [row[previous] ?? source, source]));
  for (const item of document.querySelectorAll(".field-error, .error-line, #settings-status, #network-banner, #callback-banner, #live-status")) {
    const key = reverse.get(item.textContent);
    if (key) item.textContent = t(key);
  }
}
