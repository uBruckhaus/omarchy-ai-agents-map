"""Interface translations (built in; nothing is downloaded or generated).

gettext style: the English text is the key. `_("Add a shortcut")`, and for
text with values `_("Opens {0}").format(name)`. Anything without a
translation stays English. The language follows the system (LC_ALL,
LC_MESSAGES, LANG) unless one is picked in the settings (`language` in
SETTINGS_FILE, "auto" or a code).
"""

import json
import os
from pathlib import Path

LANGUAGES = [
    ("en", "English"), ("de", "Deutsch"), ("fr", "Français"), ("es", "Español"),
    ("it", "Italiano"), ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"),
]
CODES = {code for code, _name in LANGUAGES}
SETTINGS_FILE = None  # set by the plugin: Path of a small JSON settings file
_lang = None


def system_language():
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = (os.environ.get(var) or "").lower()
        code = value.replace("-", "_").split("_")[0].split(".")[0].split("@")[0]
        if code in ("", "c", "posix"):
            continue
        return code if code in CODES else "en"
    return "en"


def language_setting():
    """'auto' or a language code from the settings file."""
    if SETTINGS_FILE is None:
        return "auto"
    try:
        with open(SETTINGS_FILE, "rb") as fh:
            data = json.loads(fh.read(64 * 1024) or b"{}")
    except (OSError, ValueError):
        return "auto"
    value = data.get("language") if isinstance(data, dict) else None
    return value if value in CODES else "auto"


def save_language(code):
    """Store the choice (atomic, private); "auto" follows the system again."""
    if SETTINGS_FILE is None:
        return
    path = Path(SETTINGS_FILE)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(64 * 1024) or b"{}")
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    if code == "auto":
        data.pop("language", None)
    elif code in CODES:
        data["language"] = code
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    global _lang
    _lang = None


def current():
    global _lang
    if _lang is None:
        setting = language_setting()
        _lang = setting if setting != "auto" else system_language()
    return _lang


def language_name(code):
    return dict(LANGUAGES).get(code, code)


def _(text):
    return STRINGS.get(current(), {}).get(text, text)


def N_(text):
    """Mark a module-level constant for translation without translating it yet:
    the language is only known once settings are read. Translate it with _() where it is used."""
    return text


PLURAL_PREFIX = "\x04"   # plural tables live under their own keys, apart from plain msgids


def ngettext(one, other, n):
    """Plural: tables store a list of forms under PLURAL_PREFIX + the English
    `one` text ([one, other], or [one, few, many] for Polish)."""
    lang = current()
    forms = STRINGS.get(lang, {}).get(PLURAL_PREFIX + one)
    if not isinstance(forms, list):
        return one if n == 1 else other
    if lang == "pl" and len(forms) == 3:
        m10, m100 = n % 10, n % 100
        return forms[0] if n == 1 else forms[1] if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else forms[2]
    if lang in ("fr", "pt"):
        return forms[0] if n <= 1 else forms[-1]
    return forms[0] if n == 1 else forms[-1]


