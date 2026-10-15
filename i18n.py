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
  "\u00041 skill": [
   "1 Skill",
   "{0} Skills"
  ],
  "+{0} more": "+{0} weitere",
  "AI Agents Map restarts in the new language.": "AI Agents Map startet in der neuen Sprache neu.",
  "AI setup": "KI-Ausstattung",
  "Address": "Adresse",
  "Agents": "Agenten",
  "Agents ({0})": "Agenten ({0})",
  "Agents with a provider pointing at this runtime.": "Agenten, deren Anbieter auf diese Laufzeit zeigt.",
  "Back": "Zurück",
  "Back (Backspace, Esc)": "Zurück (Rücktaste, Esc)",
  "Clear selection": "Auswahl aufheben",
  "Clear the selection (Esc)": "Auswahl aufheben (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Ein Feld anklicken zeigt nur seine Verbindungen; Umschalt+Klick wählt mehrere.",
  "Click for details": "Klicken für Details",
  "Command": "Befehl",
  "Default": "Standard",
  "Double-click: default size": "Doppelklick: Standardgröße",
  "Double-click: default size · right double-click: maximize": "Doppelklick: Standardgröße · Rechts-Doppelklick: maximieren",
  "Double-click: maximize": "Doppelklick: maximieren",
  "Drag to move": "Ziehen zum Verschieben",
  "Drag to resize · double-click for the default size": "Ziehen zum Ändern der Größe · Doppelklick für die Standardgröße",
  "Folder": "Ordner",
  "In total": "Insgesamt",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Über Omarchys Menü installieren: Setup → Defaults → Agent.",
  "Installed": "Installiert",
  "LLM providers": "LLM-Anbieter",
  "Language": "Sprache",
  "Last used": "Zuletzt benutzt",
  "Last used view": "Zuletzt benutzte Ansicht",
  "Limits": "Limits",
  "Local LLMs ({0})": "Lokale LLMs ({0})",
  "MCP servers": "MCP-Server",
  "MCP servers ({0})": "MCP-Server ({0})",
  "MCP servers · skills": "MCP-Server · Skills",
  "Map": "Karte",
  "Map (Ctrl+2)": "Karte (Strg+2)",
  "Model": "Modell",
  "Models": "Modelle",
  "Models folder": "Modellordner",
  "No agent has an MCP server set up.": "Kein Agent hat einen MCP-Server eingerichtet.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Keine lokale LLM-Laufzeit installiert (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Keine Skills für die installierten Agenten gefunden.",
  "Not installed": "Nicht installiert",
  "Not installed ({0})": "Nicht installiert ({0})",
  "Omarchy's default agent": "Omarchys Standard-Agent",
  "Open on": "Öffnen mit",
  "Overview": "Übersicht",
  "Overview (Ctrl+1)": "Übersicht (Strg+1)",
  "Plan": "Tarif",
  "Provider": "Anbieter",
  "Runs": "Startet",
  "Selected: {0} · hold Shift and click to add or remove one": "Ausgewählt: {0} · Umschalt halten und klicken fügt hinzu oder entfernt",
  "Service": "Dienst",
  "Settings": "Einstellungen",
  "Skills": "Skills",
  "Skills ({0})": "Skills ({0})",
  "Status": "Status",
  "System ({0})": "System ({0})",
  "The built-in tools the agent may use, as its settings list them.": "Die eingebauten Werkzeuge, die der Agent laut seinen Einstellungen nutzen darf.",
  "The model set in the agent's settings.": "Das in den Einstellungen des Agenten gesetzte Modell.",
  "The user service that starts it (systemctl --user).": "Der Benutzerdienst, der sie startet (systemctl --user).",
  "The view shown when AI Agents Map opens.": "Die Ansicht, mit der AI Agents Map öffnet.",
  "Today": "Heute",
  "Tools": "Werkzeuge",
  "Type": "Art",
  "Updating usage…": "Nutzung wird aktualisiert…",
  "Usage from Omarchy, updated {0}": "Nutzung von Omarchy, Stand {0}",
  "Used by": "Genutzt von",
  "Your subscription, as Omarchy's usage data reports it.": "Dein Abo, wie Omarchys Nutzungsdaten es melden.",
  "as a package": "als Paket",
  "local": "lokal",
  "local · {0}": "lokal · {0}",
  "remote server": "entfernter Server",
  "resets {0}": "zurückgesetzt {0}",
  "running": "läuft",
  "runs a program": "startet ein Programm",
  "stopped": "gestoppt",
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
  "\u00041 skill": [
   "1 skill",
   "{0} skills"
  ],
  "+{0} more": "+{0} más",
  "AI Agents Map restarts in the new language.": "AI Agents Map se reinicia en el nuevo idioma.",
  "AI setup": "Configuración de IA",
  "Address": "Dirección",
  "Agents": "Agentes",
  "Agents ({0})": "Agentes ({0})",
  "Agents with a provider pointing at this runtime.": "Agentes con un proveedor que apunta a este motor.",
  "Back": "Atrás",
  "Back (Backspace, Esc)": "Atrás (Retroceso, Esc)",
  "Clear selection": "Quitar selección",
  "Clear the selection (Esc)": "Quitar la selección (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Haz clic en una caja para ver solo sus conexiones; Mayús+clic selecciona varias.",
  "Click for details": "Haz clic para ver detalles",
  "Command": "Comando",
  "Default": "Predeterminado",
  "Double-click: default size": "Doble clic: tamaño predeterminado",
  "Double-click: default size · right double-click: maximize": "Doble clic: tamaño predeterminado · doble clic derecho: maximizar",
  "Double-click: maximize": "Doble clic: maximizar",
  "Drag to move": "Arrastra para mover",
  "Drag to resize · double-click for the default size": "Arrastra para cambiar el tamaño · doble clic para el tamaño predeterminado",
  "Folder": "Carpeta",
  "In total": "En total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Instálalo desde el menú de Omarchy: Setup → Defaults → Agent.",
  "Installed": "Instalado",
  "LLM providers": "Proveedores de LLM",
  "Language": "Idioma",
  "Last used": "Último uso",
  "Last used view": "Última vista usada",
  "Limits": "Límites",
  "Local LLMs ({0})": "LLM locales ({0})",
  "MCP servers": "Servidores MCP",
  "MCP servers ({0})": "Servidores MCP ({0})",
  "MCP servers · skills": "Servidores MCP · skills",
  "Map": "Mapa",
  "Map (Ctrl+2)": "Mapa (Ctrl+2)",
  "Model": "Modelo",
  "Models": "Modelos",
  "Models folder": "Carpeta de modelos",
  "No agent has an MCP server set up.": "Ningún agente tiene un servidor MCP configurado.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "No hay ningún motor LLM local instalado (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "No se encontraron skills para los agentes instalados.",
  "Not installed": "No instalado",
  "Not installed ({0})": "No instalados ({0})",
  "Omarchy's default agent": "Agente predeterminado de Omarchy",
  "Open on": "Abrir en",
  "Overview": "Resumen",
  "Overview (Ctrl+1)": "Resumen (Ctrl+1)",
  "Plan": "Plan",
  "Provider": "Proveedor",
  "Runs": "Ejecuta",
  "Selected: {0} · hold Shift and click to add or remove one": "Seleccionado: {0} · mantén Mayús y haz clic para añadir o quitar",
  "Service": "Servicio",
  "Settings": "Ajustes",
  "Skills": "Skills",
  "Skills ({0})": "Skills ({0})",
  "Status": "Estado",
  "System ({0})": "Sistema ({0})",
  "The built-in tools the agent may use, as its settings list them.": "Las herramientas integradas que el agente puede usar, según sus ajustes.",
  "The model set in the agent's settings.": "El modelo configurado en los ajustes del agente.",
  "The user service that starts it (systemctl --user).": "El servicio de usuario que lo inicia (systemctl --user).",
  "The view shown when AI Agents Map opens.": "La vista que muestra AI Agents Map al abrirse.",
  "Today": "Hoy",
  "Tools": "Herramientas",
  "Type": "Tipo",
  "Updating usage…": "Actualizando el uso…",
  "Usage from Omarchy, updated {0}": "Uso según Omarchy, actualizado {0}",
  "Used by": "Usado por",
  "Your subscription, as Omarchy's usage data reports it.": "Tu suscripción, según los datos de uso de Omarchy.",
  "as a package": "como paquete",
  "local": "local",
  "local · {0}": "local · {0}",
  "remote server": "servidor remoto",
  "resets {0}": "se restablece {0}",
  "running": "en marcha",
  "runs a program": "ejecuta un programa",
  "stopped": "detenido",
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
  "\u00041 skill": [
   "{0} skill",
   "{0} skills"
  ],
  "+{0} more": "+{0} de plus",
  "AI Agents Map restarts in the new language.": "AI Agents Map redémarre dans la nouvelle langue.",
  "AI setup": "Configuration IA",
  "Address": "Adresse",
  "Agents": "Agents",
  "Agents ({0})": "Agents ({0})",
  "Agents with a provider pointing at this runtime.": "Agents dont un fournisseur pointe vers ce moteur.",
  "Back": "Retour",
  "Back (Backspace, Esc)": "Retour (Retour arrière, Échap)",
  "Clear selection": "Effacer la sélection",
  "Clear the selection (Esc)": "Effacer la sélection (Échap)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Cliquez sur une case pour ne voir que ses liens ; Maj+clic en sélectionne plusieurs.",
  "Click for details": "Cliquer pour les détails",
  "Command": "Commande",
  "Default": "Par défaut",
  "Double-click: default size": "Double-clic : taille par défaut",
  "Double-click: default size · right double-click: maximize": "Double-clic : taille par défaut · double-clic droit : agrandir",
  "Double-click: maximize": "Double-clic : agrandir",
  "Drag to move": "Glisser pour déplacer",
  "Drag to resize · double-click for the default size": "Glisser pour redimensionner · double-clic pour la taille par défaut",
  "Folder": "Dossier",
  "In total": "Au total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installez-le depuis le menu d'Omarchy : Setup → Defaults → Agent.",
  "Installed": "Installé",
  "LLM providers": "Fournisseurs de LLM",
  "Language": "Langue",
  "Last used": "Dernière utilisation",
  "Last used view": "Dernière vue utilisée",
  "Limits": "Limites",
  "Local LLMs ({0})": "LLM locaux ({0})",
  "MCP servers": "Serveurs MCP",
  "MCP servers ({0})": "Serveurs MCP ({0})",
  "MCP servers · skills": "Serveurs MCP · skills",
  "Map": "Carte",
  "Map (Ctrl+2)": "Carte (Ctrl+2)",
  "Model": "Modèle",
  "Models": "Modèles",
  "Models folder": "Dossier des modèles",
  "No agent has an MCP server set up.": "Aucun agent n'a de serveur MCP configuré.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Aucun moteur LLM local installé (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Aucun skill trouvé pour les agents installés.",
  "Not installed": "Non installé",
  "Not installed ({0})": "Non installés ({0})",
  "Omarchy's default agent": "Agent par défaut d'Omarchy",
  "Open on": "Ouvrir sur",
  "Overview": "Vue d'ensemble",
  "Overview (Ctrl+1)": "Vue d'ensemble (Ctrl+1)",
  "Plan": "Forfait",
  "Provider": "Fournisseur",
  "Runs": "Lance",
  "Selected: {0} · hold Shift and click to add or remove one": "Sélection : {0} · maintenez Maj et cliquez pour ajouter ou retirer",
  "Service": "Service",
  "Settings": "Réglages",
  "Skills": "Skills",
  "Skills ({0})": "Skills ({0})",
  "Status": "État",
  "System ({0})": "Système ({0})",
  "The built-in tools the agent may use, as its settings list them.": "Les outils intégrés que l'agent peut utiliser, d'après ses réglages.",
  "The model set in the agent's settings.": "Le modèle défini dans les réglages de l'agent.",
  "The user service that starts it (systemctl --user).": "Le service utilisateur qui le démarre (systemctl --user).",
  "The view shown when AI Agents Map opens.": "La vue affichée à l'ouverture de AI Agents Map.",
  "Today": "Aujourd'hui",
  "Tools": "Outils",
  "Type": "Type",
  "Updating usage…": "Mise à jour de l'utilisation…",
  "Usage from Omarchy, updated {0}": "Utilisation d'Omarchy, mise à jour {0}",
  "Used by": "Utilisé par",
  "Your subscription, as Omarchy's usage data reports it.": "Votre abonnement, selon les données d'utilisation d'Omarchy.",
  "as a package": "comme paquet",
  "local": "local",
  "local · {0}": "local · {0}",
  "remote server": "serveur distant",
  "resets {0}": "réinitialisé {0}",
  "running": "en marche",
  "runs a program": "lance un programme",
  "stopped": "arrêté",
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
  "\u00041 skill": [
   "1 skill",
   "{0} skill"
  ],
  "+{0} more": "+{0} altri",
  "AI Agents Map restarts in the new language.": "AI Agents Map si riavvia nella nuova lingua.",
  "AI setup": "Configurazione IA",
  "Address": "Indirizzo",
  "Agents": "Agenti",
  "Agents ({0})": "Agenti ({0})",
  "Agents with a provider pointing at this runtime.": "Agenti con un provider che punta a questo motore.",
  "Back": "Indietro",
  "Back (Backspace, Esc)": "Indietro (Backspace, Esc)",
  "Clear selection": "Annulla selezione",
  "Clear the selection (Esc)": "Annulla la selezione (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Fai clic su un riquadro per vedere solo i suoi collegamenti; Maiusc+clic ne seleziona più di uno.",
  "Click for details": "Clic per i dettagli",
  "Command": "Comando",
  "Default": "Predefinito",
  "Double-click: default size": "Doppio clic: dimensione predefinita",
  "Double-click: default size · right double-click: maximize": "Doppio clic: dimensione predefinita · doppio clic destro: massimizza",
  "Double-click: maximize": "Doppio clic: massimizza",
  "Drag to move": "Trascina per spostare",
  "Drag to resize · double-click for the default size": "Trascina per ridimensionare · doppio clic per la dimensione predefinita",
  "Folder": "Cartella",
  "In total": "In totale",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installalo dal menu di Omarchy: Setup → Defaults → Agent.",
  "Installed": "Installato",
  "LLM providers": "Provider LLM",
  "Language": "Lingua",
  "Last used": "Ultimo uso",
  "Last used view": "Ultima vista usata",
  "Limits": "Limiti",
  "Local LLMs ({0})": "LLM locali ({0})",
  "MCP servers": "Server MCP",
  "MCP servers ({0})": "Server MCP ({0})",
  "MCP servers · skills": "Server MCP · skill",
  "Map": "Mappa",
  "Map (Ctrl+2)": "Mappa (Ctrl+2)",
  "Model": "Modello",
  "Models": "Modelli",
  "Models folder": "Cartella dei modelli",
  "No agent has an MCP server set up.": "Nessun agente ha un server MCP configurato.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Nessun motore LLM locale installato (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Nessuna skill trovata per gli agenti installati.",
  "Not installed": "Non installato",
  "Not installed ({0})": "Non installati ({0})",
  "Omarchy's default agent": "Agente predefinito di Omarchy",
  "Open on": "Apri su",
  "Overview": "Panoramica",
  "Overview (Ctrl+1)": "Panoramica (Ctrl+1)",
  "Plan": "Piano",
  "Provider": "Provider",
  "Runs": "Avvia",
  "Selected: {0} · hold Shift and click to add or remove one": "Selezionati: {0} · tieni premuto Maiusc e fai clic per aggiungere o togliere",
  "Service": "Servizio",
  "Settings": "Impostazioni",
  "Skills": "Skill",
  "Skills ({0})": "Skill ({0})",
  "Status": "Stato",
  "System ({0})": "Sistema ({0})",
  "The built-in tools the agent may use, as its settings list them.": "Gli strumenti integrati che l'agente può usare, secondo le sue impostazioni.",
  "The model set in the agent's settings.": "Il modello impostato nelle impostazioni dell'agente.",
  "The user service that starts it (systemctl --user).": "Il servizio utente che lo avvia (systemctl --user).",
  "The view shown when AI Agents Map opens.": "La vista mostrata all'apertura di AI Agents Map.",
  "Today": "Oggi",
  "Tools": "Strumenti",
  "Type": "Tipo",
  "Updating usage…": "Aggiornamento dell'uso…",
  "Usage from Omarchy, updated {0}": "Uso da Omarchy, aggiornato {0}",
  "Used by": "Usato da",
  "Your subscription, as Omarchy's usage data reports it.": "Il tuo abbonamento, secondo i dati d'uso di Omarchy.",
  "as a package": "come pacchetto",
  "local": "locale",
  "local · {0}": "locale · {0}",
  "remote server": "server remoto",
  "resets {0}": "si azzera {0}",
  "running": "in esecuzione",
  "runs a program": "avvia un programma",
  "stopped": "fermo",
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
  "\u00041 skill": [
   "1 skill",
   "{0} skills"
  ],
  "+{0} more": "+{0} meer",
  "AI Agents Map restarts in the new language.": "AI Agents Map start opnieuw in de nieuwe taal.",
  "AI setup": "AI-opzet",
  "Address": "Adres",
  "Agents": "Agents",
  "Agents ({0})": "Agents ({0})",
  "Agents with a provider pointing at this runtime.": "Agents met een provider die naar deze runtime wijst.",
  "Back": "Terug",
  "Back (Backspace, Esc)": "Terug (Backspace, Esc)",
  "Clear selection": "Selectie wissen",
  "Clear the selection (Esc)": "Selectie wissen (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Klik op een vak om alleen zijn verbindingen te zien; Shift+klik selecteert er meer.",
  "Click for details": "Klik voor details",
  "Command": "Opdracht",
  "Default": "Standaard",
  "Double-click: default size": "Dubbelklik: standaardgrootte",
  "Double-click: default size · right double-click: maximize": "Dubbelklik: standaardgrootte · rechts dubbelklikken: maximaliseren",
  "Double-click: maximize": "Dubbelklik: maximaliseren",
  "Drag to move": "Sleep om te verplaatsen",
  "Drag to resize · double-click for the default size": "Sleep om het formaat te wijzigen · dubbelklik voor de standaardgrootte",
  "Folder": "Map",
  "In total": "In totaal",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Installeer het via het menu van Omarchy: Setup → Defaults → Agent.",
  "Installed": "Geïnstalleerd",
  "LLM providers": "LLM-providers",
  "Language": "Taal",
  "Last used": "Laatst gebruikt",
  "Last used view": "Laatst gebruikte weergave",
  "Limits": "Limieten",
  "Local LLMs ({0})": "Lokale LLM's ({0})",
  "MCP servers": "MCP-servers",
  "MCP servers ({0})": "MCP-servers ({0})",
  "MCP servers · skills": "MCP-servers · skills",
  "Map": "Kaart",
  "Map (Ctrl+2)": "Kaart (Ctrl+2)",
  "Model": "Model",
  "Models": "Modellen",
  "Models folder": "Modellenmap",
  "No agent has an MCP server set up.": "Geen enkele agent heeft een MCP-server ingesteld.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Geen lokale LLM-runtime geïnstalleerd (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Geen skills gevonden voor de geïnstalleerde agents.",
  "Not installed": "Niet geïnstalleerd",
  "Not installed ({0})": "Niet geïnstalleerd ({0})",
  "Omarchy's default agent": "Standaardagent van Omarchy",
  "Open on": "Openen op",
  "Overview": "Overzicht",
  "Overview (Ctrl+1)": "Overzicht (Ctrl+1)",
  "Plan": "Abonnement",
  "Provider": "Provider",
  "Runs": "Start",
  "Selected: {0} · hold Shift and click to add or remove one": "Geselecteerd: {0} · houd Shift ingedrukt en klik om toe te voegen of weg te halen",
  "Service": "Dienst",
  "Settings": "Instellingen",
  "Skills": "Skills",
  "Skills ({0})": "Skills ({0})",
  "Status": "Status",
  "System ({0})": "Systeem ({0})",
  "The built-in tools the agent may use, as its settings list them.": "De ingebouwde tools die de agent volgens zijn instellingen mag gebruiken.",
  "The model set in the agent's settings.": "Het model uit de instellingen van de agent.",
  "The user service that starts it (systemctl --user).": "De gebruikersdienst die hem start (systemctl --user).",
  "The view shown when AI Agents Map opens.": "De weergave waarmee AI Agents Map opent.",
  "Today": "Vandaag",
  "Tools": "Tools",
  "Type": "Soort",
  "Updating usage…": "Gebruik bijwerken…",
  "Usage from Omarchy, updated {0}": "Gebruik van Omarchy, bijgewerkt {0}",
  "Used by": "Gebruikt door",
  "Your subscription, as Omarchy's usage data reports it.": "Je abonnement, volgens de gebruiksgegevens van Omarchy.",
  "as a package": "als pakket",
  "local": "lokaal",
  "local · {0}": "lokaal · {0}",
  "remote server": "externe server",
  "resets {0}": "reset {0}",
  "running": "actief",
  "runs a program": "start een programma",
  "stopped": "gestopt",
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
  "\u00041 skill": [
   "1 skill",
   "{0} skille",
   "{0} skilli"
  ],
  "+{0} more": "+{0} więcej",
  "AI Agents Map restarts in the new language.": "AI Agents Map uruchomi się ponownie w nowym języku.",
  "AI setup": "Konfiguracja AI",
  "Address": "Adres",
  "Agents": "Agenci",
  "Agents ({0})": "Agenci ({0})",
  "Agents with a provider pointing at this runtime.": "Agenci, których dostawca wskazuje na to środowisko.",
  "Back": "Wstecz",
  "Back (Backspace, Esc)": "Wstecz (Backspace, Esc)",
  "Clear selection": "Wyczyść zaznaczenie",
  "Clear the selection (Esc)": "Wyczyść zaznaczenie (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Kliknij pole, aby zobaczyć tylko jego połączenia; Shift+klik zaznacza więcej.",
  "Click for details": "Kliknij, aby zobaczyć szczegóły",
  "Command": "Polecenie",
  "Default": "Domyślny",
  "Double-click: default size": "Dwuklik: domyślny rozmiar",
  "Double-click: default size · right double-click: maximize": "Dwuklik: domyślny rozmiar · dwuklik prawym: maksymalizuj",
  "Double-click: maximize": "Dwuklik: maksymalizuj",
  "Drag to move": "Przeciągnij, aby przesunąć",
  "Drag to resize · double-click for the default size": "Przeciągnij, aby zmienić rozmiar · dwuklik: rozmiar domyślny",
  "Folder": "Folder",
  "In total": "Łącznie",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Zainstaluj z menu Omarchy: Setup → Defaults → Agent.",
  "Installed": "Zainstalowany",
  "LLM providers": "Dostawcy LLM",
  "Language": "Język",
  "Last used": "Ostatnio używany",
  "Last used view": "Ostatnio używany widok",
  "Limits": "Limity",
  "Local LLMs ({0})": "Lokalne LLM ({0})",
  "MCP servers": "Serwery MCP",
  "MCP servers ({0})": "Serwery MCP ({0})",
  "MCP servers · skills": "Serwery MCP · skille",
  "Map": "Mapa",
  "Map (Ctrl+2)": "Mapa (Ctrl+2)",
  "Model": "Model",
  "Models": "Modele",
  "Models folder": "Folder modeli",
  "No agent has an MCP server set up.": "Żaden agent nie ma skonfigurowanego serwera MCP.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Brak lokalnego środowiska LLM (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Nie znaleziono skilli dla zainstalowanych agentów.",
  "Not installed": "Niezainstalowany",
  "Not installed ({0})": "Niezainstalowane ({0})",
  "Omarchy's default agent": "Domyślny agent Omarchy",
  "Open on": "Otwieraj w",
  "Overview": "Przegląd",
  "Overview (Ctrl+1)": "Przegląd (Ctrl+1)",
  "Plan": "Plan",
  "Provider": "Dostawca",
  "Runs": "Uruchamia",
  "Selected: {0} · hold Shift and click to add or remove one": "Zaznaczone: {0} · przytrzymaj Shift i kliknij, aby dodać lub usunąć",
  "Service": "Usługa",
  "Settings": "Ustawienia",
  "Skills": "Skille",
  "Skills ({0})": "Skille ({0})",
  "Status": "Stan",
  "System ({0})": "System ({0})",
  "The built-in tools the agent may use, as its settings list them.": "Wbudowane narzędzia, których agent może używać według swoich ustawień.",
  "The model set in the agent's settings.": "Model ustawiony w ustawieniach agenta.",
  "The user service that starts it (systemctl --user).": "Usługa użytkownika, która je uruchamia (systemctl --user).",
  "The view shown when AI Agents Map opens.": "Widok pokazywany po otwarciu AI Agents Map.",
  "Today": "Dziś",
  "Tools": "Narzędzia",
  "Type": "Rodzaj",
  "Updating usage…": "Aktualizowanie użycia…",
  "Usage from Omarchy, updated {0}": "Użycie z Omarchy, zaktualizowano {0}",
  "Used by": "Używane przez",
  "Your subscription, as Omarchy's usage data reports it.": "Twoja subskrypcja według danych użycia Omarchy.",
  "as a package": "jako pakiet",
  "local": "lokalnie",
  "local · {0}": "lokalnie · {0}",
  "remote server": "serwer zdalny",
  "resets {0}": "reset {0}",
  "running": "działa",
  "runs a program": "uruchamia program",
  "stopped": "zatrzymane",
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
  "\u00041 skill": [
   "{0} skill",
   "{0} skills"
  ],
  "+{0} more": "+{0} mais",
  "AI Agents Map restarts in the new language.": "O AI Agents Map reinicia no novo idioma.",
  "AI setup": "Configuração de IA",
  "Address": "Endereço",
  "Agents": "Agentes",
  "Agents ({0})": "Agentes ({0})",
  "Agents with a provider pointing at this runtime.": "Agentes com um fornecedor que aponta para este motor.",
  "Back": "Voltar",
  "Back (Backspace, Esc)": "Voltar (Backspace, Esc)",
  "Clear selection": "Limpar seleção",
  "Clear the selection (Esc)": "Limpar a seleção (Esc)",
  "Click a box to see only its connections; Shift+click selects more than one.": "Clique numa caixa para ver só as suas ligações; Shift+clique seleciona várias.",
  "Click for details": "Clique para ver detalhes",
  "Command": "Comando",
  "Default": "Padrão",
  "Double-click: default size": "Duplo clique: tamanho padrão",
  "Double-click: default size · right double-click: maximize": "Duplo clique: tamanho padrão · duplo clique direito: maximizar",
  "Double-click: maximize": "Duplo clique: maximizar",
  "Drag to move": "Arraste para mover",
  "Drag to resize · double-click for the default size": "Arraste para redimensionar · clique duplo para o tamanho padrão",
  "Folder": "Pasta",
  "In total": "No total",
  "Install it from Omarchy's menu: Setup → Defaults → Agent.": "Instale-o pelo menu do Omarchy: Setup → Defaults → Agent.",
  "Installed": "Instalado",
  "LLM providers": "Fornecedores de LLM",
  "Language": "Idioma",
  "Last used": "Último uso",
  "Last used view": "Última vista usada",
  "Limits": "Limites",
  "Local LLMs ({0})": "LLMs locais ({0})",
  "MCP servers": "Servidores MCP",
  "MCP servers ({0})": "Servidores MCP ({0})",
  "MCP servers · skills": "Servidores MCP · skills",
  "Map": "Mapa",
  "Map (Ctrl+2)": "Mapa (Ctrl+2)",
  "Model": "Modelo",
  "Models": "Modelos",
  "Models folder": "Pasta dos modelos",
  "No agent has an MCP server set up.": "Nenhum agente tem um servidor MCP configurado.",
  "No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM).": "Nenhum motor LLM local instalado (llama.cpp, Ollama, LM Studio, vLLM).",
  "No skills found for the installed agents.": "Nenhuma skill encontrada para os agentes instalados.",
  "Not installed": "Não instalado",
  "Not installed ({0})": "Não instalados ({0})",
  "Omarchy's default agent": "Agente padrão do Omarchy",
  "Open on": "Abrir em",
  "Overview": "Visão geral",
  "Overview (Ctrl+1)": "Visão geral (Ctrl+1)",
  "Plan": "Plano",
  "Provider": "Fornecedor",
  "Runs": "Executa",
  "Selected: {0} · hold Shift and click to add or remove one": "Selecionado: {0} · mantenha Shift e clique para adicionar ou remover",
  "Service": "Serviço",
  "Settings": "Configurações",
  "Skills": "Skills",
  "Skills ({0})": "Skills ({0})",
  "Status": "Estado",
  "System ({0})": "Sistema ({0})",
  "The built-in tools the agent may use, as its settings list them.": "As ferramentas integradas que o agente pode usar, segundo as suas configurações.",
  "The model set in the agent's settings.": "O modelo definido nas configurações do agente.",
  "The user service that starts it (systemctl --user).": "O serviço de utilizador que o inicia (systemctl --user).",
  "The view shown when AI Agents Map opens.": "A vista mostrada quando o AI Agents Map abre.",
  "Today": "Hoje",
  "Tools": "Ferramentas",
  "Type": "Tipo",
  "Updating usage…": "A atualizar o uso…",
  "Usage from Omarchy, updated {0}": "Uso do Omarchy, atualizado {0}",
  "Used by": "Usado por",
  "Your subscription, as Omarchy's usage data reports it.": "A sua assinatura, segundo os dados de uso do Omarchy.",
  "as a package": "como pacote",
  "local": "local",
  "local · {0}": "local · {0}",
  "remote server": "servidor remoto",
  "resets {0}": "reinicia {0}",
  "running": "em execução",
  "runs a program": "executa um programa",
  "stopped": "parado",
  "today {0}": "hoje {0}",
  "tomorrow {0}": "amanhã {0}",
  "with mise": "com mise",
  "yesterday {0}": "ontem {0}",
  "your own install": "instalação própria",
  "{0} %": "{0} %",
  "{0} tokens": "{0} tokens"
 }
}
