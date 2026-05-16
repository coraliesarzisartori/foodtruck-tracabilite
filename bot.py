"""
Bot Telegram HACCP — FoodTruck Coralie
Connecté à la même base Neon que l'app Streamlit.
"""
import os, base64, json, io, logging, requests
import psycopg2, psycopg2.extras
from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
BOT_TOKEN    = os.environ["BOT_TOKEN"]

# ─── Base de données ────────────────────────────────────────────
def db_query(sql, params=None, fetch=None):
    sql = sql.replace("?", "%s")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if fetch == "one":  return cur.fetchone()
        if fetch == "all":  return cur.fetchall()
        if "RETURNING" in sql.upper():
            row = cur.fetchone()
            conn.commit()
            return row
        conn.commit()
    finally:
        conn.close()

def get_fournisseurs():
    return db_query("SELECT * FROM fournisseurs ORDER BY nom", fetch="all") or []

def get_plats():
    return db_query("SELECT * FROM plats ORDER BY nom", fetch="all") or []

def get_livraisons_recent(n=5):
    return db_query("""
        SELECT l.id, l.numero_bl, l.date_reception,
               f.nom AS nom_fourn,
               COUNT(p.id) AS nb_produits
        FROM livraisons l
        JOIN fournisseurs f ON l.fournisseur_id = f.id
        LEFT JOIN produits p ON p.livraison_id = l.id
        GROUP BY l.id, l.numero_bl, l.date_reception, f.nom
        ORDER BY l.date_reception DESC LIMIT %s
    """, (n,), fetch="all") or []

