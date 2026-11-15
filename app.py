"""
Vérification collaborative d'annotations de tribunes.
Lancer : streamlit run app.py
Base : st.secrets["DATABASE_URL"] (Postgres) ou SQLite local par défaut.

Une seule file commune. En mode "À valider", chaque tribune ouverte est
verrouillée 10 min pour la personne qui la travaille : l'autre reçoit la suivante.
"""
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

# ------------------------------------------------------------------ CONFIG
USERS = ["Lou", "Jules"]   # <- mets vos prénoms
LOCK_MIN = 10                              # durée du verrou (minutes)

OUI_NON = ["oui", "non"]
TYPES_AUTEUR = [
    "élu·e / politique",
    "chercheur·se / scientifique",
    "médecin / professionnel·le de santé",
    "entrepreneur·se / chef·fe d'entreprise",
    "ONG / asso / syndicat",
    "essayiste / écrivain·e / journaliste",
    "think tank / institut",
    "personnalité (artiste, sportif·ve, cuisinier·ère…)",
    "prof de droit",
    "autre",
]
BORDS = ["extrême droite", "droite", "centre / majorité présidentielle", "gauche", "non applicable"]

MULTI = {"type_auteur"}     # champs à réponses multiples (plusieurs casquettes)
SEP = " | "                 # séparateur dans la base : "élu·e / politique | essayiste / ..."

FIELDS = {
    "collectif": ("Tribune collective (signée par plus d'une personne) ?", OUI_NON),
    "signe_orga": ("Signée par une asso / ONG / syndicat ?", OUI_NON),
    "proche_orga": ("L'auteur·ice est-il·elle président·e d'une asso / d'un collectif ?", OUI_NON),
    "bord_politique": ("Bord politique (si politicien·nes)", BORDS),
    "type_auteur": ("Type d'auteur·ice", TYPES_AUTEUR),
}
# ------------------------------------------------------------------------

st.set_page_config(page_title="Annotation tribunes", layout="wide")


# ------------------------------------------------------------------ AUTH (optionnelle)
def check_password():
    try:
        pwd = st.secrets["password"]
    except Exception:
        return
    if not st.session_state.get("auth_ok"):
        if st.text_input("Mot de passe", type="password") == pwd:
            st.session_state.auth_ok = True
            st.rerun()
        st.stop()


check_password()


# ------------------------------------------------------------------ DB
@st.cache_resource
def get_engine():
    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = "sqlite:///tribunes.db"
    eng = create_engine(url, pool_recycle=240)
    # migration : colonnes de verrou et d'indicateur "à discuter" (ignorées si déjà présentes)
    for col, typ in (("verrou_par", "TEXT"), ("verrou_date", "TEXT"),
                     ("a_discuter", "INTEGER DEFAULT 0")):
        try:
            with eng.begin() as c:
                c.execute(text(f"ALTER TABLE tribunes ADD COLUMN {col} {typ}"))
        except Exception:
            pass
    return eng


engine = get_engine()


def _retry(fn):
    """Si le pooler a coupé une connexion inactive, on repart sur une connexion neuve."""
    try:
        return fn()
    except DBAPIError:
        engine.dispose()
        return fn()


def q(sql, **params):
    def run():
        with engine.connect() as c:
            return pd.read_sql(text(sql), c, params=params)
    return _retry(run)


def execute(sql, **params):
    def run():
        with engine.begin() as c:
            return c.execute(text(sql), params).rowcount
    return _retry(run)


@st.cache_data(ttl=15, show_spinner=False)
def get_stats():
    """Compteurs en UNE requête légère (mis en cache 15 s, vidé à chaque validation)."""
    return q("SELECT annotateur, valide, COALESCE(a_discuter, 0) AS a_discuter, COUNT(*) AS n "
             "FROM tribunes GROUP BY annotateur, valide, COALESCE(a_discuter, 0)")


def now_s():
    return datetime.now().isoformat(timespec="seconds")


def cutoff_s():
    return (datetime.now() - timedelta(minutes=LOCK_MIN)).isoformat(timespec="seconds")


FREE = "(verrou_par IS NULL OR verrou_par = '' OR verrou_par = :me OR verrou_date < :cut)"


def try_claim(i, me):
    """Prend (ou renouvelle) le verrou si la tribune est libre ou déjà à moi."""
    n = execute(
        f"UPDATE tribunes SET verrou_par = :me, verrou_date = :now "
        f"WHERE id = :i AND valide = 0 AND {FREE}",
        me=me, now=now_s(), cut=cutoff_s(), i=int(i),
    )
    return n == 1


def release(i, me):
    execute("UPDATE tribunes SET verrou_par = NULL, verrou_date = NULL "
            "WHERE id = :i AND verrou_par = :me", i=int(i), me=me)


