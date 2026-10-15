.pragma library

// Interface translations. All built in: nothing is
// downloaded or generated. The language follows the system (LC_ALL,
// LC_MESSAGES, LANG) unless one is picked in the editor; anything without a
// translation falls back to English.

var LANGUAGES = [
  { code: "en", name: "English" },
  { code: "de", name: "Deutsch" },
  { code: "fr", name: "Français" },
  { code: "es", name: "Español" },
  { code: "it", name: "Italiano" },
  { code: "pt", name: "Português" },
  { code: "nl", name: "Nederlands" },
  { code: "pl", name: "Polski" }
]

// "xx_YY.UTF-8" / "xx-YY" / "xx" -> "xx" when we have it, else "en".
function systemLanguage(lcAll, lcMessages, lang) {
  var values = [lcAll, lcMessages, lang]
  for (var i = 0; i < values.length; i++) {
    var code = String(values[i] || "").toLowerCase().split(/[_.@-]/)[0]
    if (code === "c" || code === "posix" || code === "") continue
    return supported(code) ? code : "en"
  }
  return "en"
}

function supported(code) {
  for (var i = 0; i < LANGUAGES.length; i++) if (LANGUAGES[i].code === code) return true
  return false
}

function languageName(code) {
  for (var i = 0; i < LANGUAGES.length; i++) if (LANGUAGES[i].code === code) return LANGUAGES[i].name
  return code
}

// The setting is "auto" (system) or a language code.
function resolve(setting, systemCode) {
  return setting && setting !== "auto" && supported(setting) ? setting : systemCode
}

function format(text, args) {
  return String(text).replace(/\{(\d+)\}/g, function(match, index) {
    var value = args && args[Number(index)]
    return value === undefined || value === null ? match : String(value)
  })
}

function tr(lang, key, args) {
  var table = STRINGS[lang] || STRINGS.en
  var text = table[key]
  if (text === undefined) text = STRINGS.en[key]
  if (text === undefined) return key
  if (Array.isArray(text)) text = text[text.length - 1]
  return format(text, args)
}

// Plural: entries are [one, other], or [one, few, many] for Polish.
function trn(lang, key, n, args) {
  var table = STRINGS[lang] || STRINGS.en
  var forms = table[key] !== undefined ? table[key] : STRINGS.en[key]
  if (forms === undefined) return key
  if (!Array.isArray(forms)) return format(forms, [n].concat(args || []))
  var index
  if (lang === "pl" && forms.length === 3) {
    var mod10 = n % 10, mod100 = n % 100
    index = n === 1 ? 0 : (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) ? 1 : 2
  } else if (lang === "fr" || lang === "pt") {
    index = n <= 1 ? 0 : forms.length - 1
  } else {
    index = n === 1 ? 0 : forms.length - 1
  }
  return format(forms[index], [n].concat(args || []))
}

var STRINGS = {
  en: {
      "settings": "Settings",
      "back": "Back",
      "backTip": "Back (Backspace, Esc)",
      "language": "Language",
      "systemLanguage": "System ({0})",
      "tip": "AI Agents Map"
  },
  de: {
      "settings": "Einstellungen",
      "back": "Zurück",
      "backTip": "Zurück (Rücktaste, Esc)",
      "language": "Sprache",
      "systemLanguage": "System ({0})",
      "tip": "AI Agents Map"
  },
  fr: {
      "settings": "Réglages",
      "back": "Retour",
      "backTip": "Retour (Retour arrière, Échap)",
      "language": "Langue",
      "systemLanguage": "Système ({0})",
      "tip": "AI Agents Map"
  },
  es: {
      "settings": "Ajustes",
      "back": "Atrás",
      "backTip": "Atrás (Retroceso, Esc)",
      "language": "Idioma",
      "systemLanguage": "Sistema ({0})",
      "tip": "AI Agents Map"
  },
  it: {
      "settings": "Impostazioni",
      "back": "Indietro",
      "backTip": "Indietro (Backspace, Esc)",
      "language": "Lingua",
      "systemLanguage": "Sistema ({0})",
      "tip": "AI Agents Map"
  },
  pt: {
      "settings": "Configurações",
      "back": "Voltar",
      "backTip": "Voltar (Backspace, Esc)",
      "language": "Idioma",
      "systemLanguage": "Sistema ({0})",
      "tip": "AI Agents Map"
  },
  nl: {
      "settings": "Instellingen",
      "back": "Terug",
      "backTip": "Terug (Backspace, Esc)",
      "language": "Taal",
      "systemLanguage": "Systeem ({0})",
      "tip": "AI Agents Map"
  },
  pl: {
      "settings": "Ustawienia",
      "back": "Wstecz",
      "backTip": "Wstecz (Backspace, Esc)",
      "language": "Język",
      "systemLanguage": "Systemowy ({0})",
      "tip": "AI Agents Map"
  }
}