# ─── GPT ────────────────────────────────────────────────────────
def _gpt(prompt, image_bytes=None):
    if not OPENAI_KEY:
        return {}
    content = [{"type": "text", "text": prompt}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        content.append({"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}})
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": content}],
              "max_tokens": 400},
        timeout=30
    )
    txt = resp.json()["choices"][0]["message"]["content"].strip()
    for sep in ["```json", "```"]:
        if sep in txt:
            txt = txt.split(sep)[1].split("```")[0].strip()
            break
    try:
        return json.loads(txt)
    except Exception:
        return {}

def lire_bl(img):
    return _gpt(
        'Analyse ce bon de livraison. Réponds UNIQUEMENT en JSON valide: '
        '{"fournisseur":"nom societe","numero_bl":"N°BL","date":"YYYY-MM-DD",'
        '"produits":[{"nom":"produit","quantite":"qte"}]}. Si absent mets null.',
        img
    )

def lire_etiquette(img):
    return _gpt(
        'Lis cette étiquette alimentaire. Réponds UNIQUEMENT en JSON valide: '
        '{"nom_produit":"nom","numero_lot":"lot","dlc":"YYYY-MM-DD","quantite":"qte"}.'
        ' Si absent mets null.',
        img
    )

# ─── Helpers ────────────────────────────────────────────────────
def ikb(*rows):
    """Crée un clavier inline simple."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
    ])

async def get_photo_bytes(update: Update, context) -> bytes | None:
    if update.message.photo:
        f = await context.bot.get_file(update.message.photo[-1].file_id)
    elif update.message.document:
        f = await context.bot.get_file(update.message.document.file_id)
    else:
        return None
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    return buf.getvalue()

# ─── États conversation ─────────────────────────────────────────
(WAIT_BL, CHOIX_FOURN, EDIT_BL, WAIT_ETIQ, CONFIRM_PROD, EDIT_PROD) = range(6)

# ─── /start ─────────────────────────────────────────────────────
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "👋 Bonjour ! Je suis ton assistant HACCP 🚚\n\n"
        "📦 /reception — Nouvelle réception\n"
        "📋 /historique — Dernières livraisons\n"
        "🍽️ /plats — Tes fiches plats\n"
        "❌ /annuler — Annuler en cours de route"
    )

# ─── /historique ────────────────────────────────────────────────
async def cmd_historique(update: Update, context):
    livs = get_livraisons_recent()
    if not livs:
        await update.message.reply_text("Aucune livraison enregistrée.")
        return
    txt = "📋 *5 dernières livraisons :*\n\n"
    for l in livs:
        txt += (f"• *{l['nom_fourn']}*  BL {l['numero_bl'] or '—'}"
                f"  _{l['date_reception']}_  ({l['nb_produits']} produit(s))\n")
    await update.message.reply_text(txt, parse_mode="Markdown")

# ─── /plats ─────────────────────────────────────────────────────
async def cmd_plats(update: Update, context):
    plats = get_plats()
    if not plats:
        await update.message.reply_text("Aucune fiche plat. Ajoute-les dans l'app Streamlit.")
        return
    txt = "🍽️ *Fiches plats :*\n\n"
    for p in plats:
        txt += f"• *{p['nom']}* — DLC {p['dlc_jours']} jours\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════
#  RÉCEPTION — flux de conversation
# ════════════════════════════════════════════════════════════════

async def reception_start(update: Update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "📦 *Nouvelle réception*\n\n"
        "📷 Envoie la *photo du BL* (ou le PDF)\n"
        "_(tape /stop si tu n'as pas le BL sous la main)_",
        parse_mode="Markdown"
    )
    return WAIT_BL

# ── Étape 1 : photo du BL ───────────────────────────────────────
async def recu_bl_photo(update: Update, context):
    msg = await update.message.reply_text("⏳ Lecture du BL avec l'IA...")
    img = await get_photo_bytes(update, context)
    if not img:
        await msg.edit_text("❌ Je n'arrive pas à lire ce fichier. Réessaie.")
        return WAIT_BL

    context.user_data["bl_photo"] = img
    try:
        data = lire_bl(img)
    except Exception:
        data = {}
    context.user_data["bl_data"] = data

    found_txt = ""
    if data.get("fournisseur"): found_txt += f"🏭 {data['fournisseur']}\n"
    if data.get("numero_bl"):   found_txt += f"📄 BL {data['numero_bl']}\n"
    if data.get("date"):        found_txt += f"📅 {data['date']}\n"
    prods = [p for p in (data.get("produits") or []) if p and p.get("nom")]
    if prods: found_txt += f"📦 {len(prods)} produit(s) détecté(s)\n"
    context.user_data["bl_produits"] = prods

    await msg.edit_text(
        ("✅ *BL lu !*\n\n" + found_txt if found_txt else "⚠️ BL lu mais infos non trouvées.\n\n")
        + "\n*Quel fournisseur ?*",
        parse_mode="Markdown"
    )
    return await _demander_fournisseur(update, context)

async def bl_sans_photo(update: Update, context):
    """Utilisateur tape /stop à l'étape BL."""
    context.user_data["bl_data"]     = {}
    context.user_data["bl_produits"] = []
    context.user_data["bl_photo"]    = None
    await update.message.reply_text("*Quel fournisseur ?*", parse_mode="Markdown")
    return await _demander_fournisseur(update, context)

async def _demander_fournisseur(update, context):
    fournisseurs = get_fournisseurs()
    context.user_data["fournisseurs"] = [dict(f) for f in fournisseurs]

    # Pré-sélectionner le plus probable
    det = (context.user_data.get("bl_data") or {}).get("fournisseur", "").lower()
    buttons = []
    for f in fournisseurs:
        nl = f["nom"].lower()
        star = "✅ " if (det and (det in nl or nl in det)) else ""
        buttons.append([InlineKeyboardButton(f"{star}{f['nom']}", callback_data=f"f_{f['id']}")])
    buttons.append([InlineKeyboardButton("➕ Autre fournisseur", callback_data="f_new")])

    await update.message.reply_text(
        "Choisis le fournisseur :",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CHOIX_FOURN

# ── Étape 2 : choix fournisseur ─────────────────────────────────
async def choix_fournisseur(update: Update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "f_new":
        await q.edit_message_text("📝 Tape le nom du nouveau fournisseur :")
        context.user_data["saisie"] = "nouveau_fourn"
        return CHOIX_FOURN

    fourn_id = int(q.data.replace("f_", ""))
    fourn = next((f for f in context.user_data["fournisseurs"] if f["id"] == fourn_id), None)
    if not fourn:
        await q.edit_message_text("❌ Fournisseur introuvable.")
        return ConversationHandler.END

    context.user_data["fourn_id"]  = fourn_id
    context.user_data["fourn_nom"] = fourn["nom"]

    # Pré-remplir N°BL et date
    bl_data = context.user_data.get("bl_data") or {}
    context.user_data.setdefault("num_bl",   bl_data.get("numero_bl") or "")
    context.user_data.setdefault("date_liv", bl_data.get("date") or str(date.today()))

    await q.edit_message_text(
        f"✅ *{fourn['nom']}*\n\n"
        f"📄 N° BL : `{context.user_data['num_bl'] or '(non trouvé)'}`\n"
        f"📅 Date   : `{context.user_data['date_liv']}`\n\n"
        "C'est bon ?",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("✅ Oui, enregistrer", "bl_ok")],
            [("✏️ Changer le N° BL", "bl_num"), ("📅 Changer la date", "bl_date")]
        )
    )
    return EDIT_BL

async def saisie_texte_fourn(update: Update, context):
    """Nouveau fournisseur saisi manuellement."""
    nom = update.message.text.strip()
    try:
        row = db_query(
            "INSERT INTO fournisseurs (nom) VALUES (%s) RETURNING id",
            (nom,)
        )
        fourn_id = row["id"]
    except Exception:
        # Existe déjà
        row = db_query("SELECT id FROM fournisseurs WHERE nom=%s", (nom,), fetch="one")
        fourn_id = row["id"] if row else None

    if not fourn_id:
        await update.message.reply_text("❌ Erreur. Réessaie.")
        return CHOIX_FOURN

    context.user_data["fourn_id"]  = fourn_id
    context.user_data["fourn_nom"] = nom
    bl_data = context.user_data.get("bl_data") or {}
    context.user_data.setdefault("num_bl",   bl_data.get("numero_bl") or "")
    context.user_data.setdefault("date_liv", bl_data.get("date") or str(date.today()))

    await update.message.reply_text(
        f"✅ *{nom}*\n\n"
        f"📄 N° BL : `{context.user_data['num_bl'] or '(vide)'}`\n"
        f"📅 Date   : `{context.user_data['date_liv']}`\n\n"
        "C'est bon ?",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("✅ Oui, enregistrer", "bl_ok")],
            [("✏️ Changer le N° BL", "bl_num"), ("📅 Changer la date", "bl_date")]
        )
    )
    return EDIT_BL

