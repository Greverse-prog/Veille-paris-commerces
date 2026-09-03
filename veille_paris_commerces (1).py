#!/usr/bin/env python3
r"""
Veille automatisée des créations de coffee shops, restaurants et boulangeries à Paris.

Source de données : API Recherche d'entreprises (recherche-entreprises.api.gouv.fr)
- API officielle, publique, gratuite, sans clé d'accès requise.
- Agrège les données Sirene (INSEE) et RNE, mises à jour en continu.

Ce que fait ce script :
1. Interroge l'API pour chaque code NAF pertinent, dans les arrondissements de Paris.
2. Compare avec la liste des SIREN déjà vus (fichier local "vus.json").
3. N'affiche/n'enregistre que les NOUVELLES créations depuis la dernière exécution.
4. Écrit un CSV horodaté avec les nouveautés + met à jour la liste des "vus".
5. (Optionnel) Envoie un email si tu configures les identifiants SMTP plus bas.

Comment l'automatiser (choisis une option) :
------------------------------------------
A) En local avec cron (Mac/Linux) :
   crontab -e
   puis ajoute (exécution tous les jours à 8h) :
   0 8 * * * /usr/bin/python3 /chemin/vers/veille_paris_commerces.py >> /chemin/vers/veille.log 2>&1

B) Avec le Planificateur de tâches Windows :
   Crée une tâche qui lance : python.exe C:\chemin\veille_paris_commerces.py

C) Gratuitement dans le cloud avec GitHub Actions (recommandé si tu veux zéro maintenance) :
   - Crée un dépôt GitHub privé, mets-y ce script.
   - Ajoute un fichier .github/workflows/veille.yml (je peux te le générer si tu veux)
     qui lance le script tous les jours et commit le résultat, ou envoie un email
     via une Action dédiée.

Dépendances : aucune (uniquement la bibliothèque standard Python 3).
"""

import json
import csv
import urllib.request
import urllib.parse
import urllib.error
import time
from pathlib import Path
from datetime import datetime, timedelta

# ============================================================
# CONFIGURATION — à adapter selon tes besoins
# ============================================================

# Codes NAF à surveiller (ajoute/retire selon tes besoins)
NAF_CODES = {
    "56.10A": "Restauration traditionnelle",
    "56.10C": "Restauration rapide (souvent utilisé aussi par les coffee shops)",
    "56.30Z": "Débits de boissons (coffee shops, bars à café)",
    "10.71C": "Boulangerie-pâtisserie",
}

# Arrondissements de Paris à surveiller (75001 à 75020)
CODES_POSTAUX = [f"750{str(i).zfill(2)}" for i in range(1, 21)]

# Ne garder que les établissements créés dans les N derniers jours
FENETRE_JOURS = 30

# Fichiers locaux (mémoire de ce qui a déjà été vu + résultats)
DOSSIER = Path(__file__).parent
FICHIER_VUS = DOSSIER / "vus.json"
DOSSIER_RESULTATS = DOSSIER / "resultats"

# --- Envoi d'email (optionnel) ---
# Laisse ENVOYER_EMAIL = False si tu ne veux pas configurer l'envoi automatique.
ENVOYER_EMAIL = False
EMAIL_EXPEDITEUR = "ton.adresse@gmail.com"
EMAIL_MOT_DE_PASSE = "xxxx xxxx xxxx xxxx"  # mot de passe d'application, jamais ton vrai mdp
EMAIL_DESTINATAIRE = "ton.adresse@gmail.com"
SMTP_SERVEUR = "smtp.gmail.com"
SMTP_PORT = 587

# ============================================================
# LOGIQUE DU SCRIPT — pas besoin d'y toucher
# ============================================================

API_BASE = "https://recherche-entreprises.api.gouv.fr/search"
MAX_TENTATIVES = 3


