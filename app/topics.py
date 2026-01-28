import random

DAILY_PHRASES_DEEP = [
    "Non è la verità che fa male: è il tempo che ci metti a riconoscerla.",
    "La cosa che temi di perdere è spesso ciò che ti sta già lasciando.",
    "Ci sono scelte che sembrano piccole finché non ti accorgi che ti hanno definito.",
    "La nostalgia è memoria con l’audio distorto.",
    "A volte la pace assomiglia troppo alla resa.",
    "Le persone cambiano prima dentro e poi nei dettagli.",
    "Il coraggio non è fare rumore: è restare quando potresti sparire.",
    "Siamo fedeli a ciò che ci ferisce quando ci è familiare.",
    "Il futuro arriva come una notifica: senza chiedere permesso.",
    "Un silenzio può essere una risposta più precisa di mille parole.",
    "Non ti manca una persona: ti manca la versione di te che eri con lei.",
    "La libertà è anche il diritto di deludere aspettative che non hai scelto.",
    "La distanza più grande è tra ciò che senti e ciò che riesci a dire.",
    "Le abitudini sono decisioni che hanno smesso di farsi notare.",
    "Il controllo è spesso paura con un vestito elegante.",
    "Quando ti giustifichi troppo, stai già negoziando con la tua coscienza.",
    "Un dettaglio ripetuto è un messaggio che non vuoi leggere.",
    "La fiducia si rompe in anticipo: la crepa arriva prima del rumore.",
    "Non tutto ciò che chiudi finisce: a volte cambia solo forma.",
    "La solitudine non è assenza: è mancanza di risonanza.",
]

def random_topic() -> str:
    return random.choice(DAILY_PHRASES_DEEP)
