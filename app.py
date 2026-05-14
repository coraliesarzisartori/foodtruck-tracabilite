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
    """)
    db.commit()
    # Migrations colonnes
    for migration in [
        "ALTER TABLE livraisons ADD COLUMN numero_bl TEXT",
        "ALTER TABLE produits ADD COLUMN temperature REAL",
        "ALTER TABLE produits ADD COLUMN conformite TEXT DEFAULT 'conforme'",
        "ALTER TABLE produits ADD COLUMN notes TEXT",
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
        mail.select("INBOX")

        since = (datetime.now() - timedelta(days=jours)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'SINCE {since}')
        if status != "OK" or not data[0]:
            mail.logout()
            return []

        ids = data[0].split()
        fournisseurs    = get_fournisseurs()
        livraisons      = get_livraisons()
        keywords_invoice = ["facture", "invoice", "bon de livraison", "bl ", "commande", "livraison", "avoir"]
        found = []

        for eid in ids[-150:]:
            try:
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                subject  = _decode_str(msg.get("Subject", ""))
                sender   = _decode_str(msg.get("From", ""))
                date_str = msg.get("Date", "")

                text_low   = (subject + " " + sender).lower()
                is_invoice = any(k in text_low for k in keywords_invoice)

                # Detection automatique fournisseur
                fourn_id, fourn_nom = _trouver_fourn(text_low, fournisseurs)
                is_fourn = fourn_id is not None

                # Detection automatique numero BL
                num_bl_email = _extraire_numero_bl(subject) or _extraire_numero_bl(sender)

                # Detection automatique livraison correspondante
                livraison_id_auto = _trouver_livraison(num_bl_email, fourn_id, livraisons)

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
                        "num_bl_email":     num_bl_email,
                        "livraison_id_auto":livraison_id_auto,
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

                    produits = get_produits_livraison(l['id'])
                    if produits:
                        st.markdown(f"**{len(produits)} produit(s) :**")
                        for p in produits:
                            lot  = f"LOT: {p['numero_lot']}" if p['numero_lot'] else "Pas de lot"
                            dlc  = f"DLC: {p['dlc']}"        if p['dlc']        else "Pas de DLC"
                            temp = f"🌡️ {p['temperature']}°C" if p['temperature'] is not None else ""
                            conf = p['conformite'] or "conforme"
                            conf_icon = "✅" if conf == "conforme" else ("⚠️" if conf == "avec reserve" else "❌")
                            st.markdown(f"""<div class="card" style="margin:0.2rem 0;padding:0.5rem 1rem;">
                                <strong>{p['nom']}</strong><br>
                                <span class="badge-lot">{lot}</span>&nbsp;
                                <span class="badge-dlc">{dlc}</span>&nbsp;
                                {f'<small>{temp}</small>' if temp else ''}
                                <small> {conf_icon} {conf}</small>
                                {f'<br><small><i>{p["notes"]}</i></small>' if p['notes'] else ''}
                            </div>""", unsafe_allow_html=True)
                    else:
                        st.info("Aucun produit pour cette livraison.")

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

    with onglet_rec:
        step = st.session_state.get("rec_step", 1)

        # ── ETAPE 1 : BL + Facture + Infos livraison ─────────────
        if step == 1:
            st.markdown('<div class="step-indicator">📋 Etape 1 / 3 — BL + Facture</div>', unsafe_allow_html=True)

            # --- Photo BL ---
            st.markdown("**📷 Photo du Bon de Livraison**")
            photo_bl = st.camera_input("Prends une photo du BL", key="cam_bl")
            if not photo_bl:
                photo_bl = st.file_uploader("Ou importe depuis la galerie", type=["jpg","jpeg","png"], key="up_bl")

            bl_data = st.session_state.get("bl_data", {})
            if photo_bl:
                st.image(photo_bl, use_container_width=True)
                if st.button("🤖 Lire le BL automatiquement", key="btn_bl"):
                    with st.spinner("Lecture BL en cours..."):
                        data = lire_bl(photo_bl.getvalue())
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
                    db = conn()
                    cur = db.execute(
                        "INSERT INTO livraisons (fournisseur_id,numero_bl,date_reception) VALUES (?,?,?)",
                        (fourn_id, numero_bl.strip() or None, date_rec)
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
                lot = st.text_input("N° de lot",       value=etiq.get("numero_lot")  or "")
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
                        db = conn()
                        db.execute(
                            "INSERT INTO produits (livraison_id,fournisseur_id,nom,numero_lot,dlc,temperature,conformite,notes) VALUES (?,?,?,?,?,?,?,?)",
                            (st.session_state.rec_livraison_id, st.session_state.rec_fournisseur_id,
                             nom.strip(), lot.strip() or None, dlc, temp_prod, conf_prod, notes_prod or None)
                        )
                        db.commit(); db.close()
                        st.session_state.rec_nb_produits += 1
                        st.session_state.pop("etiq_data", None)
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
                    st.markdown(f"""<div class="card" style="margin:0.3rem 0;padding:0.6rem 1rem;">
                        <strong>{p['nom']}</strong><br>
                        <span class="badge-lot">{lot}</span>&nbsp;
                        <span class="badge-dlc">{dlc}</span>&nbsp;
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
            st.markdown(f"""<div class="card">
                <strong>{p['nom']}</strong><br>
                <span class="badge-lot">LOT: {p['numero_lot'] or '—'}</span>&nbsp;
                <span class="badge-dlc">DLC: {p['dlc'] or '—'}</span><br>
                <small>📦 {p['fourn']} • Recu le {p['date_reception'] or '?'}</small>
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
    factures = get_factures()
    if factures:
        st.subheader(f"📎 {len(factures)} facture(s) enregistree(s)")
        for f in factures:
            bl_label = f"BL #{f['livraison_id']} — {f['nom_fourn'] or '?'}" if f['livraison_id'] else "Non liee"
            with st.expander(f"📄 {f['nom_fichier']}  •  {bl_label}"):
                st.caption(f"De : {f['expediteur']}")
                st.caption(f"Sujet : {f['sujet']}")
                st.caption(f"Date email : {f['date_email']}")
                col1, col2 = st.columns(2)
                with col1:
                    mime = "application/pdf" if f['nom_fichier'].endswith(".pdf") else "image/jpeg"
                    st.download_button(
                        "📥 Telecharger",
                        base64.b64decode(f['contenu_b64']),
                        f['nom_fichier'], mime,
                        key=f"dl_{f['id']}",
                        use_container_width=True
                    )
                with col2:
                    if st.button("🗑️ Supprimer", key=f"del_{f['id']}", use_container_width=True):
                        supprimer_facture(f['id'])
                        st.rerun()
        st.divider()

    # ── Synchronisation Gmail ────────────────────────────────────
    st.subheader("🔄 Synchroniser depuis Gmail")
    col1, col2 = st.columns([3, 1])
    with col1:
        jours = st.slider("Chercher dans les derniers X jours", 7, 90, 30, key="fac_jours")
    with col2:
        sync = st.button("🔄 Lancer", use_container_width=True, key="btn_sync")

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
        show_all = st.checkbox("Tout afficher (pas seulement fournisseurs)", value=False)
    with col_b:
        st.caption(f"{len(found)} fichier(s) au total")

    filtered = found if show_all else [x for x in found if x["is_fourn"] or x["is_invoice"]]
    if not filtered:
        st.info("Aucune facture detectee. Coche 'Tout afficher' pour voir tous les fichiers.")
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
        if f.get("fourn_nom"):
            tag = f"✅ {f['fourn_nom']}"
        elif f["is_invoice"]:
            tag = "📄 Facture"
        else:
            tag = "📎 Fichier"

        bl_auto = f"→ BL detecte : {f['num_bl_email']}" if f.get("num_bl_email") else ""
        with st.expander(f"{tag}  •  {f['filename']}  {bl_auto}"):
            st.caption(f"De : {f['sender']}")
            st.caption(f"Sujet : {f['subject']}")
            st.caption(f"Date : {f['date']}")

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