# ── Étape 3 : confirmation / édition BL ─────────────────────────
async def edit_bl(update: Update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "bl_ok":
        return await _enregistrer_livraison(q.message, context)
    elif q.data == "bl_num":
        context.user_data["saisie"] = "num_bl"
        await q.edit_message_text(
            f"📄 Tape le N° BL :\n_(actuel : `{context.user_data.get('num_bl') or 'vide'}`)_",
            parse_mode="Markdown"
        )
    elif q.data == "bl_date":
        context.user_data["saisie"] = "date_liv"
        await q.edit_message_text(
            f"📅 Tape la date (JJ/MM/AAAA) :\n_(actuelle : `{context.user_data.get('date_liv')}`)_",
            parse_mode="Markdown"
        )
    return EDIT_BL

async def saisie_bl_champ(update: Update, context):
    saisie = context.user_data.get("saisie")
    val    = update.message.text.strip()

    if saisie == "nouveau_fourn":
        return await saisie_texte_fourn(update, context)

    if saisie == "num_bl":
        context.user_data["num_bl"] = val
    elif saisie == "date_liv":
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                context.user_data["date_liv"] = str(datetime.strptime(val, fmt).date())
                break
            except Exception:
                pass
    context.user_data.pop("saisie", None)

    await update.message.reply_text(
        f"✅ *{context.user_data['fourn_nom']}*\n\n"
        f"📄 N° BL : `{context.user_data.get('num_bl') or 'vide'}`\n"
        f"📅 Date   : `{context.user_data.get('date_liv')}`\n\n"
        "C'est bon ?",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("✅ Oui, enregistrer", "bl_ok")],
            [("✏️ Changer le N° BL", "bl_num"), ("📅 Changer la date", "bl_date")]
        )
    )
    return EDIT_BL