def claim_row(i, me):
    """Prend / renouvelle le verrou ET renvoie la ligne, en une seule requête."""
    def run():
        with engine.begin() as c:
            return c.execute(
                text(f"UPDATE tribunes SET verrou_par = :me, verrou_date = :now "
                     f"WHERE id = :i AND valide = 0 AND {FREE} RETURNING *"),
                dict(me=me, now=now_s(), cut=cutoff_s(), i=int(i)),
            ).mappings().first()
    r = _retry(run)
    return pd.Series(dict(r)) if r else None


def claim_next(me, skipped):
    cand = q(f"SELECT id FROM tribunes WHERE valide = 0 AND {FREE} ORDER BY id "
             f"LIMIT {30 + len(skipped)}", me=me, cut=cutoff_s())["id"].tolist()
    for i in cand:
        if i not in skipped:
            row = claim_row(i, me)
            if row is not None:
                return row
    return None


def get_row(i):
    return q("SELECT * FROM tribunes WHERE id = :i", i=int(i)).iloc[0]


def clean(v, options):
    return v if isinstance(v, str) and v in options else None


def clean_multi(v, options):
    """'a | b' -> ['a', 'b'] (ne garde que les options valides ; accepte une ancienne valeur simple)."""
    if not isinstance(v, str):
        return []
    return [x.strip() for x in v.split(SEP.strip()) if x.strip() in options]


# ------------------------------------------------------------------ SAUVEGARDE AUTO
# Envoie un CSV complet dans un dépôt GitHub privé à la connexion d'une personne,
# au plus une fois par heure (tous utilisateurs confondus). Inactif sans secrets.
BACKUP_MIN_INTERVAL = 60   # minutes


@st.cache_resource
def _backup_state():
    return {"last": None}


def backup_if_needed():
    try:
        token, repo = st.secrets["GITHUB_TOKEN"], st.secrets["BACKUP_REPO"]
    except Exception:
        return
    import base64
    import requests

    state, now = _backup_state(), datetime.now()
    if state["last"] and now - state["last"] < timedelta(minutes=BACKUP_MIN_INTERVAL):
        return
    state["last"] = now
    try:
        data = q("SELECT * FROM tribunes ORDER BY id").to_csv(index=False).encode("utf-8")
        r = requests.put(
            f"https://api.github.com/repos/{repo}/contents/backups/tribunes_{now:%Y%m%d_%H%M}.csv",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"message": f"backup {now:%Y-%m-%d %H:%M}", "content": base64.b64encode(data).decode()},
            timeout=20,
        )
        ok = r.status_code in (200, 201)
    except Exception:
        ok = False
    if not ok:   # nouvel essai dans ~5 min
        state["last"] = now - timedelta(minutes=BACKUP_MIN_INTERVAL - 5)


if not st.session_state.get("backup_done"):   # une fois par connexion
    backup_if_needed()
    st.session_state.backup_done = True


# ------------------------------------------------------------------ SIDEBAR
st.session_state.setdefault("pos", 0)
st.session_state.setdefault("skipped", set())
st.session_state.setdefault("cur_id", None)

with st.sidebar:
    st.header("Annotation")
    me = st.selectbox("Qui es-tu ?", USERS)
    mode = st.radio(
        "Mode", ["À valider", "Tout", "Déjà validés", "À discuter"],
        help="« À valider » te sert automatiquement la prochaine tribune libre. "
             "Les autres modes servent à parcourir / corriger.",
    )

    stats = get_stats()
    n = int(stats["n"].sum())
    v = int(stats.loc[stats["valide"] == 1, "n"].sum())
    st.progress(v / max(n, 1), text=f"{v} / {n} validées")
    done = stats[stats["valide"] == 1].groupby("annotateur")["n"].sum()
    if len(done):
        st.caption(" · ".join(f"{k} : {int(c)}" for k, c in done.items()))
    st.caption(f"🚩 {int(stats.loc[stats['a_discuter'] == 1, 'n'].sum())} à discuter")
    if st.button("🔄 Rafraîchir"):
        get_stats.clear()
        st.rerun()

    with st.expander("Export"):
        if st.button("Préparer le CSV"):
            st.session_state.export = q("SELECT * FROM tribunes ORDER BY id").to_csv(index=False).encode("utf-8")
        if st.session_state.get("export"):
            st.download_button("⬇️ Télécharger le CSV", st.session_state.export,
                               file_name="tribunes_annotees.csv", mime="text/csv")