def interroger_api(code_naf: str, code_postal: str, page: int = 1) -> dict:
    """Appelle l'API Recherche d'entreprises, avec tentatives automatiques en cas d'erreur."""
    params = {
        "activite_principale": code_naf,
        "code_postal": code_postal,
        "per_page": 25,
        "page": page,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "veille-paris-commerces/1.0"})

    for tentative in range(1, MAX_TENTATIVES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:  # trop de requêtes : on patiente plus longtemps
                attente = 5 * tentative
                print(f"  Rate limit atteint, pause de {attente}s...")
                time.sleep(attente)
            elif tentative == MAX_TENTATIVES:
                raise
            else:
                time.sleep(2 * tentative)
        except (urllib.error.URLError, TimeoutError):
            if tentative == MAX_TENTATIVES:
                raise
            time.sleep(2 * tentative)

    return {"results": []}


def toutes_les_pages(code_naf: str, code_postal: str):
    """Génère tous les résultats pour un couple (NAF, code postal), toutes pages confondues."""
    page = 1
    while True:
        data = interroger_api(code_naf, code_postal, page=page)
        resultats = data.get("results", [])
        if not resultats:
            return
        yield from resultats

        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            return
        page += 1
        time.sleep(0.3)


def charger_vus() -> set:
    if FICHIER_VUS.exists():
        return set(json.loads(FICHIER_VUS.read_text(encoding="utf-8")))
    return set()


def sauvegarder_vus(vus: set) -> None:
    FICHIER_VUS.write_text(json.dumps(sorted(vus)), encoding="utf-8")


def extraire_ligne(entreprise: dict, code_naf: str) -> dict:
    siege = entreprise.get("siege", {}) or {}
    return {
        "siren": entreprise.get("siren"),
        "nom": entreprise.get("nom_complet"),
        "naf": code_naf,
        "activite": NAF_CODES.get(code_naf, ""),
        "adresse": siege.get("adresse"),
        "code_postal": siege.get("code_postal"),
        "date_creation": entreprise.get("date_creation"),
        "dirigeant": ", ".join(
            f"{d.get('prenoms', '')} {d.get('nom', '')}".strip()
            for d in entreprise.get("dirigeants", [])
            if d.get("type_dirigeant") == "personne physique"
        ),
    }


def date_recente(date_str: str, limite: datetime) -> bool:
    if not date_str:
        return False
    try:
        return datetime.strptime(date_str, "%Y-%m-%d") >= limite
    except ValueError:
        return False


def envoyer_email(nouveautes: list) -> None:
    import smtplib
    from email.mime.text import MIMEText

    corps = "Nouvelles créations détectées :\n\n"
    for n in nouveautes:
        corps += (
            f"- {n['nom']} ({n['activite']})\n"
            f"  {n['adresse']} {n['code_postal']}\n"
            f"  Créée le {n['date_creation']} — SIREN {n['siren']}\n\n"
        )

    msg = MIMEText(corps, _charset="utf-8")
    msg["Subject"] = f"Veille commerces Paris — {len(nouveautes)} nouveauté(s)"
    msg["From"] = EMAIL_EXPEDITEUR
    msg["To"] = EMAIL_DESTINATAIRE

    with smtplib.SMTP(SMTP_SERVEUR, SMTP_PORT) as serveur:
        serveur.starttls()
        serveur.login(EMAIL_EXPEDITEUR, EMAIL_MOT_DE_PASSE)
        serveur.send_message(msg)


def main() -> None:
    debut = datetime.now()
    print(f"[{debut:%Y-%m-%d %H:%M}] Démarrage de la veille...")

    vus = charger_vus()
    limite = datetime.now() - timedelta(days=FENETRE_JOURS)
    nouveautes = []

    for code_naf, libelle in NAF_CODES.items():
        print(f"  Recherche : {libelle} ({code_naf})")
        for code_postal in CODES_POSTAUX:
            try:
                for entreprise in toutes_les_pages(code_naf, code_postal):
                    siren = entreprise.get("siren")
                    if siren in vus:
                        continue
                    if not date_recente(entreprise.get("date_creation"), limite):
                        continue
                    ligne = extraire_ligne(entreprise, code_naf)
                    nouveautes.append(ligne)
                    vus.add(siren)
            except Exception as e:
                print(f"  Erreur API ({code_naf}, {code_postal}) : {e}")
                continue

    # Les créations les plus récentes en premier
    nouveautes.sort(key=lambda n: n.get("date_creation") or "", reverse=True)

    duree = (datetime.now() - debut).total_seconds()
    print(f"Recherche terminée en {duree:.0f}s.")

    if not nouveautes:
        print("Aucune nouveauté détectée aujourd'hui.")
        sauvegarder_vus(vus)
        return

    DOSSIER_RESULTATS.mkdir(exist_ok=True)
    horodatage = datetime.now().strftime("%Y-%m-%d_%Hh%M")
    fichier_csv = DOSSIER_RESULTATS / f"nouveautes_{horodatage}.csv"

    with open(fichier_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(nouveautes[0].keys()))
        writer.writeheader()
        writer.writerows(nouveautes)

    print(f"{len(nouveautes)} nouveauté(s) trouvée(s). Détails dans {fichier_csv}")
    for n in nouveautes:
        print(f"  - {n['nom']} ({n['activite']}) — {n['adresse']} {n['code_postal']}")

    sauvegarder_vus(vus)

    if ENVOYER_EMAIL:
        envoyer_email(nouveautes)
        print("Email envoyé.")


if __name__ == "__main__":
    main()