async def _enregistrer_livraison(message, context):
    bl_photo = context.user_data.get("bl_photo")
    bl_b64   = base64.b64encode(bl_photo).decode() if bl_photo else None
    try:
        row = db_query(
            "INSERT INTO livraisons (fournisseur_id, numero_bl, date_reception, photo_bl_b64, photo_bl_ext) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (context.user_data["fourn_id"],
             context.user_data.get("num_bl") or None,
             context.user_data.get("date_liv") or str(date.today()),
             bl_b64, "jpg" if bl_photo else None)
        )
        context.user_data["livraison_id"] = row["id"]
    except Exception as e:
        await message.reply_text(f"❌ Erreur DB : {e}")
        return ConversationHandler.END

    context.user_data["nb_produits"] = 0
    context.user_data["bl_prod_idx"] = 0
    prods = context.user_data.get("bl_produits", [])

    txt = "✅ *Livraison enregistrée !*\n\n"
    if prods:
        txt += f"🤖 {len(prods)} produit(s) détecté(s) dans le BL.\n"
    txt += "📷 Envoie les photos des *étiquettes* une par une.\n_/stop quand tu as fini._"

    await message.reply_text(txt, parse_mode="Markdown")

    # Proposer le 1er produit BL si dispo
    if prods:
        await _proposer_prochain_bl_produit(message, context)

    return WAIT_ETIQ

async def _proposer_prochain_bl_produit(message, context):
    prods   = context.user_data.get("bl_produits", [])
    idx     = context.user_data.get("bl_prod_idx", 0)
    if idx >= len(prods):
        return
    p = prods[idx]
    await message.reply_text(
        f"📦 Produit {idx+1}/{len(prods)} vu dans le BL :\n"
        f"*{p.get('nom','')}*  {p.get('quantite','') or ''}\n\n"
        "📷 Envoie la photo de l'étiquette pour compléter lot + DLC\n"
        "_(ou /stop pour terminer)_",
        parse_mode="Markdown"
    )

# ── Étape 4 : étiquettes produits ───────────────────────────────
async def recu_etiquette(update: Update, context):
    msg = await update.message.reply_text("⏳ Lecture de l'étiquette...")
    img = await get_photo_bytes(update, context)
    if not img:
        await msg.edit_text("❌ Je n'arrive pas à lire. Réessaie ou /stop.")
        return WAIT_ETIQ

    context.user_data["etiq_photo"] = img
    try:
        etiq = lire_etiquette(img)
    except Exception:
        etiq = {}

    # Fusionner avec produit BL si dispo
    prods   = context.user_data.get("bl_produits", [])
    idx     = context.user_data.get("bl_prod_idx", 0)
    bl_prod = prods[idx] if idx < len(prods) else {}

    nom = etiq.get("nom_produit") or bl_prod.get("nom") or "?"
    lot = etiq.get("numero_lot") or "?"
    dlc = etiq.get("dlc") or "?"
    qte = etiq.get("quantite") or bl_prod.get("quantite") or "?"

    context.user_data["etiq_cur"] = {"nom": nom, "lot": lot, "dlc": dlc, "qte": qte}

    await msg.edit_text(
        f"🏷️ *Étiquette lue :*\n\n"
        f"📦 *{nom}*\n"
        f"🔢 Lot : `{lot}`\n"
        f"📅 DLC : `{dlc}`\n"
        f"⚖️ Qté : `{qte}`\n\n"
        "C'est correct ?",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("✅ Enregistrer", "p_ok")],
            [("✏️ Modifier", "p_edit")],
            [("⏭️ Passer", "p_skip")]
        )
    )
    return CONFIRM_PROD

