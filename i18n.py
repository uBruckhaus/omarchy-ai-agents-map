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
  "\u00041 active day": [
   "1 aktiver Tag",
   "{0} aktive Tage"
  ],
  "\u00041 model": [
   "1 Modell",
   "{0} Modelle"
  ],
  "\u00041 prompt": [
   "1 Prompt",
   "{0} Prompts"
  ],
  "\u00041 session": [
   "1 Sitzung",
   "{0} Sitzungen"
  ],
  "AI Manager restarts in the new language.": "AI Manager startet in der neuen Sprache neu.",
  "AI Manager view": "AI-Manager-Ansicht",
  "AI Manager view (Ctrl+1)": "AI-Manager-Ansicht (Strg+1)",
  "Agents": "Agenten",
  "Back": "Zurück",
  "Back (Backspace, Esc)": "Zurück (Rücktaste, Esc)",
  "Build something here.": "Hier entsteht etwas.",
  "Builder": "Builder",
  "Builder view": "Builder-Ansicht",
  "Builder view (Ctrl+2)": "Builder-Ansicht (Strg+2)",
  "Click for details": "Klicken für Details",
  "Command": "Befehl",
  "Default": "Standard",
  "Double-click: default size": "Doppelklick: Standardgröße",
  "Double-click: default size · right double-click: maximize": "Doppelklick: Standardgröße · Rechts-Doppelklick: maximieren",
  "Double-click: maximize": "Doppelklick: maximieren",
  "Drag to move": "Ziehen zum Verschieben",
  "Drag to resize · double-click for the default size": "Ziehen zum Ändern der Größe · Doppelklick für die Standardgröße",
  "Empty canvas": "Leere Arbeitsfläche",
  "In total": "Insgesamt",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Über Omarchys Menü installieren: Setup → Defaults → Agent.",
  "Installed": "Installiert",
  "Installed ({0})": "Installiert ({0})",
  "Language": "Sprache",
  "Last used": "Zuletzt benutzt",
  "Last used view": "Zuletzt benutzte Ansicht",
  "Limits": "Limits",
  "Model": "Modell",
  "Not installed": "Nicht installiert",
  "Not installed ({0})": "Nicht installiert ({0})",
  "Omarchy's default agent": "Omarchys Standard-Agent",
  "Open on": "Öffnen mit",
  "Plan": "Tarif",
  "Provider": "Anbieter",
  "Settings": "Einstellungen",
  "Status": "Status",
  "System ({0})": "System ({0})",
  "The model set in the agent's settings.": "Das in den Einstellungen des Agenten gesetzte Modell.",
  "The view shown when AI Manager opens.": "Die Ansicht, mit der AI Manager öffnet.",
  "Today": "Heute",
  "Updating usage…": "Nutzung wird aktualisiert…",
  "Usage from Omarchy, updated {0}": "Nutzung von Omarchy, Stand {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Dein Abo, wie Omarchys Nutzungsdaten es melden.",
  "as a package": "als Paket",
  "local": "lokal",
  "local · {0}": "lokal · {0}",
  "resets {0}": "zurückgesetzt {0}",
  "today {0}": "heute {0}",
  "tomorrow {0}": "morgen {0}",
  "with mise": "mit mise",
  "yesterday {0}": "gestern {0}",
  "your own install": "eigene Installation",
  "{0} %": "{0} %",
  "{0} tokens": "{0} Tokens"
 },
 "es": {
  "\u00041 active day": [
   "1 día activo",
   "{0} días activos"
  ],
  "\u00041 model": [
   "1 modelo",
   "{0} modelos"
  ],
  "\u00041 prompt": [
   "1 prompt",
   "{0} prompts"
  ],
  "\u00041 session": [
   "1 sesión",
   "{0} sesiones"
  ],
  "AI Manager restarts in the new language.": "AI Manager se reinicia en el nuevo idioma.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Agents": "Agentes",
  "Back": "Atrás",
  "Back (Backspace, Esc)": "Atrás (Retroceso, Esc)",
  "Build something here.": "Construye algo aquí.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Click for details": "Haz clic para ver detalles",
  "Command": "Comando",
  "Default": "Predeterminado",
  "Double-click: default size": "Doble clic: tamaño predeterminado",
  "Double-click: default size · right double-click: maximize": "Doble clic: tamaño predeterminado · doble clic derecho: maximizar",
  "Double-click: maximize": "Doble clic: maximizar",
  "Drag to move": "Arrastra para mover",
  "Drag to resize · double-click for the default size": "Arrastra para cambiar el tamaño · doble clic para el tamaño predeterminado",
  "Empty canvas": "Lienzo vacío",
  "In total": "En total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Instálalo desde el menú de Omarchy: Setup → Defaults → Agent.",
  "Installed": "Instalado",
  "Installed ({0})": "Instalados ({0})",
  "Language": "Idioma",
  "Last used": "Último uso",
  "Last used view": "Última vista usada",
  "Limits": "Límites",
  "Model": "Modelo",
  "Not installed": "No instalado",
  "Not installed ({0})": "No instalados ({0})",
  "Omarchy's default agent": "Agente predeterminado de Omarchy",
  "Open on": "Abrir en",
  "Plan": "Plan",
  "Provider": "Proveedor",
  "Settings": "Ajustes",
  "Status": "Estado",
  "System ({0})": "Sistema ({0})",
  "The model set in the agent's settings.": "El modelo configurado en los ajustes del agente.",
  "The view shown when AI Manager opens.": "La vista que muestra AI Manager al abrirse.",
  "Today": "Hoy",
  "Updating usage…": "Actualizando el uso…",
  "Usage from Omarchy, updated {0}": "Uso según Omarchy, actualizado {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Tu suscripción, según los datos de uso de Omarchy.",
  "as a package": "como paquete",
  "local": "local",
  "local · {0}": "local · {0}",
  "resets {0}": "se restablece {0}",
  "today {0}": "hoy {0}",
  "tomorrow {0}": "mañana {0}",
  "with mise": "con mise",
  "yesterday {0}": "ayer {0}",
  "your own install": "tu propia instalación",
  "{0} %": "{0} %",
  "{0} tokens": "{0} tokens"
 },
 "fr": {
  "\u00041 active day": [
   "{0} jour actif",
   "{0} jours actifs"
  ],
  "\u00041 model": [
   "{0} modèle",
   "{0} modèles"
  ],
  "\u00041 prompt": [
   "{0} prompt",
   "{0} prompts"
  ],
  "\u00041 session": [
   "{0} session",
   "{0} sessions"
  ],
  "AI Manager restarts in the new language.": "AI Manager redémarre dans la nouvelle langue.",
  "AI Manager view": "Vue AI Manager",
  "AI Manager view (Ctrl+1)": "Vue AI Manager (Ctrl+1)",
  "Agents": "Agents",
  "Back": "Retour",
  "Back (Backspace, Esc)": "Retour (Retour arrière, Échap)",
  "Build something here.": "Construisez quelque chose ici.",
  "Builder": "Builder",
  "Builder view": "Vue Builder",
  "Builder view (Ctrl+2)": "Vue Builder (Ctrl+2)",
  "Click for details": "Cliquer pour les détails",
  "Command": "Commande",
  "Default": "Par défaut",
  "Double-click: default size": "Double-clic : taille par défaut",
  "Double-click: default size · right double-click: maximize": "Double-clic : taille par défaut · double-clic droit : agrandir",
  "Double-click: maximize": "Double-clic : agrandir",
  "Drag to move": "Glisser pour déplacer",
  "Drag to resize · double-click for the default size": "Glisser pour redimensionner · double-clic pour la taille par défaut",
  "Empty canvas": "Canevas vide",
  "In total": "Au total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installez-le depuis le menu d'Omarchy : Setup → Defaults → Agent.",
  "Installed": "Installé",
  "Installed ({0})": "Installés ({0})",
  "Language": "Langue",
  "Last used": "Dernière utilisation",
  "Last used view": "Dernière vue utilisée",
  "Limits": "Limites",
  "Model": "Modèle",
  "Not installed": "Non installé",
  "Not installed ({0})": "Non installés ({0})",
  "Omarchy's default agent": "Agent par défaut d'Omarchy",
  "Open on": "Ouvrir sur",
  "Plan": "Forfait",
  "Provider": "Fournisseur",
  "Settings": "Réglages",
  "Status": "État",
  "System ({0})": "Système ({0})",
  "The model set in the agent's settings.": "Le modèle défini dans les réglages de l'agent.",
  "The view shown when AI Manager opens.": "La vue affichée à l'ouverture d'AI Manager.",
  "Today": "Aujourd'hui",
  "Updating usage…": "Mise à jour de l'utilisation…",
  "Usage from Omarchy, updated {0}": "Utilisation d'Omarchy, mise à jour {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Votre abonnement, selon les données d'utilisation d'Omarchy.",
  "as a package": "comme paquet",
  "local": "local",
  "local · {0}": "local · {0}",
  "resets {0}": "réinitialisé {0}",
  "today {0}": "aujourd'hui {0}",
  "tomorrow {0}": "demain {0}",
  "with mise": "avec mise",
  "yesterday {0}": "hier {0}",
  "your own install": "votre propre installation",
  "{0} %": "{0} %",
  "{0} tokens": "{0} jetons"
 },
 "it": {
  "\u00041 active day": [
   "1 giorno attivo",
   "{0} giorni attivi"
  ],
  "\u00041 model": [
   "1 modello",
   "{0} modelli"
  ],
  "\u00041 prompt": [
   "1 prompt",
   "{0} prompt"
  ],
  "\u00041 session": [
   "1 sessione",
   "{0} sessioni"
  ],
  "AI Manager restarts in the new language.": "AI Manager si riavvia nella nuova lingua.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Agents": "Agenti",
  "Back": "Indietro",
  "Back (Backspace, Esc)": "Indietro (Backspace, Esc)",
  "Build something here.": "Costruisci qualcosa qui.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Click for details": "Clic per i dettagli",
  "Command": "Comando",
  "Default": "Predefinito",
  "Double-click: default size": "Doppio clic: dimensione predefinita",
  "Double-click: default size · right double-click: maximize": "Doppio clic: dimensione predefinita · doppio clic destro: massimizza",
  "Double-click: maximize": "Doppio clic: massimizza",
  "Drag to move": "Trascina per spostare",
  "Drag to resize · double-click for the default size": "Trascina per ridimensionare · doppio clic per la dimensione predefinita",
  "Empty canvas": "Area vuota",
  "In total": "In totale",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installalo dal menu di Omarchy: Setup → Defaults → Agent.",
  "Installed": "Installato",
  "Installed ({0})": "Installati ({0})",
  "Language": "Lingua",
  "Last used": "Ultimo uso",
  "Last used view": "Ultima vista usata",
  "Limits": "Limiti",
  "Model": "Modello",
  "Not installed": "Non installato",
  "Not installed ({0})": "Non installati ({0})",
  "Omarchy's default agent": "Agente predefinito di Omarchy",
  "Open on": "Apri su",
  "Plan": "Piano",
  "Provider": "Provider",
  "Settings": "Impostazioni",
  "Status": "Stato",
  "System ({0})": "Sistema ({0})",
  "The model set in the agent's settings.": "Il modello impostato nelle impostazioni dell'agente.",
  "The view shown when AI Manager opens.": "La vista mostrata all'apertura di AI Manager.",
  "Today": "Oggi",
  "Updating usage…": "Aggiornamento dell'uso…",
  "Usage from Omarchy, updated {0}": "Uso da Omarchy, aggiornato {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Il tuo abbonamento, secondo i dati d'uso di Omarchy.",
  "as a package": "come pacchetto",
  "local": "locale",
  "local · {0}": "locale · {0}",
  "resets {0}": "si azzera {0}",
  "today {0}": "oggi {0}",
  "tomorrow {0}": "domani {0}",
  "with mise": "con mise",
  "yesterday {0}": "ieri {0}",
  "your own install": "installazione personale",
  "{0} %": "{0} %",
  "{0} tokens": "{0} token"
 },
 "nl": {
  "\u00041 active day": [
   "1 actieve dag",
   "{0} actieve dagen"
  ],
  "\u00041 model": [
   "1 model",
   "{0} modellen"
  ],
  "\u00041 prompt": [
   "1 prompt",
   "{0} prompts"
  ],
  "\u00041 session": [
   "1 sessie",
   "{0} sessies"
  ],
  "AI Manager restarts in the new language.": "AI Manager start opnieuw in de nieuwe taal.",
  "AI Manager view": "AI Manager-weergave",
  "AI Manager view (Ctrl+1)": "AI Manager-weergave (Ctrl+1)",
  "Agents": "Agents",
  "Back": "Terug",
  "Back (Backspace, Esc)": "Terug (Backspace, Esc)",
  "Build something here.": "Bouw hier iets.",
  "Builder": "Builder",
  "Builder view": "Builder-weergave",
  "Builder view (Ctrl+2)": "Builder-weergave (Ctrl+2)",
  "Click for details": "Klik voor details",
  "Command": "Opdracht",
  "Default": "Standaard",
  "Double-click: default size": "Dubbelklik: standaardgrootte",
  "Double-click: default size · right double-click: maximize": "Dubbelklik: standaardgrootte · rechts dubbelklikken: maximaliseren",
  "Double-click: maximize": "Dubbelklik: maximaliseren",
  "Drag to move": "Sleep om te verplaatsen",
  "Drag to resize · double-click for the default size": "Sleep om het formaat te wijzigen · dubbelklik voor de standaardgrootte",
  "Empty canvas": "Leeg canvas",
  "In total": "In totaal",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installeer het via het menu van Omarchy: Setup → Defaults → Agent.",
  "Installed": "Geïnstalleerd",
  "Installed ({0})": "Geïnstalleerd ({0})",
  "Language": "Taal",
  "Last used": "Laatst gebruikt",
  "Last used view": "Laatst gebruikte weergave",
  "Limits": "Limieten",
  "Model": "Model",
  "Not installed": "Niet geïnstalleerd",
  "Not installed ({0})": "Niet geïnstalleerd ({0})",
  "Omarchy's default agent": "Standaardagent van Omarchy",
  "Open on": "Openen op",
  "Plan": "Abonnement",
  "Provider": "Provider",
  "Settings": "Instellingen",
  "Status": "Status",
  "System ({0})": "Systeem ({0})",
  "The model set in the agent's settings.": "Het model uit de instellingen van de agent.",
  "The view shown when AI Manager opens.": "De weergave waarmee AI Manager opent.",
  "Today": "Vandaag",
  "Updating usage…": "Gebruik bijwerken…",
  "Usage from Omarchy, updated {0}": "Gebruik van Omarchy, bijgewerkt {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Je abonnement, volgens de gebruiksgegevens van Omarchy.",
  "as a package": "als pakket",
  "local": "lokaal",
  "local · {0}": "lokaal · {0}",
  "resets {0}": "reset {0}",
  "today {0}": "vandaag {0}",
  "tomorrow {0}": "morgen {0}",
  "with mise": "met mise",
  "yesterday {0}": "gisteren {0}",
  "your own install": "eigen installatie",
  "{0} %": "{0} %",
  "{0} tokens": "{0} tokens"
 },
 "pl": {
  "\u00041 active day": [
   "1 aktywny dzień",
   "{0} aktywne dni",
   "{0} aktywnych dni"
  ],
  "\u00041 model": [
   "1 model",
   "{0} modele",
   "{0} modeli"
  ],
  "\u00041 prompt": [
   "1 prompt",
   "{0} prompty",
   "{0} promptów"
  ],
  "\u00041 session": [
   "1 sesja",
   "{0} sesje",
   "{0} sesji"
  ],
  "AI Manager restarts in the new language.": "AI Manager uruchomi się ponownie w nowym języku.",
  "AI Manager view": "Widok AI Manager",
  "AI Manager view (Ctrl+1)": "Widok AI Manager (Ctrl+1)",
  "Agents": "Agenci",
  "Back": "Wstecz",
  "Back (Backspace, Esc)": "Wstecz (Backspace, Esc)",
  "Build something here.": "Zbuduj tu coś.",
  "Builder": "Builder",
  "Builder view": "Widok Builder",
  "Builder view (Ctrl+2)": "Widok Builder (Ctrl+2)",
  "Click for details": "Kliknij, aby zobaczyć szczegóły",
  "Command": "Polecenie",
  "Default": "Domyślny",
  "Double-click: default size": "Dwuklik: domyślny rozmiar",
  "Double-click: default size · right double-click: maximize": "Dwuklik: domyślny rozmiar · dwuklik prawym: maksymalizuj",
  "Double-click: maximize": "Dwuklik: maksymalizuj",
  "Drag to move": "Przeciągnij, aby przesunąć",
  "Drag to resize · double-click for the default size": "Przeciągnij, aby zmienić rozmiar · dwuklik: rozmiar domyślny",
  "Empty canvas": "Puste płótno",
  "In total": "Łącznie",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Zainstaluj z menu Omarchy: Setup → Defaults → Agent.",
  "Installed": "Zainstalowany",
  "Installed ({0})": "Zainstalowane ({0})",
  "Language": "Język",
  "Last used": "Ostatnio używany",
  "Last used view": "Ostatnio używany widok",
  "Limits": "Limity",
  "Model": "Model",
  "Not installed": "Niezainstalowany",
  "Not installed ({0})": "Niezainstalowane ({0})",
  "Omarchy's default agent": "Domyślny agent Omarchy",
  "Open on": "Otwieraj w",
  "Plan": "Plan",
  "Provider": "Dostawca",
  "Settings": "Ustawienia",
  "Status": "Stan",
  "System ({0})": "System ({0})",
  "The model set in the agent's settings.": "Model ustawiony w ustawieniach agenta.",
  "The view shown when AI Manager opens.": "Widok pokazywany po otwarciu AI Manager.",
  "Today": "Dziś",
  "Updating usage…": "Aktualizowanie użycia…",
  "Usage from Omarchy, updated {0}": "Użycie z Omarchy, zaktualizowano {0}",
  "Your subscription, as Omarchy's usage data reports it.": "Twoja subskrypcja według danych użycia Omarchy.",
  "as a package": "jako pakiet",
  "local": "lokalnie",
  "local · {0}": "lokalnie · {0}",
  "resets {0}": "reset {0}",
  "today {0}": "dziś {0}",
  "tomorrow {0}": "jutro {0}",
  "with mise": "przez mise",
  "yesterday {0}": "wczoraj {0}",
  "your own install": "własna instalacja",
  "{0} %": "{0} %",
  "{0} tokens": "{0} tokenów"
 },
 "pt": {
  "\u00041 active day": [
   "{0} dia ativo",
   "{0} dias ativos"
  ],
  "\u00041 model": [
   "{0} modelo",
   "{0} modelos"
  ],
  "\u00041 prompt": [
   "{0} prompt",
   "{0} prompts"
  ],
  "\u00041 session": [
   "{0} sessão",
   "{0} sessões"
  ],
  "AI Manager restarts in the new language.": "O AI Manager reinicia no novo idioma.",
  "AI Manager view": "Vista AI Manager",
  "AI Manager view (Ctrl+1)": "Vista AI Manager (Ctrl+1)",
  "Agents": "Agentes",
  "Back": "Voltar",
  "Back (Backspace, Esc)": "Voltar (Backspace, Esc)",
  "Build something here.": "Construa algo aqui.",
  "Builder": "Builder",
  "Builder view": "Vista Builder",
  "Builder view (Ctrl+2)": "Vista Builder (Ctrl+2)",
  "Click for details": "Clique para ver detalhes",
  "Command": "Comando",
  "Default": "Padrão",
  "Double-click: default size": "Duplo clique: tamanho padrão",
  "Double-click: default size · right double-click: maximize": "Duplo clique: tamanho padrão · duplo clique direito: maximizar",
  "Double-click: maximize": "Duplo clique: maximizar",
  "Drag to move": "Arraste para mover",
  "Drag to resize · double-click for the default size": "Arraste para redimensionar · clique duplo para o tamanho padrão",
  "Empty canvas": "Tela vazia",
  "In total": "No total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Instale-o pelo menu do Omarchy: Setup → Defaults → Agent.",
  "Installed": "Instalado",
  "Installed ({0})": "Instalados ({0})",
  "Language": "Idioma",
  "Last used": "Último uso",
  "Last used view": "Última vista usada",
  "Limits": "Limites",
  "Model": "Modelo",
  "Not installed": "Não instalado",
  "Not installed ({0})": "Não instalados ({0})",
  "Omarchy's default agent": "Agente padrão do Omarchy",
  "Open on": "Abrir em",
  "Plan": "Plano",
  "Provider": "Fornecedor",
  "Settings": "Configurações",
  "Status": "Estado",
  "System ({0})": "Sistema ({0})",
  "The model set in the agent's settings.": "O modelo definido nas configurações do agente.",
  "The view shown when AI Manager opens.": "A vista mostrada quando o AI Manager abre.",
  "Today": "Hoje",
  "Updating usage…": "A atualizar o uso…",
  "Usage from Omarchy, updated {0}": "Uso do Omarchy, atualizado {0}",
  "Your subscription, as Omarchy's usage data reports it.": "A sua assinatura, segundo os dados de uso do Omarchy.",
  "as a package": "como pacote",
  "local": "local",
  "local · {0}": "local · {0}",
  "resets {0}": "reinicia {0}",
  "today {0}": "hoje {0}",
  "tomorrow {0}": "amanhã {0}",
  "with mise": "com mise",
  "yesterday {0}": "ontem {0}",
  "your own install": "instalação própria",
  "{0} %": "{0} %",
  "{0} tokens": "{0} tokens"
 }
}
