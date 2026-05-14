import streamlit as st
import sqlite3
import json
import base64
import requests
from datetime import datetime, date
from PIL import Image, ImageEnhance
import io
import pandas as pd

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
    """)
    db.commit()
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
            l.date_reception, l.temperature, l.conformite,
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

# ═══════════════════════════════════════════════════════════════
#  PAGE : RECEPTION
# ═══════════════════════════════════════════════════════════════
def page_reception():
    st.header("📦 Reception livraison")
    step = st.session_state.get("rec_step", 1)

    if step == 1:
        st.markdown('<div class="step-indicator">Etape 1 / 3 — Bon de livraison</div>', unsafe_allow_html=True)
        photo = st.camera_input("📷 Photo du BL", key="cam_bl")
        if not photo:
            photo = st.file_uploader("Ou importe depuis la galerie", type=["jpg","jpeg","png"], key="up_bl")

        bl_data = st.session_state.get("bl_data", {})

        if photo:
            st.image(photo, use_container_width=True)
            if st.button("🤖 Lire automatiquement", key="btn_bl"):
                with st.spinner("Lecture en cours..."):
                    data = lire_bl(photo.getvalue())
                if data and "erreur" not in data:
                    st.session_state.bl_data = data
                    bl_data = data
                    st.success("✅ BL lu !")
                else:
                    st.error(data.get("erreur","Erreur") if data else "Erreur")

        fournisseurs = get_fournisseurs()
        if not fournisseurs:
            st.warning("Aucun fournisseur. Va dans Config pour en ajouter.")
            return

        noms = [f["nom"] for f in fournisseurs]
        ids  = [f["id"]  for f in fournisseurs]
        default = 0
        if bl_data.get("fournisseur"):
            for i, n in enumerate(noms):
                if bl_data["fournisseur"].lower() in n.lower():
                    default = i; break

        with st.form("form_bl"):
            fourn_sel  = st.selectbox("Fournisseur", noms, index=default)
            date_rec   = st.date_input("Date de reception", value=date.today())
            temp       = st.number_input("Température (°C)", value=4.0, step=0.5)
            conformite = st.selectbox("Conformite", ["conforme","non conforme","avec reserve"])
            notes      = st.text_area("Notes", placeholder="Remarques...")
            if st.form_submit_button("✅ Valider →"):
                fourn_id = ids[noms.index(fourn_sel)]
                db = conn()
                cur = db.execute(
                    "INSERT INTO livraisons (fournisseur_id,date_reception,temperature,conformite,notes) VALUES (?,?,?,?,?)",
                    (fourn_id, date_rec, temp, conformite, notes)
                )
                db.commit()
                st.session_state.rec_livraison_id   = cur.lastrowid
                st.session_state.rec_fournisseur_id = fourn_id
                st.session_state.rec_nb_produits    = 0
                st.session_state.rec_step           = 2
                db.close()
                st.rerun()

    elif step == 2:
        nb = st.session_state.get("rec_nb_produits", 0)
        st.markdown(f'<div class="step-indicator">Etape 2 / 3 — Produits ({nb} enregistre{"s" if nb>1 else ""})</div>', unsafe_allow_html=True)

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

            col1, col2 = st.columns(2)
            with col1: encore   = st.form_submit_button("💾 Enregistrer + suivant")
            with col2: terminer = st.form_submit_button("✅ Terminer")

            if encore or terminer:
                if not nom.strip():
                    st.error("Nom obligatoire.")
                else:
                    db = conn()
                    db.execute(
                        "INSERT INTO produits (livraison_id,fournisseur_id,nom,numero_lot,dlc) VALUES (?,?,?,?,?)",
                        (st.session_state.rec_livraison_id, st.session_state.rec_fournisseur_id,
                         nom.strip(), lot.strip() or None, dlc)
                    )
                    db.commit(); db.close()
                    st.session_state.rec_nb_produits += 1
                    st.session_state.pop("etiq_data", None)
                    if terminer: st.session_state.rec_step = 3
                    st.rerun()

        if st.button("➡️ Terminer sans ajouter"):
            st.session_state.rec_step = 3
            st.rerun()

    elif step == 3:
        nb = st.session_state.get("rec_nb_produits", 0)
        st.success(f"✅ Livraison #{st.session_state.rec_livraison_id} — {nb} produit(s) enregistre(s) !")
        if st.button("📦 Nouvelle reception"):
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
    t1, t2, t3, t4 = st.tabs(["📦 Reception", "👨‍🍳 Prepa", "🔍 Traca", "⚙️ Config"])
    with t1: page_reception()
    with t2: page_preparation()
    with t3: page_tracabilite()
    with t4: page_config()

if __name__ == "__main__":
    main()