STRINGS = {
 "de": {
  "AI Manager restarts in the new language.": "AI Manager startet in der neuen Sprache neu.",
  "AI Manager view": "AI-Manager-Ansicht",
  "AI Manager view (Ctrl+1)": "AI-Manager-Ansicht (Strg+1)",
  "Back": "Zurück",
  "Back (Backspace, Esc)": "Zurück (Rücktaste, Esc)",
  "Build something here.": "Hier entsteht etwas.",
  "Builder": "Builder",
  "Builder view": "Builder-Ansicht",
  "Builder view (Ctrl+2)": "Builder-Ansicht (Strg+2)",
  "Double-click: default size": "Doppelklick: Standardgröße",
  "Double-click: default size · right double-click: maximize": "Doppelklick: Standardgröße · Rechts-Doppelklick: maximieren",
  "Double-click: maximize": "Doppelklick: maximieren",
  "Drag to move": "Ziehen zum Verschieben",
  "Drag to resize · double-click for the default size": "Ziehen zum Ändern der Größe · Doppelklick für die Standardgröße",
  "Empty canvas": "Leere Arbeitsfläche",
  "Language": "Sprache",
  "Last used view": "Zuletzt benutzte Ansicht",
  "Nothing here yet": "Noch nichts hier",
  "Open on": "Öffnen mit",
  "Overview": "Übersicht",
  "Settings": "Einstellungen",
  "System ({0})": "System ({0})",
  "The view shown when AI Manager opens.": "Die Ansicht, mit der AI Manager öffnet.",
  "Your AI tools will be listed here.": "Hier erscheinen deine KI-Werkzeuge."
 },
 "es": {
  "AI Manager restarts in the new language.": "AI Manager se reinicia en el nuevo idioma.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Back": "Atrás",
  "Back (Backspace, Esc)": "Atrás (Retroceso, Esc)",
  "Build something here.": "Construye algo aquí.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Double-click: default size": "Doble clic: tamaño predeterminado",
  "Double-click: default size · right double-click: maximize": "Doble clic: tamaño predeterminado · doble clic derecho: maximizar",
  "Double-click: maximize": "Doble clic: maximizar",
  "Drag to move": "Arrastra para mover",
  "Drag to resize · double-click for the default size": "Arrastra para cambiar el tamaño · doble clic para el tamaño predeterminado",
  "Empty canvas": "Lienzo vacío",
  "Language": "Idioma",
  "Last used view": "Última vista usada",
  "Nothing here yet": "Aún no hay nada",
  "Open on": "Abrir en",
  "Overview": "Resumen",
  "Settings": "Ajustes",
  "System ({0})": "Sistema ({0})",
  "The view shown when AI Manager opens.": "La vista que muestra AI Manager al abrirse.",
  "Your AI tools will be listed here.": "Tus herramientas de IA aparecerán aquí."
 },
 "fr": {
  "AI Manager restarts in the new language.": "AI Manager redémarre dans la nouvelle langue.",
  "AI Manager view": "Vue AI Manager",
  "AI Manager view (Ctrl+1)": "Vue AI Manager (Ctrl+1)",
  "Back": "Retour",
  "Back (Backspace, Esc)": "Retour (Retour arrière, Échap)",
  "Build something here.": "Construisez quelque chose ici.",
  "Builder": "Builder",
  "Builder view": "Vue Builder",
  "Builder view (Ctrl+2)": "Vue Builder (Ctrl+2)",
  "Double-click: default size": "Double-clic : taille par défaut",
  "Double-click: default size · right double-click: maximize": "Double-clic : taille par défaut · double-clic droit : agrandir",
  "Double-click: maximize": "Double-clic : agrandir",
  "Drag to move": "Glisser pour déplacer",
  "Drag to resize · double-click for the default size": "Glisser pour redimensionner · double-clic pour la taille par défaut",
  "Empty canvas": "Canevas vide",
  "Language": "Langue",
  "Last used view": "Dernière vue utilisée",
  "Nothing here yet": "Rien ici pour l'instant",
  "Open on": "Ouvrir sur",
  "Overview": "Vue d'ensemble",
  "Settings": "Réglages",
  "System ({0})": "Système ({0})",
  "The view shown when AI Manager opens.": "La vue affichée à l'ouverture d'AI Manager.",
  "Your AI tools will be listed here.": "Vos outils d'IA apparaîtront ici."
 },
 "it": {
  "AI Manager restarts in the new language.": "AI Manager si riavvia nella nuova lingua.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Back": "Indietro",
  "Back (Backspace, Esc)": "Indietro (Backspace, Esc)",
  "Build something here.": "Costruisci qualcosa qui.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Double-click: default size": "Doppio clic: dimensione predefinita",
  "Double-click: default size · right double-click: maximize": "Doppio clic: dimensione predefinita · doppio clic destro: massimizza",
  "Double-click: maximize": "Doppio clic: massimizza",
  "Drag to move": "Trascina per spostare",
  "Drag to resize · double-click for the default size": "Trascina per ridimensionare · doppio clic per la dimensione predefinita",
  "Empty canvas": "Area vuota",
  "Language": "Lingua",
  "Last used view": "Ultima vista usata",
  "Nothing here yet": "Ancora niente qui",
  "Open on": "Apri su",
  "Overview": "Panoramica",
  "Settings": "Impostazioni",
  "System ({0})": "Sistema ({0})",
  "The view shown when AI Manager opens.": "La vista mostrata all'apertura di AI Manager.",
  "Your AI tools will be listed here.": "Qui compariranno i tuoi strumenti di IA."
 },
 "nl": {
  "AI Manager restarts in the new language.": "AI Manager start opnieuw in de nieuwe taal.",
  "AI Manager view": "AI Manager-weergave",
  "AI Manager view (Ctrl+1)": "AI Manager-weergave (Ctrl+1)",
  "Back": "Terug",
  "Back (Backspace, Esc)": "Terug (Backspace, Esc)",
  "Build something here.": "Bouw hier iets.",
  "Builder": "Builder",
  "Builder view": "Builder-weergave",
  "Builder view (Ctrl+2)": "Builder-weergave (Ctrl+2)",
  "Double-click: default size": "Dubbelklik: standaardgrootte",
  "Double-click: default size · right double-click: maximize": "Dubbelklik: standaardgrootte · rechts dubbelklikken: maximaliseren",
  "Double-click: maximize": "Dubbelklik: maximaliseren",
  "Drag to move": "Sleep om te verplaatsen",
  "Drag to resize · double-click for the default size": "Sleep om het formaat te wijzigen · dubbelklik voor de standaardgrootte",
  "Empty canvas": "Leeg canvas",
  "Language": "Taal",
  "Last used view": "Laatst gebruikte weergave",
  "Nothing here yet": "Nog niets hier",
  "Open on": "Openen op",
  "Overview": "Overzicht",
  "Settings": "Instellingen",
  "System ({0})": "Systeem ({0})",
  "The view shown when AI Manager opens.": "De weergave waarmee AI Manager opent.",
  "Your AI tools will be listed here.": "Je AI-tools verschijnen hier."
 },
 "pl": {
  "AI Manager restarts in the new language.": "AI Manager uruchomi się ponownie w nowym języku.",
  "AI Manager view": "Widok AI Manager",
  "AI Manager view (Ctrl+1)": "Widok AI Manager (Ctrl+1)",
  "Back": "Wstecz",
  "Back (Backspace, Esc)": "Wstecz (Backspace, Esc)",
  "Build something here.": "Zbuduj tu coś.",
  "Builder": "Builder",
  "Builder view": "Widok Builder",
  "Builder view (Ctrl+2)": "Widok Builder (Ctrl+2)",
  "Double-click: default size": "Dwuklik: domyślny rozmiar",
  "Double-click: default size · right double-click: maximize": "Dwuklik: domyślny rozmiar · dwuklik prawym: maksymalizuj",
  "Double-click: maximize": "Dwuklik: maksymalizuj",
  "Drag to move": "Przeciągnij, aby przesunąć",
  "Drag to resize · double-click for the default size": "Przeciągnij, aby zmienić rozmiar · dwuklik: rozmiar domyślny",
  "Empty canvas": "Puste płótno",
  "Language": "Język",
  "Last used view": "Ostatnio używany widok",
  "Nothing here yet": "Na razie nic tu nie ma",
  "Open on": "Otwieraj w",
  "Overview": "Przegląd",
  "Settings": "Ustawienia",
  "System ({0})": "System ({0})",
  "The view shown when AI Manager opens.": "Widok pokazywany po otwarciu AI Manager.",
  "Your AI tools will be listed here.": "Tutaj pojawią się Twoje narzędzia AI."
 },
 "pt": {
  "AI Manager restarts in the new language.": "O AI Manager reinicia no novo idioma.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Back": "Voltar",
  "Back (Backspace, Esc)": "Voltar (Backspace, Esc)",
  "Build something here.": "Construa algo aqui.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Double-click: default size": "Duplo clique: tamanho padrão",
  "Double-click: default size · right double-click: maximize": "Duplo clique: tamanho padrão · duplo clique direito: maximizar",
  "Double-click: maximize": "Duplo clique: maximizar",
  "Drag to move": "Arraste para mover",
  "Drag to resize · double-click for the default size": "Arraste para redimensionar · clique duplo para o tamanho padrão",
  "Empty canvas": "Tela vazia",
  "Language": "Idioma",
  "Last used view": "Última vista usada",
  "Nothing here yet": "Nada aqui ainda",
  "Open on": "Abrir em",
  "Overview": "Visão geral",
  "Settings": "Configurações",
  "System ({0})": "Sistema ({0})",
  "The view shown when AI Manager opens.": "A vista mostrada quando o AI Manager abre.",
  "Your AI tools will be listed here.": "As suas ferramentas de IA aparecerão aqui."
 }
}