async def confirmer_produit(update: Update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "p_ok":
        await _sauver_produit(context)
        nb  = context.user_data["nb_produits"]
        idx = context.user_data.get("bl_prod_idx", 0) + 1
        context.user_data["bl_prod_idx"] = idx
        prods = context.user_data.get("bl_produits", [])

        if idx < len(prods):
            await q.edit_message_text(f"✅ Produit {nb} enregistré !")
            await _proposer_prochain_bl_produit(q.message, context)
        else:
            await q.edit_message_text(
                f"✅ Produit {nb} enregistré !\n\n"
                "📷 Envoie la prochaine étiquette ou /stop pour terminer."
            )
        return WAIT_ETIQ

    elif q.data == "p_skip":
        context.user_data["bl_prod_idx"] = context.user_data.get("bl_prod_idx", 0) + 1
        context.user_data["etiq_cur"]    = {}
        context.user_data["etiq_photo"]  = None
        prods = context.user_data.get("bl_produits", [])
        idx   = context.user_data.get("bl_prod_idx", 0)
        await q.edit_message_text("⏭️ Passé.")
        if idx < len(prods):
            await _proposer_prochain_bl_produit(q.message, context)
        else:
            await q.message.reply_text("📷 Envoie la prochaine étiquette ou /stop.")
        return WAIT_ETIQ

    elif q.data == "p_edit":
        e = context.user_data.get("etiq_cur", {})
        await q.edit_message_text(
            "✏️ Envoie les infos dans ce format :\n"
            "`Nom | N°lot | DLC (JJ/MM/AAAA) | Quantité`\n\n"
            f"Exemple : `{e.get('nom','?')} | {e.get('lot','?')} | {e.get('dlc','?')} | {e.get('qte','?')}`",
            parse_mode="Markdown"
        )
        return EDIT_PROD

async def modifier_produit_texte(update: Update, context):
    parts = [p.strip() for p in update.message.text.split("|")]
    e = context.user_data.get("etiq_cur", {})
    if len(parts) > 0 and parts[0]: e["nom"] = parts[0]
    if len(parts) > 1 and parts[1]: e["lot"] = parts[1]
    if len(parts) > 2 and parts[2]:
        val = parts[2]
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                e["dlc"] = str(datetime.strptime(val, fmt).date()); break
            except Exception: pass
    if len(parts) > 3 and parts[3]: e["qte"] = parts[3]
    context.user_data["etiq_cur"] = e

    await update.message.reply_text(
        f"🏷️ *Produit corrigé :*\n\n"
        f"📦 *{e.get('nom','?')}*\n"
        f"🔢 Lot : `{e.get('lot','?')}`\n"
        f"📅 DLC : `{e.get('dlc','?')}`\n"
        f"⚖️ Qté : `{e.get('qte','?')}`\n\n"
        "Enregistrer ?",
        parse_mode="Markdown",
        reply_markup=ikb([("✅ Enregistrer", "p_ok"), ("⏭️ Passer", "p_skip")])
    )
    return CONFIRM_PROD

async def _sauver_produit(context):
    e    = context.user_data.get("etiq_cur", {})
    img  = context.user_data.get("etiq_photo")
    b64  = base64.b64encode(img).decode() if img else None
    dlc  = e.get("dlc")
    if dlc in ("?", "", None): dlc = None
    db_query(
        "INSERT INTO produits (livraison_id, fournisseur_id, nom, numero_lot, quantite, dlc, photo_etiquette_b64) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (context.user_data["livraison_id"],
         context.user_data["fourn_id"],
         e.get("nom") or "Produit",
         e.get("lot") if e.get("lot") != "?" else None,
         e.get("qte") if e.get("qte") != "?" else None,
         dlc, b64)
    )
    context.user_data["nb_produits"] = context.user_data.get("nb_produits", 0) + 1
    context.user_data["etiq_cur"]   = {}
    context.user_data["etiq_photo"] = None

# ── Terminer ─────────────────────────────────────────────────────
async def reception_stop(update: Update, context):
    nb    = context.user_data.get("nb_produits", 0)
    fourn = context.user_data.get("fourn_nom", "")
    bl    = context.user_data.get("num_bl", "")
    await update.message.reply_text(
        f"🎉 *Réception terminée !*\n\n"
        f"🏭 {fourn}\n"
        f"📄 BL {bl or '—'}\n"
        f"📦 {nb} produit(s) enregistré(s)\n\n"
        f"✅ Tout est sauvegardé ! Visible dans l'app Streamlit.",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cmd_annuler(update: Update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Annulé.")
    return ConversationHandler.END

# ─── Main ────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reception", reception_start)],
        states={
            WAIT_BL: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, recu_bl_photo),
                CommandHandler("stop", bl_sans_photo),
            ],
            CHOIX_FOURN: [
                CallbackQueryHandler(choix_fournisseur, pattern="^f_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_bl_champ),
            ],
            EDIT_BL: [
                CallbackQueryHandler(edit_bl, pattern="^bl_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_bl_champ),
            ],
            WAIT_ETIQ: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, recu_etiquette),
                CommandHandler("stop", reception_stop),
            ],
            CONFIRM_PROD: [
                CallbackQueryHandler(confirmer_produit, pattern="^p_"),
            ],
            EDIT_PROD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, modifier_produit_texte),
            ],
        },
        fallbacks=[CommandHandler("annuler", cmd_annuler)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("plats",      cmd_plats))
    app.add_handler(conv)

    print("🤖 Bot HACCP démarré...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