# ------------------------------------------------------------------ CHOIX DE LA TRIBUNE
if mode == "À valider":
    cur = st.session_state.cur_id
    row = claim_row(cur, me) if cur is not None else None   # renouvelle le verrou + lit la ligne
    if row is None:                                          # validée ou prise par l'autre entre-temps
        row = claim_next(me, st.session_state.skipped)
    if row is None:
        st.session_state.cur_id = None
        if st.session_state.skipped:
            st.warning("Plus de tribune libre, hormis celles que tu as passées.")
            if st.button("Revoir les tribunes passées"):
                st.session_state.skipped = set()
                st.rerun()
        else:
            st.success("Plus aucune tribune à valider (ou celles qui restent sont en cours chez l'autre) 🎉")
        st.stop()
    i = int(row["id"])
    st.session_state.cur_id = i
    remaining = n - v
    caption = f"Reste {remaining} tribune(s) à valider  ·  id {i}"
else:
    where = {"Déjà validés": "WHERE valide = 1", "À discuter": "WHERE a_discuter = 1"}.get(mode, "")
    ids = q(f"SELECT id FROM tribunes {where} ORDER BY id")["id"].tolist()
    if not ids:
        st.success("Rien à afficher avec ce filtre.")
        st.stop()
    with st.sidebar:
        st.session_state.pos = min(st.session_state.pos, len(ids) - 1)
        st.session_state.pos = st.number_input(
            f"Aller à la tribune n° (sur {len(ids)})", 1, len(ids), st.session_state.pos + 1) - 1
    i = ids[st.session_state.pos]
    caption = f"Tribune {st.session_state.pos + 1} / {len(ids)}  ·  id {i}"
    row = get_row(i)

# ------------------------------------------------------------------ MAIN
st.caption(f"{caption}  ·  {row['date']}")
st.title(row["titre"])
st.markdown(f"**Auteur·ice(s)** : {row['auteur']}")
st.write(row["description"])
st.markdown(f"[🔗 Ouvrir l'article]({row['url']})")

if row["valide"]:
    st.info(f"Déjà validée par **{row['annotateur']}** le {row['date_validation']}. "
            "Tu peux la modifier et revalider.")
elif mode != "À valider":
    lock = row.get("verrou_par")
    if isinstance(lock, str) and lock and lock != me and str(row["verrou_date"]) >= cutoff_s():
        st.warning(f"⚠️ **{lock}** est en train de travailler sur cette tribune.")
if pd.notna(row["a_discuter"]) and row["a_discuter"] == 1:
    st.warning("🚩 Marquée « à discuter ».")
st.divider()

with st.form(key=f"form_{i}"):
    answers = {}
    cols = st.columns(2)
    for k, (f, (label, opts)) in enumerate(FIELDS.items()):
        ia = row[f"ia_{f}"]
        with cols[k % 2]:
            if f in MULTI:
                answers[f] = st.multiselect(
                    label + " (plusieurs choix possibles)", opts,
                    default=clean_multi(row[f"humain_{f}"], opts),
                    key=f"{f}_{i}",
                    placeholder="Choisir une ou plusieurs options",
                )
            else:
                current = clean(row[f"humain_{f}"], opts)
                answers[f] = st.radio(
                    label, opts,
                    index=opts.index(current) if current else None,
                    horizontal=(opts == OUI_NON),
                    key=f"{f}_{i}",
                )
            st.caption(f"🤖 IA : {ia if isinstance(ia, str) else '— (non renseigné)'}")
    comment = st.text_input("Commentaire (optionnel)",
                            value=row["commentaire"] if isinstance(row["commentaire"], str) else "")

    c1, c2, c3, c4 = st.columns(4)
    prev = c1.form_submit_button("← Précédente", disabled=(mode == "À valider"))
    skip = c2.form_submit_button("Passer →")
    flag = c3.form_submit_button("🚩 À discuter")
    ok = c4.form_submit_button("✅ Valider et suivante", type="primary")

if ok or flag or skip or prev:
    if ok or flag:
        # "Valider" exige tous les champs ; "À discuter" accepte un remplissage partiel
        if ok and any(a is None or a == [] for a in answers.values()):
            st.error("Il reste des champs non remplis (ou clique sur « À discuter »).")
            st.stop()
        sets = ", ".join(f"humain_{f} = :{f}" for f in FIELDS)
        answers = {f: (SEP.join(a) if a else None) if isinstance(a, list) else a
                   for f, a in answers.items()}
        execute(
            f"UPDATE tribunes SET {sets}, a_discuter = :disc, valide = 1, annotateur = :me, "
            "date_validation = :d, commentaire = :c, verrou_par = NULL, verrou_date = NULL "
            "WHERE id = :i",
            me=me, d=now_s(), c=comment, i=int(i), disc=int(flag), **answers,
        )
        get_stats.clear()
    if mode == "À valider":
        if skip:
            st.session_state.skipped.add(int(i))
            release(i, me)
        st.session_state.cur_id = None
    else:
        if prev:
            st.session_state.pos = max(0, st.session_state.pos - 1)
        elif mode == "À discuter" and ok:
            pass   # la tribune sort de la liste (marqueur retiré) : on reste à la même position
        else:
            st.session_state.pos += 1
    st.rerun()
