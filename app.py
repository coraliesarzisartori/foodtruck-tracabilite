import streamlit as st
import sqlite3
import json
import base64
import requests
from datetime import datetime, date, timedelta
from PIL import Image, ImageEnhance
import io
import pandas as pd
import imaplib
import email as email_lib
from email.header import decode_header as decode_hdr

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="FoodTruck Tracabilite",
    page_icon="🚚",
    layout="centered",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    .main > div { padding: 0.8rem; }
    .stButton > button {
        width: 100%; border-radius: 12px;
        height: 3rem; font-size: 1rem; font-weight: 600;
    }
    .card {
        background: #f8f9fa; border-radius: 12px;
        padding: 1rem; margin: 0.5rem 0;
        border-left: 4px solid #ff4b4b;
    }
    .badge-lot { background: #ff4b4b; color: white; border-radius: 8px; padding: 2px 8px; font-size: 0.8rem; font-weight: 600; }
    .badge-dlc { background: #0068c9; color: white; border-radius: 8px; padding: 2px 8px; font-size: 0.8rem; font-weight: 600; }
    .badge-qte { background: #21a846; color: white; border-radius: 8px; padding: 2px 8px; font-size: 0.8rem; font-weight: 600; }
    .step-indicator { background: #e8f4fd; border-radius: 8px; padding: 0.5rem 1rem; margin-bottom: 1rem; font-weight: 600; color: #0068c9; }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════
#  OPENAI SETUP
# ═══════════════════════════════════════════════════════════════
try:
    OPENAI_KEY = st.secrets["OPENAI_API_KEY"]
    AI_OK = True
except Exception:
    OPENAI_KEY = None
    AI_OK = False

# ═══════════════════════════════════════════════════════════════
#  BASE DE DONNEES
# ═══════════════════════════════════════════════════════════════
DB = "tracabilite.db"

def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    db = conn()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fournisseurs (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            nom  TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS livraisons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fournisseur_id  INTEGER NOT NULL,
            numero_bl       TEXT,
            date_reception  DATE NOT NULL,
            temperature     REAL,
            conformite      TEXT DEFAULT 'conforme',
            notes           TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (fournisseur_id) REFERENCES fournisseurs(id)
        );
        CREATE TABLE IF NOT EXISTS produits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            livraison_id    INTEGER,
            fournisseur_id  INTEGER NOT NULL,
            nom             TEXT NOT NULL,
            numero_lot      TEXT,
            dlc             DATE,
            temperature     REAL,
            conformite      TEXT DEFAULT 'conforme',
            notes           TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (livraison_id)   REFERENCES livraisons(id),
            FOREIGN KEY (fournisseur_id) REFERENCES fournisseurs(id)
        );
        CREATE TABLE IF NOT EXISTS preparations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date_prep  DATE NOT NULL,
            heure_prep TEXT NOT NULL,
            notes      TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS preparation_produits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            preparation_id  INTEGER NOT NULL,
            produit_id      INTEGER NOT NULL,
            FOREIGN KEY (preparation_id) REFERENCES preparations(id),
            FOREIGN KEY (produit_id)     REFERENCES produits(id)
        );
        CREATE TABLE IF NOT EXISTS factures (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            livraison_id INTEGER,
            nom_fichier  TEXT,
            contenu_b64  TEXT,
            expediteur   TEXT,
            sujet        TEXT,
            date_email   TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (livraison_id) REFERENCES livraisons(id)
        );
        CREATE TABLE IF NOT EXISTS app_config (
            cle    TEXT PRIMARY KEY,
            valeur TEXT
        );
    """)
    db.commit()
    # Migrations colonnes
    for migration in [
        "ALTER TABLE livraisons ADD COLUMN numero_bl TEXT",
        "ALTER TABLE produits ADD COLUMN temperature REAL",
        "ALTER TABLE produits ADD COLUMN conformite TEXT DEFAULT 'conforme'",
        "ALTER TABLE produits ADD COLUMN notes TEXT",
        "ALTER TABLE produits ADD COLUMN quantite TEXT",
        "ALTER TABLE livraisons ADD COLUMN photo_bl_b64 TEXT",
        "ALTER TABLE livraisons ADD COLUMN photo_bl_ext TEXT",
        "ALTER TABLE produits ADD COLUMN photo_etiquette_b64 TEXT",
    ]:
        try:
            db.execute(migration)
            db.commit()
        except Exception:
            pass

    # Migration donnees : rattache les produits orphelins a leur livraison la plus proche
    try:
        db.execute("""
            UPDATE produits
            SET livraison_id = (
                SELECT l.id FROM livraisons l
                WHERE l.fournisseur_id = produits.fournisseur_id
                ORDER BY ABS(julianday(l.date_reception) - julianday(produits.created_at))
                LIMIT 1
            )
            WHERE livraison_id IS NULL
              AND fournisseur_id IS NOT NULL
        """)
        db.commit()
    except Exception:
        pass
    db.close()

init_db()

# ═══════════════════════════════════════════════════════════════
#  IA — GPT-4o-mini
# ═══════════════════════════════════════════════════════════════
def image_base64(image_data):
    img = Image.open(io.BytesIO(image_data))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    w, h = img.size
    if w < 1000:
        ratio = 1000 / w
        img = img.resize((int(w*ratio), int(h*ratio)), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.5)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def appeler_gpt(prompt, image_data):
    if not AI_OK:
        return {"erreur": "Cle OpenAI non configuree"}
    try:
        img_b64 = image_base64(image_data)
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{img_b64}",
                                       "detail": "high"}}
                    ]
                }],
                "max_tokens": 500
            },
            timeout=30
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        return {"erreur": str(e)}

def appeler_gpt_texte(prompt):
    """Appel GPT texte seul (sans image) — pour PDFs numeriques."""
    if not AI_OK:
        return {"erreur": "Cle OpenAI non configuree"}
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            },
            timeout=30
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        return {"erreur": str(e)}

def pdf_premiere_page_en_image(pdf_bytes):
    """Convertit la 1ere page d'un PDF en bytes JPEG (via pymupdf)."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        mat  = fitz.Matrix(2, 2)  # zoom x2 pour meilleure qualite GPT
        pix  = page.get_pixmap(matrix=mat)
        img  = pix.tobytes("jpeg")
        doc.close()
        return img
    except Exception as e:
        return None

def lire_bl_pdf(pdf_bytes):
    """Lit un BL PDF : texte (PDF numerique) OU image 1ere page (PDF scanne)."""
    # Essai 1 : extraction de texte (PDF numerique)
    texte = extraire_texte_pdf(pdf_bytes)
    if texte.strip():
        prompt = (
            f"Voici le texte extrait d'un bon de livraison alimentaire :\n\n{texte[:3000]}\n\n"
            "Reponds UNIQUEMENT en JSON valide: "
            "{\"fournisseur\": \"nom societe\", "
            "\"numero_bl\": \"numero du bon de livraison (BL, N° BL, Bon N°, reference)\", "
            "\"date\": \"date YYYY-MM-DD\", "
            "\"produits\": [{\"nom\": \"produit\", \"quantite\": \"qte\"}]} "
            "Si absent mets null."
        )
        return appeler_gpt_texte(prompt)

    # Essai 2 : PDF scanne — convertit la 1ere page en image → GPT vision
    img_bytes = pdf_premiere_page_en_image(pdf_bytes)
    if img_bytes:
        return lire_bl(img_bytes)

    return {"erreur": "Impossible de lire ce PDF (ni texte ni image extraits)"}

def lire_etiquette(image_data):
    prompt = (
        "Tu es expert en etiquettes alimentaires francaises. "
        "Lis cette etiquette et reponds UNIQUEMENT en JSON valide: "
        "{\"nom_produit\": \"nom complet\", "
        "\"marque\": \"marque\", "
        "\"numero_lot\": \"numero de lot exact (LOT, L:, Batch)\", "
        "\"dlc\": \"date limite YYYY-MM-DD (DLC, DDM, BBD, consommer avant)\"} "
        "Si absent mets null."
    )
    return appeler_gpt(prompt, image_data)

def lire_bl(image_data):
    prompt = (
        "Analyse ce bon de livraison alimentaire. "
        "Reponds UNIQUEMENT en JSON valide: "
        "{\"fournisseur\": \"nom societe\", "
        "\"numero_bl\": \"numero du bon de livraison (BL, N° BL, Bon N°, reference)\", "
        "\"date\": \"date YYYY-MM-DD\", "
        "\"produits\": [{\"nom\": \"produit\", \"quantite\": \"qte\"}]} "
        "Si absent mets null."
    )
    return appeler_gpt(prompt, image_data)

def lire_facture_pdf(pdf_bytes):
    """Lit une facture PDF : texte (numerique) ou image 1ere page (scanne)."""
    texte = extraire_texte_pdf(pdf_bytes)
    if texte.strip():
        prompt = (
            f"Voici le texte extrait d'une facture fournisseur :\n\n{texte[:3000]}\n\n"
            "Reponds UNIQUEMENT en JSON valide: "
            "{\"numero_bl\": \"numero BL present (BL, N° BL, Ref BL, Bon de livraison)\", "
            "\"numero_facture\": \"numero de facture (N° FA, Facture N°, FAC)\", "
            "\"fournisseur\": \"nom fournisseur\", "
            "\"date_facture\": \"date YYYY-MM-DD\"} "
            "Si absent mets null."
        )
        return appeler_gpt_texte(prompt)
    img_bytes = pdf_premiere_page_en_image(pdf_bytes)
    if img_bytes:
        return lire_facture_image(img_bytes)
    return {"erreur": "Impossible de lire ce PDF"}

def lire_facture_image(image_data):
    """Lit une facture (photo/scan) avec GPT et extrait N° BL, N° facture, fournisseur."""
    prompt = (
        "Analyse cette facture fournisseur alimentaire. "
        "Reponds UNIQUEMENT en JSON valide: "
        "{\"numero_bl\": \"numero BL present sur la facture (BL, N° BL, Ref BL, Bon de livraison, Reference livraison)\", "
        "\"numero_facture\": \"numero de facture (N° FA, N° Facture, Facture N°, FAC)\", "
        "\"fournisseur\": \"nom du fournisseur\", "
        "\"date_facture\": \"date YYYY-MM-DD\"} "
        "Si absent mets null."
    )
    return appeler_gpt(prompt, image_data)

def extraire_texte_pdf(content_bytes):
    """Extrait le texte brut d'un PDF numerique (pas les PDFs scannes)."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content_bytes))
        text = ""
        for page in reader.pages[:5]:
            text += (page.extract_text() or "") + "\n"
        return text
    except Exception:
        return ""

# ═══════════════════════════════════════════════════════════════
#  BASE DE DONNEES — fonctions
# ═══════════════════════════════════════════════════════════════
def get_fournisseurs():
    db = conn()
    rows = db.execute("SELECT * FROM fournisseurs ORDER BY nom").fetchall()
    db.close()
    return rows

def ajouter_fournisseur(nom):
    db = conn()
    try:
        db.execute("INSERT INTO fournisseurs (nom) VALUES (?)", (nom,))
        db.commit()
        return True
    except Exception:
        return False
    finally:
        db.close()

def get_produits(fournisseur_id=None):
    db = conn()
    if fournisseur_id:
        rows = db.execute("""
            SELECT p.*, f.nom AS fourn, l.date_reception, l.temperature, l.conformite
            FROM produits p
            JOIN fournisseurs f ON p.fournisseur_id = f.id
            LEFT JOIN livraisons l ON p.livraison_id = l.id
            WHERE p.fournisseur_id = ?
            ORDER BY p.created_at DESC
        """, (fournisseur_id,)).fetchall()
    else:
        rows = db.execute("""
            SELECT p.*, f.nom AS fourn, l.date_reception, l.temperature, l.conformite
            FROM produits p
            JOIN fournisseurs f ON p.fournisseur_id = f.id
            LEFT JOIN livraisons l ON p.livraison_id = l.id
            ORDER BY p.created_at DESC
        """).fetchall()
    db.close()
    return rows

def rechercher_lot(numero_lot):
    db = conn()
    rows = db.execute("""
        SELECT DISTINCT
            p.id, p.nom, p.numero_lot, p.dlc, p.created_at,
            f.nom AS fourn,
            l.numero_bl, l.date_reception, l.temperature, l.conformite,
            prep.id AS prep_id, prep.date_prep, prep.heure_prep, prep.notes AS prep_notes
        FROM produits p
        JOIN fournisseurs f ON p.fournisseur_id = f.id
        LEFT JOIN livraisons l ON p.livraison_id = l.id
        LEFT JOIN preparation_produits pp ON p.id = pp.produit_id
        LEFT JOIN preparations prep ON pp.preparation_id = prep.id
        WHERE p.numero_lot LIKE ?
        ORDER BY p.created_at DESC
    """, (f"%{numero_lot}%",)).fetchall()
    db.close()
    return rows

def get_livraisons():
    db = conn()
    rows = db.execute("""
        SELECT l.id, l.numero_bl, l.date_reception, l.temperature, l.conformite, l.notes,
               l.photo_bl_b64, l.photo_bl_ext,
               f.nom AS nom_fourn
        FROM livraisons l
        JOIN fournisseurs f ON l.fournisseur_id = f.id
        ORDER BY l.date_reception DESC
    """).fetchall()
    db.close()
    return rows

def get_produits_livraison(livraison_id):
    db = conn()
    rows = db.execute("""
        SELECT * FROM produits WHERE livraison_id = ? ORDER BY created_at ASC
    """, (livraison_id,)).fetchall()
    db.close()
    return rows

def modifier_produit(produit_id, nom, lot, quantite, dlc, temperature, conformite, notes, photo_b64=None):
    db = conn()
    if photo_b64 is not None:
        db.execute("""
            UPDATE produits
            SET nom=?, numero_lot=?, quantite=?, dlc=?, temperature=?, conformite=?, notes=?, photo_etiquette_b64=?
            WHERE id=?
        """, (nom, lot or None, quantite or None, str(dlc), temperature, conformite, notes or None, photo_b64, produit_id))
    else:
        db.execute("""
            UPDATE produits
            SET nom=?, numero_lot=?, quantite=?, dlc=?, temperature=?, conformite=?, notes=?
            WHERE id=?
        """, (nom, lot or None, quantite or None, str(dlc), temperature, conformite, notes or None, produit_id))
    db.commit()
    db.close()

def maj_photo_bl(livraison_id, photo_b64, photo_ext):
    db = conn()
    db.execute("UPDATE livraisons SET photo_bl_b64=?, photo_bl_ext=? WHERE id=?",
               (photo_b64, photo_ext, livraison_id))
    db.commit()
    db.close()

def supprimer_produit(produit_id):
    db = conn()
    db.execute("DELETE FROM preparation_produits WHERE produit_id=?", (produit_id,))
    db.execute("DELETE FROM produits WHERE id=?", (produit_id,))
    db.commit()
    db.close()

def sauvegarder_facture(livraison_id, nom_fichier, contenu_b64, expediteur, sujet, date_email):
    db = conn()
    db.execute("""
        INSERT INTO factures (livraison_id, nom_fichier, contenu_b64, expediteur, sujet, date_email)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (livraison_id, nom_fichier, contenu_b64, expediteur, sujet, date_email))
    db.commit()
    db.close()

def get_factures(livraison_id=None):
    db = conn()
    if livraison_id:
        rows = db.execute("""
            SELECT f.*, l.numero_bl, four.nom AS nom_fourn
            FROM factures f
            LEFT JOIN livraisons l ON f.livraison_id = l.id
            LEFT JOIN fournisseurs four ON l.fournisseur_id = four.id
            WHERE f.livraison_id = ?
            ORDER BY f.created_at DESC
        """, (livraison_id,)).fetchall()
    else:
        rows = db.execute("""
            SELECT f.*, l.numero_bl, four.nom AS nom_fourn
            FROM factures f
            LEFT JOIN livraisons l ON f.livraison_id = l.id
            LEFT JOIN fournisseurs four ON l.fournisseur_id = four.id
            ORDER BY f.created_at DESC
        """).fetchall()
    db.close()
    return rows

def supprimer_facture(facture_id):
    db = conn()
    db.execute("DELETE FROM factures WHERE id = ?", (facture_id,))
    db.commit()
    db.close()

def lier_facture_a_livraison(facture_id, livraison_id):
    db = conn()
    db.execute("UPDATE factures SET livraison_id=? WHERE id=?", (livraison_id, facture_id))
    db.commit()
    db.close()

def get_config(cle, default=None):
    db = conn()
    row = db.execute("SELECT valeur FROM app_config WHERE cle=?", (cle,)).fetchone()
    db.close()
    return row["valeur"] if row else default

def set_config(cle, valeur):
    db = conn()
    db.execute(
        "INSERT INTO app_config (cle, valeur) VALUES (?,?) "
        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
        (cle, valeur)
    )
    db.commit()
    db.close()

def supprimer_livraison(livraison_id):
    """Supprime une livraison et tous ses produits/factures associes."""
    db = conn()
    db.execute("DELETE FROM produits  WHERE livraison_id=?", (livraison_id,))
    db.execute("DELETE FROM factures  WHERE livraison_id=?", (livraison_id,))
    db.execute("DELETE FROM livraisons WHERE id=?",          (livraison_id,))
    db.commit()
    db.close()

# ═══════════════════════════════════════════════════════════════
#  PROGINOV — telechargement automatique
# ═══════════════════════════════════════════════════════════════
PROGINOV_LOGIN_URL = "https://www.proginov.fr/ProginovDemat/connexion.html"

def telecharger_proginov(lien_facture):
    """Se connecte a PROGINOV et telecharge le PDF de la facture."""
    try:
        prog_user = st.secrets["PROGINOV_LOGIN"]
        prog_pass = st.secrets["PROGINOV_PASSWORD"]
    except Exception:
        return None, "PROGINOV non configure"

    try:
        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"
        s  = requests.Session()
        s.headers.update({"User-Agent": UA})

        # 1. Recupere la page de login (cookies de session Wicket)
        r = s.get(PROGINOV_LOGIN_URL, timeout=15)

        # 2. Extrait l'URL d'action du formulaire (contient l'ID Wicket de session)
        m_action = _re.search(r'<form[^>]+id=["\']id2["\'][^>]+action=["\']([^"\']+)["\']', r.text, _re.IGNORECASE)
        if not m_action:
            m_action = _re.search(r'action=["\'](\./connexion\.html\?[^"\']+formAuthent[^"\']*)["\']', r.text)
        action_path = m_action.group(1) if m_action else "./connexion.html?0-1.-wicket_434-formAuthent"
        action_url  = "https://www.proginov.fr/ProginovDemat/" + action_path.lstrip("./")

        # 3. Construit le POST avec les vrais noms de champs PROGINOV
        data = {
            "fUsr":                   prog_user,
            "fPwd":                   prog_pass,
            "cbCnxAuto":              "on",
            "ipcnx":                  "",
            "navigatorAppName":       "Netscape",
            "navigatorAppVersion":    "5.0 (Windows)",
            "navigatorAppCodeName":   "Mozilla",
            "navigatorCookieEnabled": "true",
            "navigatorJavaEnabled":   "false",
            "navigatorLanguage":      "fr-FR",
            "navigatorPlatform":      "Win32",
            "navigatorUserAgent":     UA,
            "navigatorWidth":         "1920",
            "navigatorHeight":        "1080",
            "screenWidth":            "1920",
            "screenHeight":           "1080",
            "screenColorDepth":       "24",
            "utcOffset":              "-120",
        }

        # 4. Connexion
        r2 = s.post(action_url, data=data, timeout=15, allow_redirects=True)

        # Verifie si connecte (page d'accueil apres login ne contient plus le form de connexion)
        if "fUsr" in r2.text or "connexion" in r2.url.lower():
            return None, "Identifiants PROGINOV incorrects ou connexion refusee"

        # 5. Telecharge le PDF avec la session authentifiee
        r3 = s.get(lien_facture, timeout=20, allow_redirects=True)

        if r3.status_code == 200:
            ct = r3.headers.get("Content-Type", "")
            if "pdf" in ct.lower() or r3.content[:4] == b"%PDF":
                return base64.b64encode(r3.content).decode("utf-8"), None
            else:
                return None, "Connecte a PROGINOV mais PDF non obtenu"
        else:
            return None, f"Erreur HTTP {r3.status_code}"

    except Exception as e:
        return None, str(e)

# ═══════════════════════════════════════════════════════════════
#  GMAIL — recuperation factures
# ═══════════════════════════════════════════════════════════════
def _decode_str(s):
    if not s:
        return ""
    parts = decode_hdr(s)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += str(part)
    return result

import re as _re

def _extraire_numero_bl(texte):
    """Extrait un numero de BL potentiel depuis un texte (sujet email)."""
    patterns = [
        r'(?:N°\s*BL|BL\s*N°|BL\s*#|BL[-_\s])([A-Z0-9][-A-Z0-9]{2,15})',
        r'(?:bon\s*(?:de\s*)?livraison\s*(?:N°|#|:)?\s*)([A-Z0-9][-A-Z0-9]{2,15})',
        r'(?:ref(?:erence)?[:\s]+)([A-Z0-9][-A-Z0-9]{3,15})',
    ]
    for pat in patterns:
        m = _re.search(pat, texte, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def _extraire_numero_facture(texte):
    """Extrait un numero de facture depuis le sujet email."""
    patterns = [
        r'(?:facture\s*N°?[\s:]*|invoice\s*N°?[\s:]*)([A-Z0-9][-A-Z0-9]{4,20})',
        r'(?:N°|#)\s*([0-9]{5,15})',
        r'\b([0-9]{8,15})\b',
    ]
    for pat in patterns:
        m = _re.search(pat, texte, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def _extraire_liens_body(msg):
    """Extrait les URLs du corps de l'email."""
    urls = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html"):
            try:
                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                found = _re.findall(r'https?://[^\s<>"\']{10,}', body)
                urls.extend(found)
            except Exception:
                pass
    return list(dict.fromkeys(urls))  # deduplique

def _trouver_fourn(text_low, fournisseurs):
    """Retourne (id, nom) du fournisseur detecte, ou (None, None)."""
    for f in fournisseurs:
        mots = [m for m in f["nom"].lower().split() if len(m) > 3]
        if any(m in text_low for m in mots):
            return f["id"], f["nom"]
    return None, None

def _trouver_livraison(numero_bl_email, fournisseur_id, livraisons):
    """Retourne l'id de livraison correspondant au BL detecte."""
    if numero_bl_email:
        for l in livraisons:
            if l["numero_bl"] and numero_bl_email.lower() in l["numero_bl"].lower():
                return l["id"]
    if fournisseur_id:
        for l in livraisons:
            # Prend la livraison la plus recente de ce fournisseur
            # (livraisons triees par date DESC dans get_livraisons)
            db2 = conn()
            row = db2.execute(
                "SELECT id FROM livraisons WHERE fournisseur_id=? ORDER BY date_reception DESC LIMIT 1",
                (fournisseur_id,)
            ).fetchone()
            db2.close()
            if row:
                return row["id"]
            break
    return None

def fetch_factures_gmail(jours=30):
    try:
        EMAIL_ADDR = st.secrets["EMAIL_ADDRESS"]
        EMAIL_PASS = st.secrets["EMAIL_APP_PASSWORD"]
    except Exception:
        return {"erreur": "Email non configure — ajoute EMAIL_ADDRESS et EMAIL_APP_PASSWORD dans Secrets"}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(EMAIL_ADDR, EMAIL_PASS)

        # Cherche dans "Tous les messages" pour trouver aussi les emails classes dans des dossiers
        # Les noms avec espaces doivent etre entre guillemets pour IMAP
        dossier_ok = False
        for dossier in ['"[Gmail]/All Mail"', '"[Gmail]/Tous les messages"', 'INBOX']:
            try:
                status, _ = mail.select(dossier, readonly=True)
                if status == "OK":
                    dossier_ok = True
                    break
            except Exception:
                continue
        if not dossier_ok:
            mail.logout()
            return {"erreur": "Impossible d'acceder aux emails Gmail"}

        since = (datetime.now() - timedelta(days=jours)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'SINCE {since}')
        if status != "OK" or not data[0]:
            mail.logout()
            return []

        ids = data[0].split()
        fournisseurs    = get_fournisseurs()
        livraisons      = get_livraisons()
        keywords_invoice = ["facture", "invoice", "bon de livraison", "bl ", "commande", "livraison", "avoir"]

        # Mots-cles fournisseurs pour filtrer (configurable depuis Config)
        kw_raw = get_config("gmail_keywords", "sysco,gda,relais d'or,krill")
        keywords_fourn_filter = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

        found = []

        for eid in ids[-500:]:
            try:
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                subject  = _decode_str(msg.get("Subject", ""))
                sender   = _decode_str(msg.get("From", ""))
                date_str = msg.get("Date", "")

                text_low = (subject + " " + sender).lower()

                # Filtre : garde uniquement les emails des fournisseurs voulus
                if keywords_fourn_filter and not any(kw in text_low for kw in keywords_fourn_filter):
                    continue

                is_invoice = any(k in text_low for k in keywords_invoice)

                # Detection automatique fournisseur
                fourn_id, fourn_nom = _trouver_fourn(text_low, fournisseurs)
                is_fourn = fourn_id is not None

                # Detection automatique numero BL
                num_bl_email = _extraire_numero_bl(subject) or _extraire_numero_bl(sender)

                # Detection automatique livraison correspondante
                livraison_id_auto = _trouver_livraison(num_bl_email, fourn_id, livraisons)

                has_attachment = False
                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    disp = part.get("Content-Disposition", "")
                    if not disp:
                        continue
                    filename = _decode_str(part.get_filename() or "")
                    if not filename:
                        continue
                    ext = filename.lower().split(".")[-1]
                    if ext not in ("pdf", "jpg", "jpeg", "png"):
                        continue
                    content = part.get_payload(decode=True)
                    if not content:
                        continue
                    has_attachment = True

                    # Pour les PDFs numeriques : extrait automatiquement N° BL et N° facture
                    bl_detecte      = num_bl_email
                    num_fac_detecte = _extraire_numero_facture(subject)
                    if ext == "pdf":
                        pdf_text = extraire_texte_pdf(content)
                        if pdf_text:
                            bl_pdf = _extraire_numero_bl(pdf_text)
                            if bl_pdf and not bl_detecte:
                                bl_detecte = bl_pdf
                            nf = _extraire_numero_facture(pdf_text)
                            if nf:
                                num_fac_detecte = nf
                    liv_id_final = _trouver_livraison(bl_detecte, fourn_id, livraisons) or livraison_id_auto

                    found.append({
                        "filename":         filename,
                        "content_b64":      base64.b64encode(content).decode("utf-8"),
                        "sender":           sender,
                        "subject":          subject,
                        "date":             date_str,
                        "is_fourn":         is_fourn,
                        "is_invoice":       is_invoice,
                        "ext":              ext,
                        "fourn_id":         fourn_id,
                        "fourn_nom":        fourn_nom,
                        "num_bl_email":     bl_detecte,
                        "livraison_id_auto":liv_id_final,
                        "lien":             None,
                        "num_facture":      num_fac_detecte,
                    })

                # Email sans PJ mais avec "facture" dans sujet → note le lien (telechargement separe)
                if not has_attachment and is_invoice:
                    num_fac  = _extraire_numero_facture(subject)
                    liens    = _extraire_liens_body(msg)
                    lien_fac = liens[0] if liens else None
                    fname    = f"FACTURE_{num_fac or 'lien'}.txt"
                    info     = f"Facture: {subject}\nDe: {sender}\nDate: {date_str}\nLien: {lien_fac or 'non trouve'}"
                    is_prog  = lien_fac and "proginov" in lien_fac.lower()
                    found.append({
                            "filename":         fname,
                            "content_b64":      base64.b64encode(info.encode()).decode("utf-8"),
                            "sender":           sender,
                            "subject":          subject,
                            "date":             date_str,
                            "is_fourn":         is_fourn,
                            "is_invoice":       True,
                            "ext":              "txt",
                            "fourn_id":         fourn_id,
                            "fourn_nom":        fourn_nom,
                            "num_bl_email":     num_bl_email,
                            "livraison_id_auto":livraison_id_auto,
                            "lien":             lien_fac,
                            "num_facture":      num_fac,
                            "auto_downloaded":  False,
                            "is_proginov":      is_prog,
                        })
            except Exception:
                continue

        mail.close()
        mail.logout()
        return found

    except imaplib.IMAP4.error as e:
        return {"erreur": f"Connexion Gmail echouee : {e}"}
    except Exception as e:
        return {"erreur": str(e)}

# ═══════════════════════════════════════════════════════════════
#  PAGE : RECEPTION
# ═══════════════════════════════════════════════════════════════
def page_reception():
    st.header("📦 Reception livraison")
    onglet_rec, onglet_histo = st.tabs(["➕ Nouvelle reception", "📋 Historique livraisons"])

    with onglet_histo:
        livraisons = get_livraisons()
        if not livraisons:
            st.info("Aucune livraison enregistree pour l'instant.")
        else:
            st.write(f"**{len(livraisons)} livraison(s) enregistree(s)**")
            for l in livraisons:
                nb_produits = len(get_produits_livraison(l['id']))
                bl_label    = l['numero_bl'] if l['numero_bl'] else "N° BL non renseigne"
                conf_icon   = "✅" if l['conformite'] == "conforme" else ("⚠️" if l['conformite'] == "avec reserve" else "❌")
                with st.expander(f"{conf_icon}  {bl_label}  •  {l['nom_fourn']}  •  {l['date_reception']}  ({nb_produits} produit(s))"):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**Fournisseur :** {l['nom_fourn']}")
                        st.write(f"**N° BL :** {l['numero_bl'] or '—'}")
                        st.write(f"**Date :** {l['date_reception']}")
                    with col2:
                        if l['temperature'] is not None:
                            st.write(f"**Temperature :** {l['temperature']}°C")
                        st.write(f"**Conformite :** {l['conformite']}")
                        if l['notes']:
                            st.write(f"**Notes :** {l['notes']}")

                    # Photo BL
                    _bl_b64 = l["photo_bl_b64"] if "photo_bl_b64" in l.keys() else None
                    _bl_ext = l["photo_bl_ext"] if "photo_bl_ext" in l.keys() else "jpg"
                    st.markdown("**🖼️ Photo du BL**")
                    if _bl_b64:
                        if _bl_ext == "pdf":
                            st.download_button("📄 Télécharger le BL (PDF)", base64.b64decode(_bl_b64),
                                               f"bl_{l['numero_bl'] or l['id']}.pdf", "application/pdf",
                                               key=f"dl_bl_histo_{l['id']}")
                        else:
                            st.image(base64.b64decode(_bl_b64), use_container_width=True)
                    new_bl_photo = st.file_uploader(
                        "📷 Ajouter / remplacer la photo BL" if not _bl_b64 else "📷 Remplacer la photo BL",
                        type=["jpg","jpeg","png","pdf"], key=f"up_bl_histo_{l['id']}"
                    )
                    if new_bl_photo:
                        _ext_new = "pdf" if new_bl_photo.name.lower().endswith(".pdf") else "jpg"
                        if _ext_new != "pdf":
                            st.image(new_bl_photo, use_container_width=True)
                        if st.button("💾 Sauvegarder photo BL", key=f"save_bl_photo_{l['id']}", use_container_width=True):
                            _new_b64 = base64.b64encode(new_bl_photo.getvalue()).decode()
                            maj_photo_bl(l['id'], _new_b64, _ext_new)
                            st.success("✅ Photo BL mise à jour !")
                            st.rerun()
                    st.divider()

                    produits = get_produits_livraison(l['id'])
                    st.markdown(f"**{len(produits)} produit(s) :**" if produits else "**Produits :**")
                    for p in produits:
                        _qte      = p["quantite"] if "quantite" in p.keys() else None
                        lot_txt   = f"LOT: {p['numero_lot']}" if p['numero_lot'] else "Pas de lot"
                        dlc_txt   = f"DLC: {p['dlc']}"        if p['dlc']        else "Pas de DLC"
                        temp_txt  = f"🌡️ {p['temperature']}°C" if p['temperature'] is not None else ""
                        conf_val  = p['conformite'] or "conforme"
                        conf_icon = "✅" if conf_val == "conforme" else ("⚠️" if conf_val == "avec reserve" else "❌")
                        qte_badge = f'<span class="badge-qte">📦 {_qte}</span>&nbsp;' if _qte else ""
                        edit_key  = f"edit_prod_{p['id']}"

                        _etiq_b64 = p["photo_etiquette_b64"] if "photo_etiquette_b64" in p.keys() else None
                        if not st.session_state.get(edit_key):
                            st.markdown(f"""<div class="card" style="margin:0.2rem 0;padding:0.5rem 1rem;">
                                <strong>{p['nom']}</strong><br>
                                <span class="badge-lot">{lot_txt}</span>&nbsp;
                                <span class="badge-dlc">{dlc_txt}</span>&nbsp;
                                {qte_badge}
                                {f'<small>{temp_txt}</small>' if temp_txt else ''}
                                <small> {conf_icon} {conf_val}</small>
                                {f'<br><small><i>{p["notes"]}</i></small>' if p['notes'] else ''}
                            </div>""", unsafe_allow_html=True)
                            if _etiq_b64:
                                st.image(base64.b64decode(_etiq_b64), width=120, caption="Étiquette")
                            col_e, col_d = st.columns(2)
                            with col_e:
                                if st.button("✏️ Modifier", key=f"btn_edit_{p['id']}", use_container_width=True):
                                    st.session_state[edit_key] = True
                                    st.rerun()
                            with col_d:
                                if st.button("🗑️ Retirer", key=f"btn_del_prod_{p['id']}", use_container_width=True):
                                    supprimer_produit(p['id'])
                                    st.rerun()
                        else:
                            st.markdown(f"**✏️ Modifier : {p['nom']}**")
                            conf_opts = ["conforme", "non conforme", "avec reserve"]
                            dlc_edit_default = date.today()
                            if p['dlc']:
                                try: dlc_edit_default = datetime.strptime(str(p['dlc']), "%Y-%m-%d").date()
                                except: pass
                            # Photo hors form (camera_input ne fonctionne pas dans un form)
                            photo_edit_key = f"photo_edit_{p['id']}"
                            if _etiq_b64:
                                st.caption("📸 Photo actuelle :")
                                st.image(base64.b64decode(_etiq_b64), width=150)
                            new_photo_e = st.file_uploader(
                                "📷 Changer la photo étiquette (optionnel)",
                                type=["jpg","jpeg","png"], key=f"up_edit_et_{p['id']}"
                            )
                            if new_photo_e:
                                st.image(new_photo_e, width=150, caption="Nouvelle photo")
                                st.session_state[photo_edit_key] = new_photo_e.getvalue()
                            with st.form(f"form_edit_prod_{p['id']}"):
                                nom_e = st.text_input("Nom", value=p['nom'])
                                col_le, col_qe = st.columns(2)
                                with col_le: lot_e = st.text_input("N° de lot", value=p['numero_lot'] or "")
                                with col_qe: qte_e = st.text_input("Quantite",  value=_qte or "")
                                dlc_e = st.date_input("DLC", value=dlc_edit_default)
                                col_te, col_ce = st.columns(2)
                                with col_te:
                                    temp_e = st.number_input("🌡️ Temp. (°C)", value=float(p['temperature'] or 4.0), step=0.5)
                                with col_ce:
                                    conf_e = st.selectbox("Conformite", conf_opts,
                                                          index=conf_opts.index(conf_val) if conf_val in conf_opts else 0)
                                notes_e = st.text_area("Notes", value=p['notes'] or "")
                                col_sv, col_cx = st.columns(2)
                                with col_sv: do_save   = st.form_submit_button("💾 Sauvegarder", use_container_width=True)
                                with col_cx: do_cancel = st.form_submit_button("❌ Annuler",      use_container_width=True)
                                if do_save:
                                    # Recupere la nouvelle photo si chargee
                                    new_b64 = None
                                    if st.session_state.get(photo_edit_key):
                                        new_b64 = base64.b64encode(st.session_state[photo_edit_key]).decode()
                                    modifier_produit(p['id'], nom_e.strip(), lot_e.strip(), qte_e.strip(), dlc_e, temp_e, conf_e, notes_e, new_b64)
                                    st.session_state.pop(edit_key, None)
                                    st.session_state.pop(photo_edit_key, None)
                                    st.rerun()
                                if do_cancel:
                                    st.session_state.pop(edit_key, None)
                                    st.session_state.pop(photo_edit_key, None)
                                    st.rerun()

                    # ── Ajouter un produit à ce BL ─────────────────
                    add_key = f"add_prod_liv_{l['id']}"
                    if not st.session_state.get(add_key):
                        if st.button("➕ Ajouter un produit", key=f"btn_add_{l['id']}", use_container_width=True):
                            st.session_state[add_key] = True
                            st.rerun()
                    else:
                        st.markdown("**➕ Nouveau produit**")
                        db_tmp   = conn()
                        fid_row  = db_tmp.execute("SELECT fournisseur_id FROM livraisons WHERE id=?", (l['id'],)).fetchone()
                        db_tmp.close()
                        fourn_id_add = fid_row['fournisseur_id'] if fid_row else None
                        # Photo hors form
                        add_photo_key = f"photo_add_{l['id']}"
                        new_photo_add = st.file_uploader(
                            "📷 Photo étiquette (optionnel)",
                            type=["jpg","jpeg","png"], key=f"up_add_et_{l['id']}"
                        )
                        if new_photo_add:
                            st.image(new_photo_add, width=150)
                            st.session_state[add_photo_key] = new_photo_add.getvalue()
                        with st.form(f"form_add_prod_{l['id']}"):
                            nom_a = st.text_input("Nom du produit")
                            col_la, col_qa = st.columns(2)
                            with col_la: lot_a = st.text_input("N° de lot")
                            with col_qa: qte_a = st.text_input("Quantite", placeholder="Ex: 5 kg")
                            dlc_a = st.date_input("DLC", value=date.today())
                            col_ta, col_ca = st.columns(2)
                            with col_ta: temp_a = st.number_input("🌡️ Temp. (°C)", value=4.0, step=0.5)
                            with col_ca: conf_a = st.selectbox("Conformite", ["conforme","non conforme","avec reserve"])
                            notes_a = st.text_area("Notes", placeholder="Aspect, odeur...")
                            col_sa, col_xa = st.columns(2)
                            with col_sa: do_add     = st.form_submit_button("💾 Ajouter",  use_container_width=True)
                            with col_xa: do_cancel2 = st.form_submit_button("❌ Annuler", use_container_width=True)
                            if do_add:
                                if not nom_a.strip():
                                    st.error("Nom obligatoire.")
                                else:
                                    _add_photo_b64 = base64.b64encode(st.session_state[add_photo_key]).decode() if st.session_state.get(add_photo_key) else None
                                    db = conn()
                                    db.execute(
                                        "INSERT INTO produits (livraison_id,fournisseur_id,nom,numero_lot,quantite,dlc,temperature,conformite,notes,photo_etiquette_b64) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                        (l['id'], fourn_id_add, nom_a.strip(), lot_a.strip() or None,
                                         qte_a.strip() or None, dlc_a, temp_a, conf_a, notes_a or None, _add_photo_b64)
                                    )
                                    db.commit(); db.close()
                                    st.session_state.pop(add_key, None)
                                    st.session_state.pop(add_photo_key, None)
                                    st.rerun()
                            if do_cancel2:
                                st.session_state.pop(add_key, None)
                                st.session_state.pop(add_photo_key, None)
                                st.rerun()

                    # Factures liees
                    factures = get_factures(l['id'])
                    if factures:
                        st.markdown(f"**📎 {len(factures)} facture(s) liee(s) :**")
                        for fac in factures:
                            mime = "application/pdf" if fac['nom_fichier'].endswith(".pdf") else "image/jpeg"
                            st.download_button(
                                f"📥 {fac['nom_fichier']}",
                                base64.b64decode(fac['contenu_b64']),
                                fac['nom_fichier'], mime,
                                key=f"histo_dl_{fac['id']}"
                            )
                    else:
                        st.caption("Aucune facture liee — va dans l'onglet Factures pour en ajouter.")

                    # Bouton suppression avec confirmation
                    st.divider()
                    del_key = f"confirm_del_liv_{l['id']}"
                    if not st.session_state.get(del_key):
                        if st.button("🗑️ Supprimer ce BL", key=f"del_liv_{l['id']}", use_container_width=True):
                            st.session_state[del_key] = True
                            st.rerun()
                    else:
                        st.warning("⚠️ Supprimer definitivement ce BL, ses produits et ses factures ?")
                        col_y, col_n = st.columns(2)
                        with col_y:
                            if st.button("✅ Oui, supprimer", key=f"yes_del_{l['id']}", use_container_width=True):
                                supprimer_livraison(l['id'])
                                st.session_state.pop(del_key, None)
                                st.rerun()
                        with col_n:
                            if st.button("❌ Annuler", key=f"no_del_{l['id']}", use_container_width=True):
                                st.session_state.pop(del_key, None)
                                st.rerun()

    with onglet_rec:
        step = st.session_state.get("rec_step", 1)

        # ── ETAPE 1 : BL + Facture + Infos livraison ─────────────
        if step == 1:
            st.markdown('<div class="step-indicator">📋 Etape 1 / 3 — BL + Facture</div>', unsafe_allow_html=True)

            # --- Photo BL ---
            st.markdown("**📷 Photo du Bon de Livraison**")
            photo_bl = st.camera_input("Prends une photo du BL", key="cam_bl")
            if not photo_bl:
                photo_bl = st.file_uploader("Ou importe depuis la galerie (JPG, PNG, PDF)",
                                            type=["jpg","jpeg","png","pdf"], key="up_bl")

            bl_data = st.session_state.get("bl_data", {})
            if photo_bl:
                bl_nom     = getattr(photo_bl, "name", "bl.jpg")
                bl_est_pdf = bl_nom.lower().endswith(".pdf")
                # Memoriser la photo pour la sauvegarder en base
                st.session_state.bl_photo_bytes = photo_bl.getvalue()
                st.session_state.bl_photo_ext   = "pdf" if bl_est_pdf else "jpg"

                if bl_est_pdf:
                    st.info(f"📄 PDF chargé : **{bl_nom}**")
                    st.download_button("👁️ Ouvrir / verifier le PDF", photo_bl.getvalue(),
                                       bl_nom, "application/pdf", key="prev_bl_pdf")
                else:
                    st.image(photo_bl, use_container_width=True)

                if st.button("🤖 Lire le BL automatiquement", key="btn_bl"):
                    with st.spinner("Lecture BL en cours..."):
                        data = lire_bl_pdf(photo_bl.getvalue()) if bl_est_pdf else lire_bl(photo_bl.getvalue())
                    if data and "erreur" not in data:
                        st.session_state.bl_data = data
                        bl_data = data
                        st.success(f"✅ N° BL : {data.get('numero_bl','?')}  •  Fournisseur : {data.get('fournisseur','?')}")
                    else:
                        st.error(data.get("erreur","Erreur lecture BL") if data else "Erreur")

            st.divider()

            # --- Photo / Upload Facture ---
            st.markdown("**📄 Facture (optionnel — ou elle viendra par email)**")
            facture_source = st.radio("Ajouter la facture maintenant ?", ["Non, plus tard", "Photo", "Fichier PDF/image"], horizontal=True, key="fac_source")
            facture_bytes = None
            facture_name  = None

            if facture_source == "Photo":
                fac_photo = st.camera_input("📷 Photo de la facture", key="cam_fac")
                if fac_photo:
                    facture_bytes = fac_photo.getvalue()
                    facture_name  = f"facture_{date.today()}.jpg"
                    st.image(fac_photo, use_container_width=True)

            elif facture_source == "Fichier PDF/image":
                fac_file = st.file_uploader("Importe la facture", type=["pdf","jpg","jpeg","png"], key="up_fac")
                if fac_file:
                    facture_bytes = fac_file.getvalue()
                    facture_name  = fac_file.name

            if facture_bytes:
                st.success(f"✅ Facture prete : {facture_name}")

                # Lecture GPT pour les photos de facture
                is_img_fac = facture_name and facture_name.lower().split(".")[-1] in ("jpg","jpeg","png")
                # Pour les PDFs : extraction de texte automatique (sans GPT)
                is_pdf_fac = facture_name and facture_name.lower().endswith(".pdf")

                if is_img_fac and AI_OK:
                    if st.button("🤖 Lire le N° BL sur la facture", key="btn_lire_fac_rec"):
                        with st.spinner("Lecture de la facture..."):
                            fac_lu = lire_facture_image(facture_bytes)
                        if fac_lu and "erreur" not in fac_lu:
                            parts = []
                            if fac_lu.get("numero_bl"):       parts.append(f"N° BL : **{fac_lu['numero_bl']}**")
                            if fac_lu.get("numero_facture"):  parts.append(f"N° Facture : **{fac_lu['numero_facture']}**")
                            if fac_lu.get("fournisseur"):     parts.append(f"Fournisseur : **{fac_lu['fournisseur']}**")
                            st.info("📝 " + "  •  ".join(parts) if parts else "Rien detecte sur la facture")
                            # Mise a jour de bl_data si pas encore rempli
                            updated = False
                            if fac_lu.get("numero_bl") and not bl_data.get("numero_bl"):
                                bl_data["numero_bl"] = fac_lu["numero_bl"]
                                updated = True
                            if fac_lu.get("fournisseur") and not bl_data.get("fournisseur"):
                                bl_data["fournisseur"] = fac_lu["fournisseur"]
                                updated = True
                            if updated:
                                st.session_state.bl_data = bl_data
                                st.rerun()
                        elif fac_lu:
                            st.error(fac_lu.get("erreur","Erreur"))

                if is_pdf_fac:
                    # Extraction texte automatique (gratuit, sans GPT)
                    pdf_text = extraire_texte_pdf(facture_bytes)
                    if pdf_text:
                        bl_pdf = _extraire_numero_bl(pdf_text)
                        nf_pdf = _extraire_numero_facture(pdf_text)
                        info_parts = []
                        if bl_pdf:  info_parts.append(f"N° BL : **{bl_pdf}**")
                        if nf_pdf:  info_parts.append(f"N° Facture : **{nf_pdf}**")
                        if info_parts:
                            st.info("📄 Lu dans le PDF — " + "  •  ".join(info_parts))
                            if bl_pdf and not bl_data.get("numero_bl"):
                                bl_data["numero_bl"] = bl_pdf
                                st.session_state.bl_data = bl_data
                    # Bouton IA (utile si PDF scanne ou texte non extrait)
                    if AI_OK:
                        if st.button("🤖 Lire la facture avec l'IA", key="btn_lire_fac_pdf"):
                            with st.spinner("Lecture IA de la facture PDF..."):
                                fac_lu = lire_facture_pdf(facture_bytes)
                            if fac_lu and "erreur" not in fac_lu:
                                parts = []
                                if fac_lu.get("numero_bl"):      parts.append(f"N° BL : **{fac_lu['numero_bl']}**")
                                if fac_lu.get("numero_facture"): parts.append(f"N° Facture : **{fac_lu['numero_facture']}**")
                                if fac_lu.get("fournisseur"):    parts.append(f"Fournisseur : **{fac_lu['fournisseur']}**")
                                st.info("📝 " + "  •  ".join(parts) if parts else "Rien detecte")
                                updated = False
                                if fac_lu.get("numero_bl") and not bl_data.get("numero_bl"):
                                    bl_data["numero_bl"] = fac_lu["numero_bl"]; updated = True
                                if fac_lu.get("fournisseur") and not bl_data.get("fournisseur"):
                                    bl_data["fournisseur"] = fac_lu["fournisseur"]; updated = True
                                if updated:
                                    st.session_state.bl_data = bl_data; st.rerun()
                            elif fac_lu:
                                st.error(fac_lu.get("erreur","Erreur"))

            st.divider()

            # --- Formulaire livraison ---
            fournisseurs = get_fournisseurs()
            if not fournisseurs:
                st.warning("Aucun fournisseur. Va dans Config pour en ajouter.")
                st.stop()

            noms = [f["nom"] for f in fournisseurs]
            ids  = [f["id"]  for f in fournisseurs]
            default = 0
            if bl_data.get("fournisseur"):
                for i, n in enumerate(noms):
                    if bl_data["fournisseur"].lower() in n.lower():
                        default = i; break

            with st.form("form_bl"):
                st.markdown("**📝 Informations livraison**")
                fourn_sel  = st.selectbox("Fournisseur", noms, index=default)
                numero_bl  = st.text_input("N° du BL", value=bl_data.get("numero_bl") or "", placeholder="Ex: BL-2024-0051")
                date_rec_default = date.today()
                if bl_data.get("date"):
                    try: date_rec_default = datetime.strptime(bl_data["date"], "%Y-%m-%d").date()
                    except: pass
                date_rec   = st.date_input("Date de reception", value=date_rec_default)
                if st.form_submit_button("✅ Valider → Etape suivante"):
                    fourn_id = ids[noms.index(fourn_sel)]
                    _bl_b64  = base64.b64encode(st.session_state.bl_photo_bytes).decode() if st.session_state.get("bl_photo_bytes") else None
                    _bl_ext  = st.session_state.get("bl_photo_ext", "jpg")
                    db = conn()
                    cur = db.execute(
                        "INSERT INTO livraisons (fournisseur_id,numero_bl,date_reception,photo_bl_b64,photo_bl_ext) VALUES (?,?,?,?,?)",
                        (fourn_id, numero_bl.strip() or None, date_rec, _bl_b64, _bl_ext)
                    )
                    livraison_id = cur.lastrowid
                    db.commit()
                    # Sauvegarde facture si fournie
                    if facture_bytes and facture_name:
                        fac_b64 = base64.b64encode(facture_bytes).decode("utf-8")
                        db.execute(
                            "INSERT INTO factures (livraison_id,nom_fichier,contenu_b64,expediteur,sujet,date_email) VALUES (?,?,?,?,?,?)",
                            (livraison_id, facture_name, fac_b64, "upload_direct", f"Facture BL {numero_bl.strip()}", str(date_rec))
                        )
                        db.commit()
                    db.close()
                    st.session_state.rec_livraison_id   = livraison_id
                    st.session_state.rec_fournisseur_id = fourn_id
                    st.session_state.rec_nb_produits    = 0
                    st.session_state.rec_step           = 2
                    st.rerun()

        # ── ETAPE 2 : Étiquettes produits ────────────────────────
        elif step == 2:
            nb = st.session_state.get("rec_nb_produits", 0)
            st.markdown(f'<div class="step-indicator">🏷️ Etape 2 / 3 — Produits ({nb} enregistre{"s" if nb>1 else ""})</div>', unsafe_allow_html=True)

            if nb > 0:
                st.info(f"✅ {nb} produit(s) deja enregistre(s) — photo de l'etiquette suivante ou termine.")

            photo = st.camera_input("📷 Photo de l'etiquette", key=f"cam_et_{nb}")
            if not photo:
                photo = st.file_uploader("Ou depuis la galerie", type=["jpg","jpeg","png"], key=f"up_et_{nb}")

            etiq = st.session_state.get("etiq_data", {})

            if photo:
                # Memoriser la photo pour la sauvegarder en base
                st.session_state.etiq_photo_bytes = photo.getvalue()
                st.image(photo, use_container_width=True)
                if st.button("🤖 Lire l'etiquette", key=f"btn_et_{nb}"):
                    with st.spinner("Lecture en cours..."):
                        data = lire_etiquette(photo.getvalue())
                    if data and "erreur" not in data:
                        st.session_state.etiq_data = data
                        etiq = data
                        trouves = [k for k,v in data.items() if v and v != "null"]
                        st.success(f"✅ Lu : {', '.join(trouves)}")
                    else:
                        st.error(data.get("erreur","Erreur") if data else "Erreur")

            with st.form("form_produit"):
                nom = st.text_input("Nom du produit", value=etiq.get("nom_produit") or "")
                col_lot, col_qte = st.columns(2)
                with col_lot:
                    lot = st.text_input("N° de lot", value=etiq.get("numero_lot") or "")
                with col_qte:
                    qte = st.text_input("Quantite", placeholder="Ex: 5 kg, 3 caisses, 12 unites")
                dlc_default = date.today()
                if etiq.get("dlc"):
                    try: dlc_default = datetime.strptime(etiq["dlc"], "%Y-%m-%d").date()
                    except: pass
                dlc = st.date_input("DLC", value=dlc_default)

                col_t, col_c = st.columns(2)
                with col_t:
                    temp_prod = st.number_input("🌡️ Temperature (°C)", value=4.0, step=0.5)
                with col_c:
                    conf_prod = st.selectbox("Conformite", ["conforme","non conforme","avec reserve"])
                notes_prod = st.text_area("Notes", placeholder="Aspect, odeur, emballage...")

                col1, col2 = st.columns(2)
                with col1: encore   = st.form_submit_button("💾 + Produit suivant")
                with col2: terminer = st.form_submit_button("✅ Terminer")

                if encore or terminer:
                    if not nom.strip():
                        st.error("Nom obligatoire.")
                    else:
                        _etiq_b64 = base64.b64encode(st.session_state.etiq_photo_bytes).decode() if st.session_state.get("etiq_photo_bytes") else None
                        db = conn()
                        db.execute(
                            "INSERT INTO produits (livraison_id,fournisseur_id,nom,numero_lot,quantite,dlc,temperature,conformite,notes,photo_etiquette_b64) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (st.session_state.rec_livraison_id, st.session_state.rec_fournisseur_id,
                             nom.strip(), lot.strip() or None, qte.strip() or None, dlc, temp_prod, conf_prod, notes_prod or None, _etiq_b64)
                        )
                        db.commit(); db.close()
                        st.session_state.rec_nb_produits += 1
                        st.session_state.pop("etiq_data", None)
                        st.session_state.pop("etiq_photo_bytes", None)
                        if terminer: st.session_state.rec_step = 3
                        st.rerun()

            if st.button("➡️ Terminer sans ajouter de produit"):
                st.session_state.rec_step = 3
                st.rerun()

        # ── ETAPE 3 : Récap dossier BL ───────────────────────────
        elif step == 3:
            st.markdown('<div class="step-indicator">✅ Etape 3 / 3 — Dossier complet</div>', unsafe_allow_html=True)

            liv_id   = st.session_state.rec_livraison_id
            db       = conn()
            livraison = db.execute("""
                SELECT l.*, f.nom AS nom_fourn FROM livraisons l
                JOIN fournisseurs f ON l.fournisseur_id = f.id
                WHERE l.id = ?
            """, (liv_id,)).fetchone()
            db.close()

            if livraison:
                conf_icon = "✅" if livraison['conformite'] == "conforme" else ("⚠️" if livraison['conformite'] == "avec reserve" else "❌")
                st.markdown(f"""<div class="card">
                    <h4>📋 BL : {livraison['numero_bl'] or 'non renseigne'}</h4>
                    <b>Fournisseur :</b> {livraison['nom_fourn']}<br>
                    <b>Date :</b> {livraison['date_reception']}<br>
                    <b>Temperature :</b> {livraison['temperature']}°C<br>
                    <b>Conformite :</b> {conf_icon} {livraison['conformite']}<br>
                    {f"<b>Notes :</b> {livraison['notes']}" if livraison['notes'] else ""}
                </div>""", unsafe_allow_html=True)

            produits = get_produits_livraison(liv_id)
            if produits:
                st.markdown(f"**🏷️ {len(produits)} produit(s) :**")
                for p in produits:
                    lot  = f"LOT: {p['numero_lot']}" if p['numero_lot'] else "Pas de lot"
                    dlc  = f"DLC: {p['dlc']}"        if p['dlc']        else "Pas de DLC"
                    temp = f"🌡️ {p['temperature']}°C" if p['temperature'] is not None else ""
                    conf = p['conformite'] or "conforme"
                    conf_icon = "✅" if conf == "conforme" else ("⚠️" if conf == "avec reserve" else "❌")
                    _qte = p["quantite"] if "quantite" in p.keys() else None
                    qte_badge = f'<span class="badge-qte">📦 {_qte}</span>&nbsp;' if _qte else ""
                    st.markdown(f"""<div class="card" style="margin:0.3rem 0;padding:0.6rem 1rem;">
                        <strong>{p['nom']}</strong><br>
                        <span class="badge-lot">{lot}</span>&nbsp;
                        <span class="badge-dlc">{dlc}</span>&nbsp;
                        {qte_badge}
                        {f'<small>{temp}</small>' if temp else ''}
                        <small> {conf_icon} {conf}</small>
                        {f'<br><small><i>{p["notes"]}</i></small>' if p['notes'] else ''}
                    </div>""", unsafe_allow_html=True)

            factures = get_factures(liv_id)
            if factures:
                st.markdown(f"**📎 {len(factures)} facture(s) liee(s)**")
                for fac in factures:
                    mime = "application/pdf" if fac['nom_fichier'].endswith(".pdf") else "image/jpeg"
                    st.download_button(f"📥 {fac['nom_fichier']}", base64.b64decode(fac['contenu_b64']),
                                       fac['nom_fichier'], mime, key=f"recap_dl_{fac['id']}")
            else:
                st.caption("Pas de facture liee — elle viendra automatiquement depuis Gmail si le N° BL correspond.")

            st.success("🎉 Dossier HACCP complet !")
            if st.button("📦 Nouvelle reception", use_container_width=True):
                for k in ["rec_step","rec_livraison_id","rec_fournisseur_id","rec_nb_produits","bl_data","etiq_data"]:
                    st.session_state.pop(k, None)
                st.rerun()

# ═══════════════════════════════════════════════════════════════
#  PAGE : PREPARATION
# ═══════════════════════════════════════════════════════════════
def page_preparation():
    st.header("👨‍🍳 Nouvelle preparation")

    fournisseurs = get_fournisseurs()
    if not fournisseurs:
        st.info("Fais d'abord une reception !")
        return

    options = ["Tous"] + [f["nom"] for f in fournisseurs]
    filtre  = st.selectbox("Filtrer par fournisseur", options)
    produits = get_produits() if filtre == "Tous" else get_produits(
        next(f["id"] for f in fournisseurs if f["nom"] == filtre))

    if not produits:
        st.info("Aucun produit — fais d'abord une reception !")
        return

    st.subheader("Coche les produits utilises")
    selectionnes = []
    for p in produits:
        lot = f"LOT: {p['numero_lot']}" if p['numero_lot'] else "Pas de lot"
        dlc = f"DLC: {p['dlc']}"        if p['dlc']        else "Pas de DLC"
        if st.checkbox(f"{p['nom']}  —  {lot}  —  {dlc}", key=f"pp_{p['id']}"):
            selectionnes.append(p["id"])

    if selectionnes:
        st.divider()
        with st.form("form_prep"):
            d = st.date_input("Date", value=date.today())
            h = st.time_input("Heure", value=datetime.now().time())
            n = st.text_area("Notes", placeholder="Description...")
            if st.form_submit_button("✅ Enregistrer"):
                db = conn()
                cur = db.execute("INSERT INTO preparations (date_prep,heure_prep,notes) VALUES (?,?,?)",
                                  (d, str(h)[:5], n))
                for pid in selectionnes:
                    db.execute("INSERT INTO preparation_produits (preparation_id,produit_id) VALUES (?,?)",
                                (cur.lastrowid, pid))
                db.commit(); db.close()
                st.success(f"✅ Preparation enregistree avec {len(selectionnes)} produit(s) !")
                st.rerun()

# ═══════════════════════════════════════════════════════════════
#  PAGE : TRACABILITE
# ═══════════════════════════════════════════════════════════════
def page_tracabilite():
    st.header("🔍 Tracabilite")
    tab1, tab2 = st.tabs(["🔎 Recherche par lot", "📋 Tous les produits"])

    with tab1:
        lot_q = st.text_input("Numero de lot", placeholder="Ex: L240051")
        if lot_q:
            resultats = rechercher_lot(lot_q)
            if not resultats:
                st.warning(f"Aucun resultat pour << {lot_q} >>")
            else:
                for r in resultats:
                    with st.expander(f"📦 {r['nom']}  —  LOT: {r['numero_lot'] or '?'}", expanded=True):
                        col1, col2 = st.columns(2)
                        with col1:
                            st.markdown("**Produit**")
                            st.write(f"Nom : {r['nom']}")
                            st.write(f"Lot : {r['numero_lot'] or '—'}")
                            st.write(f"DLC : {r['dlc'] or '—'}")
                        with col2:
                            st.markdown("**Livraison**")
                            st.write(f"Fournisseur : {r['fourn']}")
                            if r['numero_bl']:
                                st.write(f"N° BL : {r['numero_bl']}")
                            st.write(f"Reception : {r['date_reception'] or '—'}")
                            if r['temperature'] is not None:
                                st.write(f"Temperature : {r['temperature']}°C")
                            st.write(f"Conformite : {r['conformite'] or '—'}")
                        st.markdown("**Preparation**")
                        if r['date_prep']:
                            st.success(f"✅ Prepare le {r['date_prep']} a {r['heure_prep']}")
                            if r['prep_notes']: st.write(f"Notes : {r['prep_notes']}")
                        else:
                            st.info("Pas encore utilise en preparation.")

    with tab2:
        fournisseurs = get_fournisseurs()
        filtre = st.selectbox("Fournisseur", ["Tous"] + [f["nom"] for f in fournisseurs], key="tr_f")
        produits = get_produits() if filtre == "Tous" else get_produits(
            next(f["id"] for f in fournisseurs if f["nom"] == filtre))
        for p in produits:
            _qte = p["quantite"] if "quantite" in p.keys() else None
            qte_badge = f'<span class="badge-qte">📦 {_qte}</span>&nbsp;' if _qte else ""
            st.markdown(f"""<div class="card">
                <strong>{p['nom']}</strong><br>
                <span class="badge-lot">LOT: {p['numero_lot'] or '—'}</span>&nbsp;
                <span class="badge-dlc">DLC: {p['dlc'] or '—'}</span>&nbsp;
                {qte_badge}<br>
                <small>🏭 {p['fourn']} • Recu le {p['date_reception'] or '?'}</small>
            </div>""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════
#  PAGE : FACTURES
# ═══════════════════════════════════════════════════════════════
def page_factures():
    st.header("📧 Factures")

    EMAIL_OK = ("EMAIL_ADDRESS" in st.secrets and "EMAIL_APP_PASSWORD" in st.secrets)

    if not EMAIL_OK:
        st.warning("⚙️ Email non configure")
        st.info("Va dans l'onglet **Config** → section Email pour voir les instructions.")
        return

    # ── Factures deja enregistrees ──────────────────────────────
    factures_all = get_factures()

    # Barre de recherche
    search = st.text_input("🔍 Rechercher une facture",
                           placeholder="Fournisseur, N° facture, sujet, date...",
                           key="fac_search")

    if factures_all:
        factures_filtrees = factures_all
        if search:
            s = search.lower()
            factures_filtrees = [f for f in factures_all if
                                  s in (f["nom_fichier"] or "").lower() or
                                  s in (f["expediteur"]  or "").lower() or
                                  s in (f["sujet"]       or "").lower() or
                                  s in (f["nom_fourn"]   or "").lower() or
                                  s in (f["numero_bl"]   or "").lower() or
                                  s in (f["date_email"]  or "").lower()]

        st.subheader(f"📎 {len(factures_filtrees)} / {len(factures_all)} facture(s) enregistree(s)")

        # Liste des BLs pour le selecteur de lien
        livraisons_lv = get_livraisons()
        lv_labels_saved = ["-- Aucun --"]
        lv_ids_saved    = [None]
        for lv in livraisons_lv:
            bl_txt = f" · BL {lv['numero_bl']}" if lv['numero_bl'] else ""
            lv_labels_saved.append(f"{lv['nom_fourn']} — {lv['date_reception']}{bl_txt}")
            lv_ids_saved.append(lv['id'])

        for f in factures_filtrees:
            bl_label = f"BL {f['numero_bl'] or '#'+str(f['livraison_id'])} — {f['nom_fourn'] or '?'}" if f['livraison_id'] else "⚠️ Non liee"
            with st.expander(f"📄 {f['nom_fichier']}  •  {bl_label}"):
                st.caption(f"De : {f['expediteur']}")
                st.caption(f"Sujet : {f['sujet']}")
                st.caption(f"Date email : {f['date_email']}")
                col1, col2 = st.columns(2)
                with col1:
                    _nom = f['nom_fichier'] or ""
                    if _nom.endswith(".pdf"):
                        mime = "application/pdf"
                    elif _nom.endswith(".txt"):
                        mime = "text/plain"
                    else:
                        mime = "image/jpeg"
                    _b64 = f['contenu_b64']
                    _bytes = base64.b64decode(_b64) if _b64 else b""
                    st.download_button(
                        "📥 Telecharger",
                        _bytes,
                        f['nom_fichier'], mime,
                        key=f"dl_{f['id']}",
                        use_container_width=True
                    )
                with col2:
                    if st.button("🗑️ Supprimer", key=f"del_{f['id']}", use_container_width=True):
                        supprimer_facture(f['id'])
                        st.rerun()

                # Sélecteur de lien BL (toujours visible, pré-sélectionne le BL actuel)
                cur_idx = lv_ids_saved.index(f['livraison_id']) if f['livraison_id'] in lv_ids_saved else 0
                new_lv = st.selectbox("🔗 Lier a un BL", lv_labels_saved, index=cur_idx,
                                      key=f"relink_sel_{f['id']}")
                if st.button("💾 Mettre a jour le lien", key=f"relink_btn_{f['id']}", use_container_width=True):
                    new_lid = lv_ids_saved[lv_labels_saved.index(new_lv)]
                    lier_facture_a_livraison(f['id'], new_lid)
                    st.success("✅ Lien mis a jour !")
                    st.rerun()
        st.divider()

    # ── Synchronisation Gmail ────────────────────────────────────
    st.subheader("🔄 Synchroniser depuis Gmail")
    col1, col2 = st.columns([3, 1])
    with col1:
        jours = st.slider("Chercher dans les derniers X jours", 7, 365, 30, key="fac_jours")
    with col2:
        sync = st.button("🔄 Actualiser", use_container_width=True, key="btn_sync")

    if sync:
        with st.spinner("Connexion a Gmail en cours..."):
            result = fetch_factures_gmail(jours)
        if isinstance(result, dict) and "erreur" in result:
            st.error(f"❌ {result['erreur']}")
            return
        st.session_state.factures_found = result
        st.session_state.factures_jours = jours

    found = st.session_state.get("factures_found", [])
    if not found:
        if sync:
            st.info("Aucun fichier (PDF/image) trouve dans les emails de cette periode.")
        return

    # Filtres
    col_a, col_b = st.columns(2)
    with col_a:
        show_all = st.checkbox("Tout afficher (pas seulement fournisseurs)", value=True)
    with col_b:
        st.caption(f"{len(found)} fichier(s) au total")

    filtered = found if show_all else [x for x in found if x["is_fourn"] or x["is_invoice"]]
    if not filtered:
        st.info("Aucun fichier trouve.")
        return

    st.write(f"**{len(filtered)} fichier(s) detecte(s) comme factures/BL**")

    # Liste des livraisons pour le lien
    livraisons = get_livraisons()
    lv_labels = ["-- Ne pas lier --"]
    lv_ids    = [None]
    for l in livraisons:
        bl = f" (BL {l['numero_bl']})" if l['numero_bl'] else ""
        lv_labels.append(f"{l['nom_fourn']} — {l['date_reception']}{bl}")
        lv_ids.append(l['id'])

    # Bouton "Tout enregistrer automatiquement"
    auto_matches = [f for f in filtered if f.get("livraison_id_auto") or f.get("fourn_id")]
    if auto_matches:
        st.info(f"🤖 {len(auto_matches)} facture(s) avec correspondance automatique detectee")
        if st.button("⚡ Tout enregistrer automatiquement", use_container_width=True, key="btn_auto_all"):
            for f in auto_matches:
                sauvegarder_facture(
                    f.get("livraison_id_auto"),
                    f["filename"], f["content_b64"],
                    f["sender"], f["subject"], f["date"]
                )
            st.success(f"✅ {len(auto_matches)} facture(s) enregistree(s) automatiquement !")
            st.session_state.factures_found = [x for x in found if x not in auto_matches]
            st.rerun()
        st.divider()

    for i, f in enumerate(filtered):
        # Badge selon detection
        if f.get("auto_downloaded"):
            tag = "⚡ PDF telecharge auto"
        elif f.get("lien") and f.get("ext") == "txt":
            tag = "🔗 Facture via lien"
        elif f.get("fourn_nom"):
            tag = f"✅ {f['fourn_nom']}"
        elif f["is_invoice"]:
            tag = "📄 Facture"
        else:
            tag = "📎 Fichier"

        num_fac_label = f"N° {f['num_facture']}  •  " if f.get("num_facture") else ""
        bl_auto = f"→ BL : {f['num_bl_email']}" if f.get("num_bl_email") else ""
        with st.expander(f"{tag}  •  {num_fac_label}{f['filename']}  {bl_auto}"):
            st.caption(f"De : {f['sender']}")
            st.caption(f"Sujet : {f['subject']}")
            st.caption(f"Date : {f['date']}")
            if f.get("lien"):
                st.markdown(f"📥 **[Ouvrir la facture]({f['lien']})**")
            # Bouton PROGINOV pour toute facture via lien quand credentials configures
            PROG_OK = ("PROGINOV_LOGIN" in st.secrets and "PROGINOV_PASSWORD" in st.secrets)
            if f.get("ext") == "txt" and PROG_OK:
                lien_pour_prog = f.get("lien") or ""
                if st.button("⚡ Telecharger PDF via PROGINOV", key=f"prog_{i}", use_container_width=True):
                    with st.spinner("Connexion a PROGINOV..."):
                        pdf_b64, err = telecharger_proginov(lien_pour_prog) if lien_pour_prog else (None, "Pas de lien disponible")
                    if pdf_b64:
                        fname_pdf = f["filename"].replace(".txt", ".pdf")
                        st.success("✅ PDF recupere !")
                        st.download_button("📥 Telecharger le PDF", base64.b64decode(pdf_b64),
                                           fname_pdf, "application/pdf", key=f"dl_prog_{i}")
                        # Sauvegarde directement
                        lid = lv_ids[lv_labels.index(st.session_state.get(f"lv_{i}_val", lv_labels[0]))] if lv_labels else None
                        sauvegarder_facture(lid, fname_pdf, pdf_b64, f["sender"], f["subject"], f["date"])
                        st.session_state.factures_found = [x for x in found if x != f]
                        st.rerun()
                    else:
                        st.error(f"Erreur : {err}")

            # Bouton lecture facture image avec GPT (images uniquement)
            if f.get("ext") in ("jpg", "jpeg", "png") and AI_OK:
                if st.button("🤖 Lire N° BL sur cette facture", key=f"read_fac_{i}", use_container_width=True):
                    with st.spinner("Lecture GPT de la facture..."):
                        fac_bytes = base64.b64decode(f["content_b64"])
                        fac_data  = lire_facture_image(fac_bytes)
                    if fac_data and "erreur" not in fac_data:
                        detected_bl  = fac_data.get("numero_bl")
                        detected_nf  = fac_data.get("numero_facture")
                        detected_fou = fac_data.get("fournisseur")
                        msg_parts = []
                        if detected_bl:  msg_parts.append(f"N° BL : **{detected_bl}**")
                        if detected_nf:  msg_parts.append(f"N° Facture : **{detected_nf}**")
                        if detected_fou: msg_parts.append(f"Fournisseur : **{detected_fou}**")
                        st.success("✅ " + "  •  ".join(msg_parts) if msg_parts else "Rien detecte")
                        if detected_bl:
                            # Met a jour l'entree dans la liste et relance pour re-matcher le BL
                            idx_f = next((k for k, x in enumerate(found) if x is f), -1)
                            if idx_f >= 0:
                                found[idx_f]["num_bl_email"] = detected_bl
                                if detected_nf: found[idx_f]["num_facture"] = detected_nf
                                # Re-cherche le BL correspondant
                                for lv in livraisons:
                                    if lv["numero_bl"] and detected_bl.lower() in lv["numero_bl"].lower():
                                        found[idx_f]["livraison_id_auto"] = lv["id"]
                                        break
                                st.session_state.factures_found = found
                                st.rerun()
                    elif fac_data:
                        st.error(f"Erreur GPT : {fac_data.get('erreur')}")

            # Indicateur si BL detecte depuis le PDF (extraction automatique)
            if f.get("ext") == "pdf" and f.get("num_bl_email"):
                st.info(f"📄 N° BL detecte dans le PDF : **{f['num_bl_email']}**")

            # Pre-selection automatique du BL
            default_idx = 0
            if f.get("livraison_id_auto") and f["livraison_id_auto"] in lv_ids:
                default_idx = lv_ids.index(f["livraison_id_auto"])
            elif f.get("fourn_id"):
                # Pre-selectionner le premier BL de ce fournisseur
                for li, l in enumerate(livraisons):
                    db3 = conn()
                    fid = db3.execute("SELECT fournisseur_id FROM livraisons WHERE id=?", (l["id"],)).fetchone()
                    db3.close()
                    if fid and fid[0] == f["fourn_id"]:
                        if l["id"] in lv_ids:
                            default_idx = lv_ids.index(l["id"])
                        break

            if default_idx > 0:
                st.success(f"🤖 Correspondance auto : {lv_labels[default_idx]}")

            lien_sel = st.selectbox("Lier a un BL", lv_labels, index=default_idx, key=f"lv_{i}")
            if st.button("💾 Enregistrer", key=f"save_{i}", use_container_width=True):
                lid = lv_ids[lv_labels.index(lien_sel)]
                sauvegarder_facture(lid, f['filename'], f['content_b64'], f['sender'], f['subject'], f['date'])
                st.success(f"✅ '{f['filename']}' enregistree !")
                st.session_state.factures_found = [x for x in found if x != f]
                st.rerun()

# ═══════════════════════════════════════════════════════════════
#  PAGE : CONFIG
# ═══════════════════════════════════════════════════════════════
def page_config():
    st.header("⚙️ Configuration")

    st.subheader("Fournisseurs")
    for f in get_fournisseurs():
        st.write(f"• {f['nom']}")

    with st.form("add_fourn"):
        nouveau = st.text_input("Ajouter un fournisseur")
        if st.form_submit_button("Ajouter"):
            if nouveau.strip():
                if ajouter_fournisseur(nouveau.strip()):
                    st.success(f"✅ {nouveau} ajoute !"); st.rerun()
                else:
                    st.error("Fournisseur existe deja.")

    st.divider()
    st.subheader("🤖 IA")
    if AI_OK:
        st.success("✅ GPT-4o-mini connecte — lecture automatique active !")
    else:
        st.error("❌ Cle OpenAI manquante — ajoute OPENAI_API_KEY dans Secrets")

    st.divider()
    st.subheader("🏢 PROGINOV (Relais d'Or)")
    PROG_OK = ("PROGINOV_LOGIN" in st.secrets and "PROGINOV_PASSWORD" in st.secrets)
    if PROG_OK:
        st.success("✅ PROGINOV configure — telechargement auto des factures Relais d'Or actif !")
    else:
        st.warning("❌ PROGINOV non configure")
        st.markdown("""
Dans **Streamlit Secrets**, ajoute :
```
PROGINOV_LOGIN = "ton_identifiant_proginov"
PROGINOV_PASSWORD = "ton_mot_de_passe_proginov"
```
        """)

    st.divider()
    st.subheader("📬 Filtrage emails Gmail")
    kw_actuel = get_config("gmail_keywords", "sysco,gda,relais d'or,krill")
    st.caption("Mots-cles fournisseurs (separes par virgule) — seuls les emails contenant l'un de ces mots seront synchronises.")
    with st.form("form_kw_gmail"):
        new_kw = st.text_input("Mots-cles", value=kw_actuel,
                               placeholder="sysco,gda,relais d'or,krill")
        if st.form_submit_button("💾 Sauvegarder les filtres"):
            set_config("gmail_keywords", new_kw.strip())
            # Remet a zero l'auto-sync pour qu'il se relance avec les nouveaux filtres
            st.session_state.pop("factures_auto_synced", None)
            st.success("✅ Filtres mis a jour ! La prochaine synchro utilisera ces mots-cles.")

    st.divider()
    st.subheader("📧 Email (Factures Gmail)")
    EMAIL_OK = ("EMAIL_ADDRESS" in st.secrets and "EMAIL_APP_PASSWORD" in st.secrets)
    if EMAIL_OK:
        st.success("✅ Gmail connecte — synchronisation des factures active !")
    else:
        st.warning("❌ Email non configure")
        st.markdown("""
**Pour connecter ton Gmail, 2 etapes :**

**1. Active la validation en 2 etapes** (si pas deja fait)
→ [myaccount.google.com](https://myaccount.google.com) → Securite → Validation en 2 etapes

**2. Cree un mot de passe d'application**
→ [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
→ Nom : "FoodTruck" → Generer
→ Copie le code a **16 caracteres**

**3. Dans Streamlit Secrets, ajoute ces 2 lignes :**
```
EMAIL_ADDRESS = "ton.adresse@gmail.com"
EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
```
        """)

    st.divider()
    st.subheader("💾 Sauvegarde")
    db = conn()
    df_produits = pd.read_sql_query("""
        SELECT p.id, p.nom, p.numero_lot, p.dlc, f.nom AS fournisseur,
               l.date_reception, l.temperature, l.conformite, p.created_at
        FROM produits p JOIN fournisseurs f ON p.fournisseur_id=f.id
        LEFT JOIN livraisons l ON p.livraison_id=l.id ORDER BY p.created_at DESC
    """, db)
    df_preps = pd.read_sql_query("""
        SELECT prep.id, prep.date_prep, prep.heure_prep,
               p.nom AS produit, p.numero_lot, f.nom AS fournisseur, prep.notes
        FROM preparations prep
        JOIN preparation_produits pp ON prep.id=pp.preparation_id
        JOIN produits p ON pp.produit_id=p.id
        JOIN fournisseurs f ON p.fournisseur_id=f.id ORDER BY prep.date_prep DESC
    """, db)
    db.close()
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("📥 Export Produits", df_produits.to_csv(index=False).encode("utf-8"),
                           f"produits_{date.today()}.csv", "text/csv", use_container_width=True)
    with col2:
        st.download_button("📥 Export Preparations", df_preps.to_csv(index=False).encode("utf-8"),
                           f"preparations_{date.today()}.csv", "text/csv", use_container_width=True)
    st.caption("v2.0 — FoodTruck Tracabilite HACCP")

# ═══════════════════════════════════════════════════════════════
#  NAVIGATION
# ═══════════════════════════════════════════════════════════════
def main():
    t1, t2, t3, t4, t5 = st.tabs(["📦 Reception", "👨‍🍳 Prepa", "🔍 Traca", "📧 Factures", "⚙️ Config"])
    with t1: page_reception()
    with t2: page_preparation()
    with t3: page_tracabilite()
    with t4: page_factures()
    with t5: page_config()

if __name__ == "__main__":
    main()
