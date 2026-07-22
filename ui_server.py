"""
static-remix UI server
Run: python ui_server.py
Opens at: http://localhost:7373
"""
import base64, json, os, uuid, urllib.request, urllib.error, shutil, re, textwrap, random, time, socket
import concurrent.futures, threading, subprocess
from datetime import datetime as _dt
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

def safe_name(filename: str, fallback: str = "upload") -> str:
    """Sanitise a browser-supplied upload filename. Werkzeug does NOT strip path
    separators from multipart filenames (verified on werkzeug 3.1.8), so an upload
    named '../../../evil.png' would otherwise be written outside the project
    folder. secure_filename can return '' (e.g. a name that is all dots), so keep
    a fallback rather than building a path from an empty string."""
    return secure_filename(filename or "") or fallback

# ── Font paths ────────────────────────────────────────────────────────────────
FONTS_DIR       = Path(__file__).parent / "fonts"
FONT_H1         = str(FONTS_DIR / "Montserrat-Bold.ttf")        # Bold for H1 headlines
FONT_BODY       = str(FONTS_DIR / "Poppins-SemiBold.ttf")       # for bullets / subheads
FONT_REGULAR    = str(FONTS_DIR / "Poppins-Regular.ttf")
FONT_BOLD       = str(FONTS_DIR / "Poppins-Bold.ttf")
FONT_FALLBACK   = str(FONTS_DIR / "Poppins-Bold.ttf")

PORT         = int(os.environ.get("PORT", 7373))
MYR_RATE     = 4.07
# Real Gemini API per-image prices (verified against https://ai.google.dev/gemini-api/docs/pricing):
#   gemini-2.5-flash-image ("draft")              = $0.039/image
#   nano-banana-pro-preview ("final"), 1K/2K output = $0.134/image (4K tier is $0.24, not used here)
COSTS        = {"draft": 0.039, "final": 0.134}
DRAFT_MODEL  = "gemini-2.5-flash-image"           # cheap draft (~$0.01)
DRAFT_FALL   = "gemini-3.1-flash-image-preview"   # fallback draft
FINAL_MODEL  = "gemini-3.1-flash-image"           # Fast, high-quality image model (~15s/image). Measured
                                                  # 2026-07-21: Nano Banana Pro (nano-banana-pro-preview AND
                                                  # the GA gemini-3-pro-image) took 2-5+ min/image and often
                                                  # timed out; gemini-3.1-flash-image renders in ~15s with
                                                  # strong product fidelity. To go back to top-tier Pro quality
                                                  # (accepting the long wait) set this to "gemini-3-pro-image".
VISION_MODEL = "gemini-2.5-flash"                 # vision analysis (text only, cheap)

# Marker prefix on billing/rate-limit errors so callers can recognise a quota
# failure and stop trying other models (they all share one account quota).
QUOTA_ERR_PREFIX = "QUOTA: "

SKILLS_DIR    = Path(__file__).parent
# Always persist inside the project folder, never the OS temp directory — the old
# platform check (real /tmp on macOS/Linux, but the project folder itself on
# Windows) meant Brand Setup, Board, personas, and spend data all silently reset
# every time this ran on a non-Windows machine, since /tmp gets wiped on reboot
# and even routine cleanup. This caused real data loss more than once.
_TMP          = SKILLS_DIR
SESSIONS_DIR  = _TMP / "sessions"
BRAND_DIR     = SKILLS_DIR / "brand_kit"
BRAND_JSON    = _TMP / "brand_kit.json"
SPEND_JSON    = _TMP / "spend_ledger.json"   # cumulative real $ spent — NOT a Gemini "balance"
                                              # (Gemini API has no balance/credits endpoint;
                                              # this is a local running total of actual generations)
PERSONAS_JSON = _TMP / "personas.json"       # editable in-app via the Personas & Angles tab
ANGLES_JSON   = _TMP / "angles.json"         # editable in-app via the Personas & Angles tab
STYLES_JSON   = _TMP / "styles.json"         # editable in-app via the Personas & Angles tab
BENEFITS_JSON = _TMP / "product_benefits.json"  # authored per-segment benefit bullets (from user's source docs)
PHOTO_STYLE_JSON = _TMP / "photo_styles.json"   # cached classification of how each product photo presents
                                                  # the product (bottle alone / poured / held) — used to
                                                  # auto-pick whichever photo best matches the chosen template
ROTATION_JSON = _TMP / "template_rotation.json" # persistent auto-template rotation state (order + cursor) so
                                                  # every render walks to the next distinct design and the whole
                                                  # library is used before any template repeats
TEMPLATES_DIR = SKILLS_DIR / "templates"
TEMPLATES_JSON= TEMPLATES_DIR / "templates.json"
for d in (SESSIONS_DIR, BRAND_DIR, TEMPLATES_DIR):
    d.mkdir(exist_ok=True)

# ── Atomic JSON writes ─────────────────────────────────────────────────────────
# Flask serves requests multi-threaded, and the whole "database" is plain JSON
# files. Writing with a bare open("w") + json.dump is NOT atomic: if the process
# is killed mid-write (it was found dead exactly this way) or two requests write
# at once, the file is left truncated/corrupt and that data (brand kit, board,
# personas, spend) is lost. Writing to a temp file and os.replace()-ing it in is
# atomic on POSIX, so a reader/crash never sees a half-written file.
_WRITE_LOCK = threading.Lock()

def atomic_write_json(path, obj, *, indent=2, ensure_ascii=False):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _WRITE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

# ── Board / campaign mutation lock ────────────────────────────────────────────
# atomic_write_json only makes a single write atomic — it does NOT protect a
# read-modify-write. Batches fire every slot of /api/generate-one in parallel
# (see ui.html "Generate all in parallel") and Flask serves them on threads, so
# without this lock two finishing slots both read the same board, compute the
# SAME next id, write to the same board_NNNNN.png (the later image silently
# overwriting the earlier one), and the last save_board() drops the other's
# entry. That is how creatives kept disappearing from the board. Every
# load_board()->mutate->save_board() sequence must hold this lock, exactly like
# _SPEND_LOCK already does for the spend ledger. RLock so nested helpers are safe.
_BOARD_LOCK = threading.RLock()

# Same problem for a session's meta.json: all slots of a batch share one session
# file and each merges its generated copy into it.
_META_LOCK = threading.RLock()

# ── Client-supplied path safety ───────────────────────────────────────────────
# Several endpoints take a filename or path straight from the request body and
# then read/copy/upload that file. Without a containment check, "../.env" reaches
# the API keys. Resolve the path and confirm it really sits under one of the
# roots we're willing to serve; return None otherwise. (Same idea as the
# is_relative_to check on product_photo_path in /api/upload.)
def safe_path_under(candidate, *roots):
    if not candidate:
        return None
    try:
        p = Path(candidate).resolve()
    except (OSError, ValueError):
        return None
    for root in roots:
        try:
            if p.is_relative_to(Path(root).resolve()):
                return p
        except (OSError, ValueError):
            continue
    return None

# ── Automatic data snapshots ──────────────────────────────────────────────────
# Everything the user generates (board.json + board images, brand kit, personas)
# lives only on disk. When the board folder was wiped, recovery was possible ONLY
# because the data happened to have been committed to git — and anything created
# since that commit was lost. This commits the data files on a timer so there is
# never a large uncommitted window to lose. Git de-duplicates unchanged blobs, so
# repeated snapshots are cheap.
SNAPSHOT_PATHS = ["board", "sessions", "personas.json", "angles.json", "styles.json",
                  "brand_kit.json", "templates/templates.json", "spend_ledger.json",
                  "photo_styles.json", "product_benefits.json",
                  "product_visuals.json", "template_rotation.json"]
SNAPSHOT_INTERVAL = 600          # seconds between automatic snapshots
_SNAPSHOT_LOCK = threading.Lock()

def _git(*args, timeout=180):
    return subprocess.run(["git", *args], cwd=str(SKILLS_DIR),
                          capture_output=True, text=True, timeout=timeout)

def snapshot_data(reason="auto"):
    """Commit just the data paths to git. Uses an explicit pathspec so it can never
    sweep up unrelated work the user has staged. Returns (committed?, message)."""
    with _SNAPSHOT_LOCK:
        try:
            if not (SKILLS_DIR / ".git").exists():
                return False, "not a git repository"
            paths = [p for p in SNAPSHOT_PATHS if (SKILLS_DIR / p).exists()]
            if not paths:
                return False, "nothing to snapshot"
            _git("add", "--", *paths)
            # nothing staged for these paths -> nothing to do
            if _git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
                return False, "no changes"
            msg = f"Auto-snapshot ({reason}) {_dt.now():%Y-%m-%d %H:%M:%S}"
            r = _git("-c", "user.name=Ads Generator", "-c", "user.email=noreply@local",
                     "commit", "-m", msg, "--", *paths)
            if r.returncode == 0:
                return True, msg
            return False, (r.stderr or r.stdout or "commit failed").strip()[:200]
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

def snapshot_async(reason="auto"):
    """Fire-and-forget snapshot so a just-created image is committed within
    seconds instead of waiting up to SNAPSHOT_INTERVAL. Tweak variants in
    particular have no other backup (they're written straight to board/ with no
    copy in sessions/), so an unsnapshotted variant is unrecoverable if the
    folder is lost. The snapshot lock serialises overlapping calls."""
    threading.Thread(target=lambda: snapshot_data(reason), daemon=True).start()

def _snapshot_loop():
    while True:
        time.sleep(SNAPSHOT_INTERVAL)
        ok, msg = snapshot_data("timer")
        if ok:
            print(f"[snapshot] {msg}")

def start_snapshot_thread():
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    print(f"[snapshot] auto-snapshot every {SNAPSHOT_INTERVAL//60} min")

# ── Use cases per product — from 10_customers/use_cases.md ─────────────────────
# Only confidence-rated ✅ use cases are included (Primary/Strong/Common/Documented).
# Anecdotal/off-label use cases (acne, stretch marks, hair gel, pet use, old scars) are
# deliberately excluded — the source doc flags these as "must not appear in marketing"
# or "not a marketing lever" under the MAB claims framework.
PRODUCT_USE_CASES = {
    # benefit_keywords: lowercase substrings to match against this product's actual
    # Brand Setup benefit bullets — explicit, since use-case labels are in English
    # but the user's benefit text is largely in Malay (generic NLP matching fails).
    "jeli gamat": [
        {"label": "Pemulihan Selepas Bersalin Caesar", "emoji": "🤱",
         "context": "new mothers recovering after a caesarean delivery — wound healing, uterus recovery, "
                     "confinement (pantang) period support; the single highest-intent, culturally-embedded use case",
         "benefit_keywords": ["pemulihan", "kemalangan", "tisu badan"]},
        {"label": "Pemulihan Luka Selepas Pembedahan", "emoji": "🏥",
         "context": "patients recovering after surgery needing oral/internal support for faster wound healing — "
                     "many customers report a noticeable difference within ~1 week",
         "benefit_keywords": ["pemulihan", "tisu badan", "kemalangan"]},
        {"label": "Lega Gastrik / Refluks Asid", "emoji": "🫃",
         "context": "people with gastric problems, stomach ulcers, or acid reflux needing gut lining repair — "
                     "a chronic, repeat-purchase use case, often noticeable within ~1 week",
         "benefit_keywords": ["kesihatan badan", "kecergasan", "harian"]},
        {"label": "Sakit Sendi / Keradangan", "emoji": "🦵",
         "context": "people with joint pain, stiffness, or inflammation needing natural anti-inflammatory relief — "
                     "results highly variable by severity, age, and concurrent conditions",
         "benefit_keywords": ["sendi", "tulang rawan", "pergerakan"]},
        {"label": "Kesihatan & Kecergasan Umum", "emoji": "💪",
         "context": "people wanting general health maintenance, energy, and vitality support through daily use — "
                     "a cumulative, long-term benefit",
         "benefit_keywords": ["kecergasan", "konsisten", "harian", "setiap hari"]},
        {"label": "Pemulihan Selepas Kemalangan", "emoji": "🚑",
         "context": "people recovering from accidents or physical trauma needing faster natural healing and "
                     "immune-cell recovery support",
         "benefit_keywords": ["kemalangan", "pemulihan", "tisu badan"]},
    ],
    "gel gamat": [
        {"label": "Luka (Ringan & Serius)", "emoji": "🩹",
         "context": "people with minor or severe wounds needing topical healing support — the #1 reason "
                     "customers buy this product; safe on open wounds and under bandages, visible difference "
                     "typically within a few days",
         "benefit_keywords": ["wound"]},
        {"label": "Melecur", "emoji": "🔥",
         "context": "people with burns needing a calming topical gel that supports the skin's healing process",
         "benefit_keywords": ["burn"]},
        {"label": "Ekzema", "emoji": "🌿",
         "context": "people with eczema needing a gel that calms and hydrates irritated, inflamed skin",
         "benefit_keywords": ["eczema"]},
        {"label": "Elak Parut Luka Baharu", "emoji": "✨",
         "context": "people with fresh wounds wanting to minimise scarring during the early healing phase",
         "benefit_keywords": ["scar", "fresh-wound"]},
        {"label": "Pelembap Kulit", "emoji": "🧴",
         "context": "people using the gel as a gentle, everyday moisturiser for face and hands",
         "benefit_keywords": ["moisturiser", "moisturizer", "face and hands"]},
        {"label": "Untuk Kanak-Kanak", "emoji": "👶",
         "context": "parents looking for a gentle, safe topical gel for babies and children's minor cuts and "
                     "skin irritation",
         "benefit_keywords": ["wound"]},   # no dedicated pediatric bullet exists yet — safest available fallback
    ],
    "minyak urut": [
        {"label": "Otot Lenguh Selepas Aktiviti", "emoji": "💪",
         "context": "people with sore, fatigued muscles after physical activity needing a gentle, non-warming "
                     "aromatherapy massage oil",
         "benefit_keywords": ["otot lenguh", "keletihan"]},
        {"label": "Sakit & Kaku Sendi", "emoji": "🦵",
         "context": "people with joint pain or stiffness needing a gentle massage oil for relief, without the "
                     "harsh heat sensation of traditional warming oils",
         "benefit_keywords": ["sendi", "kekejangan"]},
        {"label": "Lega Otot Selepas Bersenam", "emoji": "🏋️",
         "context": "active people and athletes needing post-workout muscle relief through massage",
         "benefit_keywords": ["otot lenguh", "aktiviti fizikal"]},
        {"label": "Sakit Kepala (Urut Dahi)", "emoji": "🤕",
         "context": "people with headaches using gentle temple massage with an aromatic, non-warming oil for relief",
         "benefit_keywords": ["sakit kepala", "dahi"]},
        {"label": "Sakit Perut Kanak-Kanak", "emoji": "🍼",
         "context": "parents treating children's stomach discomfort with a traditional Malay remedy — the "
                     "gentle, non-warming formulation is appropriate for children",
         "benefit_keywords": ["lembut", "sensitif"]},   # no dedicated bullet — closest gentle/non-warming fallback
        {"label": "Urutan Badan", "emoji": "💆",
         "context": "people wanting a general relaxing body massage with a clean, aromatic, roller-ball "
                     "applied oil",
         "benefit_keywords": ["mengurut", "aroma", "roller-ball"]},
    ],
}

# Fallback generic list — used only when the active product's name doesn't match
# any known product above (e.g. no product selected yet).
DEFAULT_USE_CASES = [
    {"label": "Selepas Bersalin", "emoji": "🤱", "context": "new mothers recovering after childbirth — wound healing, uterus recovery, energy restoration"},
    {"label": "Sakit Sendi",      "emoji": "🦵", "context": "people with joint/knee pain or arthritis needing natural anti-inflammatory relief"},
    {"label": "Kemalangan",       "emoji": "🚑", "context": "people recovering from accidents or fractures needing faster natural healing"},
    {"label": "Gastrik",          "emoji": "🫃", "context": "people with gastric problems, stomach ulcers, or acid reflux needing gut lining repair"},
    {"label": "Selepas Pembedahan", "emoji": "🏥", "context": "patients after surgery needing faster wound healing and immune support"},
]

def _product_key(product_name: str) -> str:
    """Normalize a free-text product name to the PRODUCT_USE_CASES/PRODUCT_BENEFITS key."""
    name = (product_name or "").lower()
    if "jeli" in name:
        return "jeli gamat"
    if "minyak" in name:
        return "minyak urut"
    if "gel" in name and "gamat" in name:
        return "gel gamat"
    return ""

def get_use_cases_for_product(product_name: str) -> list:
    return PRODUCT_USE_CASES.get(_product_key(product_name), DEFAULT_USE_CASES)

def get_benefit_keywords(product_name: str, problem_label: str) -> list:
    """Look up the explicit benefit-matching keywords for a given product + use case."""
    for uc in get_use_cases_for_product(product_name):
        if uc["label"] == problem_label:
            return uc.get("benefit_keywords", [])
    return []

def get_other_benefit_keywords(product_name: str, problem_label: str) -> list:
    """All OTHER use cases' benefit_keywords for this product (excluding the current
    one) — used so the benefits fallback never pads a use case that has no
    dedicated block of its own with bullets that clearly belong to a DIFFERENT,
    specific use case (e.g. accident-recovery bullets bleeding into a gastric ad
    just because gastric's own keywords are too generic to hit 3 direct matches)."""
    others = []
    for uc in get_use_cases_for_product(product_name):
        if uc["label"] != problem_label:
            others.extend(uc.get("benefit_keywords", []))
    return others

# Hand-curated, UNAMBIGUOUS condition-specific nouns — deliberately much narrower
# than benefit_keywords above. benefit_keywords includes generic process words
# (pemulihan, tisu badan, harian) that are legitimately shared across many use
# cases, so they're unsafe to blindly delete from free-form text. These terms are
# only included when they unambiguously name ONE specific condition, safe to strip
# out of a DIFFERENT use case's copy-generation context (e.g. "sendi" never belongs
# in a C-section ad's background text, even though it's a valid general product fact).
CONDITION_TERMS = {
    "Pemulihan Selepas Bersalin Caesar": ["bersalin", "caesar", "czer", "cesarean"],
    "Pemulihan Luka Selepas Pembedahan": ["pembedahan"],
    "Lega Gastrik / Refluks Asid": ["gastrik", "refluks asid", "gerd"],
    "Sakit Sendi / Keradangan": ["sendi", "tulang rawan", "keradangan", "sakit lutut"],
    "Pemulihan Selepas Kemalangan": ["kemalangan"],
    "Melecur": ["melecur"],
    "Ekzema": ["ekzema"],
    "Elak Parut Luka Baharu": ["parut luka"],
    "Otot Lenguh Selepas Aktiviti": ["otot lenguh"],
    "Sakit & Kaku Sendi": ["sendi", "kaku sendi"],
    "Sakit Kepala (Urut Dahi)": ["sakit kepala"],
    "Sakit Perut Kanak-Kanak": ["sakit perut kanak-kanak"],
}

def redact_other_conditions(text: str, product_name: str, problem_label: str) -> str:
    """Strip mentions of OTHER use cases' specific conditions (e.g. 'sendi') out of
    general background text (Product description, Proof, Competitors) before it
    reaches copy generation. A 'stay on topic' instruction alone wasn't reliable
    enough against a condition named prominently in the source text itself — this
    removes the temptation at the source instead of relying on the model to resist it."""
    if not text:
        return text
    for uc in get_use_cases_for_product(product_name):
        if uc["label"] == problem_label:
            continue
        for term in CONDITION_TERMS.get(uc["label"], []):
            text = re.sub(re.escape(term), "", text, flags=re.IGNORECASE)
    # Clean up artifacts left behind by removed words
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*\.", ".", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()

# Flat label → context lookup used by build_prompt — merges every product's use
# cases plus the fallback list, since labels are unique across the whole set.
PROBLEM_CONTEXT = {}
for _uc_list in list(PRODUCT_USE_CASES.values()) + [DEFAULT_USE_CASES]:
    for _uc in _uc_list:
        PROBLEM_CONTEXT[_uc["label"]] = _uc["context"]

# ── Authored per-segment benefit bullets — user-supplied source docs (e.g.
# Manfaat_Jeli_Gamat_Meta_Ads.docx), verbatim, no AI involved. When a use case
# has authored bullets here, they are used DIRECTLY as ground truth instead of
# fuzzy-keyword-matching the general Brand Setup text — eliminates the
# cross-use-case contamination risk that keyword matching is prone to.
DEFAULT_PRODUCT_BENEFITS = {
    "jeli gamat": {
        "Sakit Sendi / Keradangan": [
            "Bantu bina semula rawan sendi.",
            "Sokong struktur sendi yang sihat.",
            "Kekal ligamen dan tendon yang kuat.",
            "Kurang geseran antara sendi.",
            "Kekal keanjalan tisu sendi.",
            "Sokong tindak balas keradangan yang normal.",
            "Kekal keselesaan ketika bergerak.",
            "Sokong kesihatan sendi harian.",
            "Bantu baiki tisu sendi.",
            "Bekal nutrien untuk bina tisu baharu.",
            "Bantu hasil kolagen semula jadi.",
            "Sokong pembentukan tisu badan.",
        ],
        "Lega Gastrik / Refluks Asid": [
            "Sokong pembaharuan tisu saluran penghadaman.",
            "Kekal kesihatan lapisan perut.",
            "Bantu baiki tisu badan.",
            "Bekal nutrien ketika badan pulih.",
            "Sokong penghasilan kolagen.",
            "Bantu pembaharuan sel.",
            "Sokong kesihatan badan.",
            "Bekal sebatian bioaktif semula jadi.",
        ],
        "Pemulihan Selepas Bersalin Caesar": [
            "Bantu bina tisu baharu.",
            "Sokong pemulihan kulit.",
            "Bantu hasil kolagen semula jadi.",
            "Kekal keanjalan kulit.",
            "Sokong pemulihan luka.",
            "Bantu baiki tisu badan.",
            "Sokong pemulihan selepas bersalin.",
            "Bantu bina sel baharu.",
            "Sokong pembaikan tisu.",
            "Sokong proses pemulihan badan.",
        ],
        "Pemulihan Selepas Kemalangan": [
            "Bantu bina tisu baharu.",
            "Sokong pembinaan semula kulit.",
            "Kekal keanjalan kulit.",
            "Sokong pemulihan tisu.",
            "Bantu baiki tisu cedera.",
            "Sokong pemulihan badan.",
            "Bantu regenerasi tisu.",
            "Sokong penghasilan kolagen.",
            "Sokong proses pemulihan badan.",
        ],
        "Pemulihan Luka Selepas Pembedahan": [
            "Bantu bina tisu baharu.",
            "Sokong pemulihan kulit.",
            "Bantu hasil kolagen badan.",
            "Kekal kekuatan tisu.",
            "Bantu baiki tisu badan.",
            "Sokong pemulihan selepas pembedahan.",
            "Bantu bina sel baharu.",
            "Sokong pembaikan tisu.",
            "Sokong proses pemulihan badan.",
        ],
    },
}

def load_product_benefits() -> dict:
    if BENEFITS_JSON.exists():
        with open(BENEFITS_JSON, encoding="utf-8") as f:
            return json.load(f)
    save_product_benefits(DEFAULT_PRODUCT_BENEFITS)
    return {k: dict(v) for k, v in DEFAULT_PRODUCT_BENEFITS.items()}

def save_product_benefits(benefits: dict):
    atomic_write_json(BENEFITS_JSON, benefits)

PRODUCT_BENEFITS = load_product_benefits()

def get_authored_benefits(product_name: str, problem_label: str) -> list:
    """Ground-truth benefit bullets authored by the user for this exact product +
    use case (e.g. from a supplied Word doc) — returns [] if none were provided,
    in which case the caller should fall back to keyword-matching Brand Setup text."""
    return PRODUCT_BENEFITS.get(_product_key(product_name), {}).get(problem_label, [])

# ── Customer personas & persuasion angles — editable in-app via the Personas & ─
# Angles tab, persisted to personas.json / angles.json (same pattern as brand_kit.json).
# Persona shapes WHO appears in the ad + the emotional voice of the copy.
# Angle shapes HOW the copy is framed (persuasion style) — fully independent of persona.
def _persona_prompt_from_narrative(narrative: str) -> str:
    """Wrap a raw persona story into a Gemini casting instruction. Gemini infers
    ethnicity/age/wardrobe/hijab/setting from the story itself — never hand-authored."""
    return (
        "Cast the person (or people) described in this real customer story. Infer appropriate, "
        "brand-consistent Malaysian casting — ethnicity, age, wardrobe, hijab/tudung if a Malay woman, "
        "and setting — directly from the story below. Show a believable moment tied to this exact "
        "situation; favour a moment of relief or returning normalcy over visible pain or illness.\n"
        f"CUSTOMER STORY: {narrative}"
    )

def _persona_copy_voice_from_narrative(narrative: str) -> str:
    """Wrap a raw persona story into ad-copy voice guidance."""
    return (
        "Write for this exact person and their emotional situation — speak to their specific worry, "
        "hope, and daily reality, not a generic customer:\n"
        f"{narrative}"
    )

DEFAULT_PERSONA_STYLE_CUES = (
    "Believable Malaysian home setting. Natural lighting — window light or warm indoor lamp light. "
    "The mood is a moment of relief or connection, not a staged product demonstration. "
    "Follow the winning ad template's color palette and background — do not impose warm/gold tones."
)

DEFAULT_PERSONAS = {
    "csection": {
        "label": "C-Section Recovery",
        "emoji": "🤱",
        "subtitle": "Ibu 56 tahun, anak lepas Caesar",
        "narrative": (
            "Ibu 56 tahun, anak baru lepas bersalin Czer, proses pemulihan perlahan, anak sukar bergerak "
            "untuk menguruskan baby dan menyebabkan seisi rumah tidak terurus."
        ),
        "use_cases": ["Pemulihan Selepas Bersalin Caesar"],
        "style_cues": DEFAULT_PERSONA_STYLE_CUES,
    },
    "knee": {
        "label": "Sakit Lutut (Knee Pain)",
        "emoji": "🦵",
        "subtitle": "60yo, stiff knees, wants dignity back",
        "narrative": (
            "A 60 year old whose stiff, aching knees makes stairs, solat and keeping up with "
            "grand-children hard and he has quietly began to feel useless — he isn't after miracle, "
            "he just wants his dignity, his prayers performed properly and his place back in family life."
        ),
        "use_cases": ["Sakit Sendi / Keradangan"],
        "style_cues": DEFAULT_PERSONA_STYLE_CUES,
    },
    "gastric_child": {
        "label": "Gastrik (Child)",
        "emoji": "🧒",
        "subtitle": "Mother, 35-40, child's gastric issues",
        "narrative": (
            "A 35 to 40 years old mother whose child battles gastric that wracked his focus and "
            "appetite — she's tried 6 things, torn between forcing him off his favourite food and "
            "watching him hurt — she wants a gentle, trusted daily helper so her child can just be "
            "comfortable again."
        ),
        "use_cases": ["Lega Gastrik / Refluks Asid"],
        "style_cues": DEFAULT_PERSONA_STYLE_CUES,
    },
    "gastric_husband": {
        "label": "Gastrik (Husband)",
        "emoji": "🍽️",
        "subtitle": "Wife, 35, husband's gastric cuts meals short",
        "narrative": (
            "A 35 year old wife whose husband's gastric cuts every kenduri and every meal short and "
            "has quietly drained the ease and romance of the marriage, she's exhausted by the "
            "complaints and early exits and wants back the food-loving partnership that used to be there."
        ),
        "use_cases": ["Lega Gastrik / Refluks Asid"],
        "style_cues": DEFAULT_PERSONA_STYLE_CUES,
    },
    "accident": {
        "label": "Accident Recovery",
        "emoji": "🏍️",
        "subtitle": "Delivery rider, 30, recovering from accident",
        "narrative": (
            "Delivery rider, 30 years old, baru alami kemalangan, pergerakan terhad sehingga rasa "
            "menyusahkan sebab terlalu bergantung dengan isteri — mahu pulih secepat mungkin dan "
            "kembali kerja untuk sara keluarga."
        ),
        "use_cases": ["Pemulihan Selepas Kemalangan"],
        "style_cues": DEFAULT_PERSONA_STYLE_CUES,
    },
}

DEFAULT_ANGLES = {
    "problem_agitate": {
        "label": "Problem Agitate",
        "psychology": "Fear of loss / urgency",
        "copy_instruction": (
            "Make the audience fully realize the impact of their problem and the cost of doing nothing. "
            "Expand the consequences of the problem without exaggeration.\n"
            "STRUCTURE — h1 states or intensifies the pain itself (NOT the solution or relief) — make the "
            "cost of inaction felt, urgent. h2 deepens the specific consequence — what keeps getting worse "
            "or what's being lost the longer this goes unaddressed. cta uses urgent, act-now language. Do "
            "not mention relief, results, or resolution anywhere except the CTA."
        ),
        "visual_treatment": (
            "Depict the specific moment of frustration or difficulty from the persona's story — visible "
            "tension, unresolved struggle, before any relief. Facial expression and body language should "
            "show the weight of the problem, not calm or ease."
        ),
    },
    "contrarian": {
        "label": "Contrarian Truth",
        "psychology": "Surprise / pattern interruption",
        "copy_instruction": (
            "Challenge a common belief and replace it with a different perspective. "
            "Formula: Everyone believes X. Actually, Y.\n"
            "STRUCTURE — h1 must follow a myth-vs-truth contrast formula, e.g. 'Ramai sangka [common belief]... "
            "Sebenarnya [surprising truth]' — never a plain claim or benefit statement. h2 backs the "
            "surprising truth with one real product fact from BENEFITS/DESCRIPTION above. cta invites them to "
            "discover or experience the real approach, not a generic 'buy now'."
        ),
        "visual_treatment": (
            "Frame the scene with an unexpected visual twist or contrast — something that visually "
            "interrupts the expected pattern for this persona's situation, prompting a second look."
        ),
    },
    "social_proof": {
        "label": "Social Proof",
        "psychology": "Trust / belonging",
        "copy_instruction": (
            "Show that other people already trust or use the product. "
            "Use customer stories, long brand history, testimonials, repeat buyers.\n"
            "STRUCTURE — h1 opens with a trust/social-validation signal (years trusted, widely used, "
            "real-customer phrasing) — NOT a product benefit or feature claim. h2 reinforces with one "
            "specific, concrete trust detail (a number, a heritage fact, a certification tied to trust — "
            "pull only from CERTIFICATIONS/DESCRIPTION above, never invent a statistic). cta uses "
            "join-the-community framing ('sertai jutaan yang...', 'seperti keluarga lain yang...')."
        ),
        "visual_treatment": (
            "Testimonial-style composition — the person could be mid-conversation or recommending to "
            "someone else, or the scene could include a review/rating visual element. Emphasize trust and "
            "shared experience over a solitary moment."
        ),
    },
    "authority": {
        "label": "Authority / Expertise",
        "psychology": "Credibility / certainty",
        "copy_instruction": (
            "Use credible facts instead of opinions. "
            "Use ingredients, certifications, experience, research, manufacturing quality.\n"
            "STRUCTURE — h1 leads with a credible fact, certification, ingredient, or heritage claim — NO "
            "emotional hook, fact-first tone. h2 briefly explains the mechanism/evidence — why or how it "
            "works, pulled only from DESCRIPTION/CERTIFICATIONS above. cta is confident and "
            "credibility-based ('Formula terbukti', 'Dipercayai...') — not urgent or emotional."
        ),
        "visual_treatment": (
            "Cleaner, more credential-forward framing — closer focus on product/ingredient detail, less "
            "busy lifestyle scene, a more clinical and confident composition."
        ),
    },
    "curiosity": {
        "label": "Curiosity",
        "psychology": "Information gap",
        "copy_instruction": (
            "Create an information gap that makes people want to know more. "
            "Ask questions or tease an unexpected fact without clickbait.\n"
            "STRUCTURE — h1 poses a genuine question or teases an unexpected fact about the problem/product — "
            "it must NOT resolve the problem or reveal the solution yet. h2 deepens the intrigue with one "
            "more detail, still without fully answering it. cta uses discover/find-out framing ('Ketahui "
            "sebabnya', 'Terokai rahsianya') — not a hard sell."
        ),
        "visual_treatment": (
            "Mysterious or intriguing framing — partial reveal, a tighter crop, product held with a "
            "questioning or curious expression. Leave something visually unresolved."
        ),
    },
    "comparison": {
        "label": "Comparison",
        "psychology": "Relative value",
        "copy_instruction": (
            "Help people understand value by comparing two options. "
            "Compare old vs new, difficult vs easy, capsules vs jelly, before vs today.\n"
            "STRUCTURE — h1 is a direct comparison statement naming both sides of the contrast in one line "
            "(old way vs new way / with vs without / difficult vs easy). h2 names the ONE specific point "
            "that makes the new/better side clearly superior, from BENEFITS above. cta uses switch/upgrade "
            "framing ('Beralih sekarang', 'Rasai bezanya')."
        ),
        "visual_treatment": (
            "Split-screen or before/after style composition contrasting two states — old vs new, "
            "difficult vs easy — within the same frame."
        ),
    },
    "transformation": {
        "label": "Transformation",
        "psychology": "Aspiration",
        "copy_instruction": (
            "Sell the desired outcome rather than the product itself. "
            "Focus on life before and after solving the problem.\n"
            "STRUCTURE — h1 frames the arc from struggle to relief in one line — name the DESTINATION "
            "(the outcome), not just the problem. h2 names the specific transformation/outcome achieved, "
            "concrete not vague. cta uses aspirational, start-the-journey framing ('Mulakan perubahan anda')."
        ),
        "visual_treatment": (
            "Explicit before-to-after visual arc — show the persona's transition from struggle to relief "
            "within the composition, emphasizing the outcome/aspiration rather than the problem."
        ),
    },
}

# ── Creative Style — Clarity vs Curiosity Gap ───────────────────────────────
# From the "Static Ads That Scale" field manual: the losing ads sit in the
# middle of this spectrum (too vague to be clear, too obvious to build
# curiosity). Every generated ad should commit to ONE lane. This is a separate
# axis from Angle — Angle is the persuasive content of the message; Style is
# how directly or opaquely that message is presented (does it read as an
# obvious ad, or does it not even register as one).
DEFAULT_STYLES = {
    "clarity": {
        "label": "Clarity",
        "subtitle": "Obvious ad, instant understanding",
        "copy_instruction": (
            "Make the angle, offer, and who this is for completely clear within 3-4 seconds. Use direct, "
            "unambiguous language — state the product/brand plainly, make the CTA obvious. This should "
            "clearly read as an advertisement. Do not hide, delay, or soften the message."
        ),
        "visual_treatment": (
            "Pattern-interrupt execution — loud, bold, and unmistakably an ad. Product/brand clearly "
            "visible, bold typography, strong contrast, obvious commercial styling. The viewer should "
            "immediately recognize this as paid advertising, not organic content."
        ),
    },
    "curiosity_gap": {
        "label": "Curiosity Gap",
        "subtitle": "Native, doesn't look like an ad",
        "copy_instruction": (
            "Deliberately withhold what's being sold — the copy should create a gap the reader wants "
            "resolved, not reveal the product/brand/offer upfront. Write like native platform content (a "
            "personal note, a screenshot-style message, a repost) rather than typical ad copy. The reader "
            "should not immediately register this as an advertisement."
        ),
        "visual_treatment": (
            "Native, blend-into-the-feed execution — mimic organic platform content (e.g. a notes-app "
            "screenshot, a text-message thread, a reposted social post) rather than a polished product ad. "
            "Keep branding minimal or absent. If the product appears at all, keep it ambiguous or partially "
            "obscured rather than a clean hero shot — it should not register as an ad at first glance."
        ),
    },
}

def _hydrate_persona(p: dict) -> dict:
    """Auto-derive the Gemini-facing prompt/copy_voice fields from the editable
    narrative, so the management UI never needs to touch prompt-engineering text."""
    narrative = p.get("narrative", "")
    p["prompt"]     = _persona_prompt_from_narrative(narrative)
    p["copy_voice"] = _persona_copy_voice_from_narrative(narrative)
    p.setdefault("style_cues", DEFAULT_PERSONA_STYLE_CUES)
    p.setdefault("emoji", "🙂")
    p.setdefault("use_cases", [])
    return p

def load_personas() -> dict:
    if PERSONAS_JSON.exists():
        with open(PERSONAS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {k: dict(v) for k, v in DEFAULT_PERSONAS.items()}
        save_personas(data)
    return {k: _hydrate_persona(dict(v)) for k, v in data.items()}

def save_personas(personas: dict):
    # Persist only the editable fields — prompt/copy_voice are derived on load.
    slim = {}
    for k, v in personas.items():
        slim[k] = {kk: vv for kk, vv in v.items() if kk not in ("prompt", "copy_voice")}
    atomic_write_json(PERSONAS_JSON, slim)

def load_angles() -> dict:
    if ANGLES_JSON.exists():
        with open(ANGLES_JSON, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {k: dict(v) for k, v in DEFAULT_ANGLES.items()}
        save_angles(data)
    # Backfill visual_treatment for angles.json files written before this field
    # existed — from the matching default if this is one of the original 7,
    # otherwise a generic fallback so custom angles still vary the scene a little.
    for k, v in data.items():
        if not v.get("visual_treatment"):
            v["visual_treatment"] = DEFAULT_ANGLES.get(k, {}).get(
                "visual_treatment",
                "Vary the specific scene, framing, or composition from a default static portrait."
            )
    return data

def save_angles(angles: dict):
    atomic_write_json(ANGLES_JSON, angles)

def load_styles() -> dict:
    if STYLES_JSON.exists():
        with open(STYLES_JSON, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {k: dict(v) for k, v in DEFAULT_STYLES.items()}
        save_styles(data)
    for k, v in data.items():
        if not v.get("visual_treatment"):
            v["visual_treatment"] = DEFAULT_STYLES.get(k, {}).get("visual_treatment", "")
    return data

def save_styles(styles: dict):
    atomic_write_json(STYLES_JSON, styles)

PERSONAS = load_personas()
ANGLES   = load_angles()
STYLES   = load_styles()

AD_FRAMEWORKS = [
    ("Us Vs Them",      "Two-column split. Left = generic competitor with red ✗ problems. Right = this brand with green ✓ wins. Header: 'US VS THEM'."),
    ("Bold Claim",      "One huge bold statement occupies the top 60% in large typography. Product below. Minimal. Confident."),
    ("Before & After",  "Split screen: left = person in pain (before), right = person recovered and happy (after). Product in centre."),
    ("Testimonial",     "Realistic customer review card (5 stars, short quote) overlaid on a soft lifestyle background. Product in corner."),
    ("Stat Surround",   "Large hero number (e.g. '10 JUTA+ BOTOL') surrounded by smaller proof points — certs, years, reviews."),
    ("Problem Hook",    "Relatable problem in bold text at top. Product below as the solution. Three short benefit bullets."),
    ("Ingredient Hero", "Close-up of the key ingredient (golden sea cucumber) alongside the product. Short benefit callout."),
    ("Social Proof",    "Grid of real-looking user review screenshots or star ratings with the product. Trust-builder layout."),
]

app = Flask(__name__, static_folder=None)

# ── Brand kit helpers ──────────────────────────────────────────────────────────
DEFAULT_BRAND = {
    "brand_name":      "",
    "tagline":         "",
    "brand_voice":     "",
    "price":           "",
    "product_url":     "",
    "key_claims":      [],
    "certifications":  [],
    "logo_path":       "",
    "product_photos":  [],
    "extra_notes":     "",
    "knowledge":       {},
    "products":        [],          # list of {id, name, sku, price, product_url, knowledge:{}}
    "active_product_id": None,
}

def load_brand() -> dict:
    if BRAND_JSON.exists():
        with open(BRAND_JSON, encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so new fields always present
        return {**DEFAULT_BRAND, **data}
    return dict(DEFAULT_BRAND)

def save_brand(data: dict):
    atomic_write_json(BRAND_JSON, data)

# ── Cumulative spend ledger ──────────────────────────────────────────────────
# Gemini's API does not expose a balance/remaining-credits endpoint for a given
# API key (verified against the official pricing docs) — there is no way to query
# "how much is left." This ledger instead tracks total REAL dollars spent so far,
# across all sessions, computed from the actual model used per generation.
def load_spend() -> float:
    if SPEND_JSON.exists():
        try:
            with open(SPEND_JSON, encoding="utf-8") as f:
                return float(json.load(f).get("total_usd", 0.0))
        except Exception:
            return 0.0
    return 0.0

_SPEND_LOCK = threading.Lock()

def add_spend(amount_usd: float) -> float:
    # Lock the whole read-modify-write: two concurrent generations both doing
    # load_spend()+add would otherwise clobber each other and undercount spend.
    with _SPEND_LOCK:
        total = load_spend() + amount_usd
        atomic_write_json(SPEND_JSON, {"total_usd": round(total, 4)}, indent=None)
    return total

def brand_to_prompt_block(brand: dict) -> str:
    """Convert brand kit + knowledge base into a full prompt context block."""
    lines = ["═══ BRAND & PRODUCT KNOWLEDGE ═══"]

    # ── Identity
    if brand.get("brand_name"):
        lines.append(f"\nBRAND: {brand['brand_name']}")
        lines.append(f"⚠️ HARD RULE: \"{brand['brand_name']}\" is the ONLY brand name/wordmark permitted "
                     f"anywhere in this ad. Any other brand name, logo, or wordmark — especially one copied "
                     f"from a reference image — is strictly forbidden.")
    if brand.get("_active_product_name"):
        lines.append(f"PRODUCT: {brand['_active_product_name']}")
    if brand.get("tagline"):
        lines.append(f"TAGLINE: {brand['tagline']}")
    # Price intentionally excluded — never shown in generated ads
    if brand.get("certifications"):
        lines.append(f"CERTIFICATIONS (show prominently): {', '.join(brand['certifications'])}")
        if any("halal" in c.lower() for c in brand["certifications"]):
            lines.append(
                "⚠️ HALAL LOGO RULE: if a Halal logo/badge appears anywhere in this ad — whether you're "
                "adding one fresh or copying a badge shape from a reference template — it MUST be the "
                "Malaysian JAKIM Halal logo (the official Malaysian government halal certification mark), "
                "never Indonesia's (MUI/BPJPH), Singapore's (MUIS), or any other country's halal mark. If "
                "you're not confident you can render the real JAKIM logo accurately, use a plain text badge "
                "reading \"Halal JAKIM\" instead of an inaccurate logo."
            )
    if brand.get("key_claims"):
        lines.append("KEY CLAIMS:")
        for c in brand["key_claims"]:
            lines.append(f"  • {c}")
    if brand.get("brand_voice"):
        lines.append(f"BRAND VOICE: {brand['brand_voice']}")


    # ── Knowledge base (the meaty part)
    kb = brand.get("knowledge", {})
    sections = [
        ("PRODUCT & INGREDIENTS", "product"),
        ("TARGET AUDIENCE",       "audience"),
        ("BENEFITS BY PROBLEM",   "benefits"),
        ("PROOF & TESTIMONIALS",  "proof"),
        ("OBJECTIONS & ANSWERS",  "objections"),
        ("VS COMPETITORS",        "competitors"),
    ]
    for title, key in sections:
        val = (kb.get(key) or "").strip()
        if val:
            lines.append(f"\n{title}:\n{val}")

    # ── Creative rules
    if brand.get("extra_notes"):
        lines.append(f"\nCREATIVE RULES (must follow):\n{brand['extra_notes']}")

    lines.append("\n═══════════════════════════════")
    return "\n".join(lines)

# ── Image helpers ──────────────────────────────────────────────────────────────
# Reference images are only used by the model to read the product label, layout,
# and style — they do NOT set the output resolution (that's imageConfig.aspectRatio).
# So a huge 4472px, multi-MB source photo just inflates upload + model-ingest time
# for zero quality gain. Cap the longest edge before sending; this is what keeps a
# Nano Banana Pro render under a minute even when a brand photo is very large.
_B64_MAX_EDGE = 1280

def img_to_b64(path: str):
    ext = Path(path).suffix.lower().strip(".")
    raw_mime = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg",
                "webp":"image/webp","gif":"image/gif"}.get(ext,"image/png")
    try:
        import io
        from PIL import Image as _Img
        im = _Img.open(path)
        w, h = im.size
        scale = _B64_MAX_EDGE / float(max(w, h))
        if scale < 1.0:  # only ever downscale, never upscale
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), _Img.LANCZOS)
        elif raw_mime != "image/gif":
            # already small enough and not animated — send original bytes untouched
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode(), raw_mime
        # Preserve transparency (cut-out product PNGs) as PNG; everything else -> JPEG.
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        buf = io.BytesIO()
        if has_alpha:
            im.convert("RGBA").save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode(), "image/png"
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception as e:
        print(f"[img_to_b64] resize skipped for {Path(path).name}: {e}")
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode(), raw_mime

def call_gemini_image(prompt, ref_paths, model,
                      winning_ad_path=None, product_image_path=None, texture_ref_path=None, is_tweak=False):
    """
    Single-turn API call with clearly labelled images.
    Image generation models require everything in ONE user turn.
    Order: text label → image → text label → image → main prompt.
    """
    api_key = os.environ.get("GEMINI_API_KEY","")
    if not api_key:
        return None, "GEMINI_API_KEY not set"

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")

    winning_ad  = winning_ad_path
    product_img = product_image_path

    valid_refs = [r for r in (ref_paths or []) if r and os.path.exists(str(r))]
    if not winning_ad and len(valid_refs) >= 1:
        winning_ad = valid_refs[0]
    if not product_img and len(valid_refs) >= 2:
        product_img = valid_refs[-1]

    print(f"[gemini] model={model}")
    print(f"[gemini] winning_ad={Path(winning_ad).name if winning_ad else 'NONE'}")
    print(f"[gemini] product_img={Path(product_img).name if product_img else 'NONE'}")

    # Single user turn — all parts in one message
    # Label each image WITH TEXT before the image so Gemini reads the label first
    parts = []

    # ── PRODUCT FIRST — establish product identity before any other image.
    # Gemini weights earlier content most heavily; showing the product first
    # locks in the correct bottle/label before the template can anchor it to
    # the wrong product shape.
    if product_img and os.path.exists(product_img):
        b64, mime = img_to_b64(product_img)
        parts.append({"text":
            "=== IMAGE 1: YOUR ACTUAL PRODUCT — REPRODUCE THIS EXACTLY ===\n"
            "Study every detail of this product photo before reading anything else."})
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        parts.append({"text":
            "⚠️ IMAGE 1 IS THE ONLY PRODUCT ALLOWED IN THE OUTPUT.\n\n"
            "Lock in these details from IMAGE 1 right now:\n"
            "- Exact container shape and silhouette (do not round, slim, or alter it)\n"
            "- Exact label: all text, logo, colors, layout — character-for-character\n"
            "- Exact cap/lid: shape, color, material\n"
            "- Exact surface finish: matte, glossy, opaque, or translucent\n"
            "- Exact packaging color (the body of the bottle/jar)\n\n"
            "HARD RULES — breaking any of these is a generation failure:\n"
            "✗ Do NOT replace with a dark amber/brown glass 'herbal tonic' bottle\n"
            "✗ Do NOT use a generic unlabeled bottle, cup, glass, bowl, or tin\n"
            "✗ Do NOT show only a poured liquid — the branded packaging must be visible\n"
            "✗ Do NOT use IMAGE 2's product — that belongs to a different brand\n"
            "✓ Every appearance of the product in the scene must match IMAGE 1 exactly"})

    if winning_ad and os.path.exists(winning_ad):
        b64, mime = img_to_b64(winning_ad)
        if is_tweak:
            parts.append({"text": "=== IMAGE 2: STYLE REFERENCE — VIBE & AESTHETIC GUIDE ONLY ==="})
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            parts.append({"text":
                "IMAGE 2 is a style reference for mood, lighting, background feel, and color palette only.\n"
                "Do NOT copy its layout or reproduce it as a collage.\n"
                "The product in IMAGE 2 belongs to a different brand — ignore it completely. Use only IMAGE 1's product."})
        else:
            parts.append({"text": "=== IMAGE 2: WINNING AD — LAYOUT & STYLE TEMPLATE ==="})
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            parts.append({"text":
                "IMAGE 2 is the layout template. Clone its composition, background treatment, lighting, graphic elements "
                "(panels, badges, icons, dividers), and overall structure.\n\n"
                "REPLACE in IMAGE 2:\n"
                "1. The product → swap with IMAGE 1's product in the exact same position, scale, angle, and role\n"
                "2. All text → replace with the copy in the main prompt below\n"
                "3. Any brand name/logo in IMAGE 2 → remove completely; use only the brand name from the prompt\n\n"
                "KEEP from IMAGE 2: layout, composition, background type, lighting, graphic element positions and style.\n\n"
                "⚠️ The product shown in IMAGE 2 belongs to a DIFFERENT unrelated brand — do not render it. "
                "Only IMAGE 1's product appears in the output."})

    if texture_ref_path and os.path.exists(texture_ref_path):
        b64, mime = img_to_b64(texture_ref_path)
        parts.append({"text": "=== IMAGE 3: LIQUID/CONTENTS TEXTURE REFERENCE ONLY ==="})
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        parts.append({"text":
            "Use this only for the color/clarity/viscosity of the product's liquid contents if they appear poured or exposed. "
            "Ignore the bottle and any props in this image — packaging always comes from IMAGE 1."})


    if parts:
        parts.append({"text":
            "⚠️ The labels above (\"=== IMAGE 1... ===\", \"=== IMAGE 2... ===\", \"=== IMAGE 3... ===\" and every "
            "ALL-CAPS instruction heading like PRODUCT:, DO NOT copy, etc.) are instructions for you to read — "
            "they are NOT text that exists on the product or in the scene. Never render any of these labels, "
            "brackets, or headings as visible text anywhere in the output image, including on the product label "
            "itself. The product label must show ONLY the real packaging text visible in the product reference "
            "image, nothing else."})

    parts.append({"text": "\n\n" + prompt})

    payload = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "4:5"},
            # Lower than the model default (~1.0) so product/brand fidelity stays
            # consistent across multiple generations from the same reference — the
            # camera-angle/layout variation notes already inject controlled variety,
            # so we don't need high sampling temperature to get different-looking ads.
            "temperature": 0.65
        }
    }).encode()

    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")

    # Retry with exponential backoff. A transient per-minute rate limit (429) or a
    # 5xx/network blip should NOT kill the whole render — the old code returned on
    # the first 429, so one momentary rate-limit spike wasted the generation. We
    # only give up immediately when the 429 is a HARD billing/credit exhaustion
    # (retrying can't fix that until the user tops up).
    MAX_ATTEMPTS = 4
    # Nano Banana Pro can legitimately take well over a minute, especially when the
    # service is busy — 120s was cutting off valid renders. Give it real headroom.
    READ_TIMEOUT = 180
    body = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                body = json.loads(resp.read())
            break  # success
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:500]
            low = err.lower()
            print(f"[gemini] HTTP {e.code} (attempt {attempt}/{MAX_ATTEMPTS}): {err}")
            if e.code == 429:
                # Hard credit/billing exhaustion — retrying is pointless.
                if any(w in low for w in ("credit", "billing", "depleted")):
                    return None, (QUOTA_ERR_PREFIX + "Your Gemini API credits are depleted (or the key is "
                                  "rate-limited). Top up billing at https://ai.studio/projects, then try again.")
                # Transient rate limit (RESOURCE_EXHAUSTED / RPM) — honour Retry-After, else back off.
                if attempt < MAX_ATTEMPTS:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    delay = float(ra) if (ra and str(ra).strip().isdigit()) else min(2 ** attempt, 30)
                    print(f"[gemini] rate-limited — backing off {delay:.0f}s then retrying")
                    time.sleep(delay)
                    continue
                return None, (QUOTA_ERR_PREFIX + "Gemini API rate limit hit (HTTP 429) after several retries. "
                              "Wait a moment and try again, or check billing at https://ai.studio/projects.")
            # Transient server errors — back off and retry.
            if e.code in (500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                delay = min(2 ** attempt, 30)
                print(f"[gemini] server error {e.code} — retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            return None, f"HTTP {e.code}: {err}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            # NOTE: on Python 3.9 socket.timeout is NOT a subclass of TimeoutError,
            # so it must be listed explicitly — otherwise a read timeout escapes
            # this handler and crashes the whole request with a 500 (this was the
            # "failed rendering new image" bug).
            reason = getattr(e, "reason", e)
            is_timeout = isinstance(e, (socket.timeout, TimeoutError)) or \
                         isinstance(reason, (socket.timeout, TimeoutError))
            # A read timeout means the model itself is slow — retrying the same slow
            # request up to 4x (each up to READ_TIMEOUT) would make the user wait many
            # minutes, so cap timeout retries at 2. The caller also falls back to the
            # faster draft model if the final model keeps timing out.
            limit = 2 if is_timeout else MAX_ATTEMPTS
            print(f"[gemini] {'timeout' if is_timeout else 'network error'} "
                  f"(attempt {attempt}/{limit}): {e}")
            if attempt < limit:
                time.sleep(min(2 ** attempt, 10))
                continue
            if is_timeout:
                return None, ("Gemini took too long to render this image and timed out. This usually means "
                              "the service is busy — please try generating again in a moment.")
            return None, f"Network error contacting Gemini: {e}"
    if body is None:
        return None, "Gemini request failed after retries"
    for part in body.get("candidates",[{}])[0].get("content",{}).get("parts",[]):
        for key in ("inlineData","inline_data"):
            if key in part:
                return base64.b64decode(part[key]["data"]), None
    return None, "No image in response"

def call_gemini_final(prompt, ref_paths):
    """Final quality — uses Nano Banana Pro which accepts reference images."""
    return call_gemini_image(prompt, ref_paths, FINAL_MODEL)

def extract_winning_ad_style(image_path: str) -> dict:
    """
    STEP 2: Run a dedicated Gemini vision call on the winning ad image.
    Returns structured JSON describing the ad's visual design style.
    If extraction fails, returns None — app continues to work without it.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not os.path.exists(image_path):
        print("[StyleExtract] Skipped — no key or image not found")
        return None

    prompt = """You are a professional ad creative analyst. Study this winning advertisement image carefully.

Return ONLY a raw JSON object — no explanation, no markdown, no code blocks. Just the JSON.

{
  "layout": {
    "structure": "describe the overall layout e.g. split left-right, single column, centered product",
    "product_placement": "exactly where the product appears on canvas",
    "headline_placement": "exactly where the headline text sits",
    "cta_placement": "where the CTA is, or 'none' if absent",
    "negative_space": "how much empty breathing room exists e.g. minimal, moderate, generous 40%+"
  },
  "colors": {
    "background": "exact hex if visible, or description",
    "background_description": "describe the background type: flat solid, gradient, sunburst, textured, photographic scene",
    "dominant_color": "hex or description of the most used color",
    "dominant_description": "what element uses this color",
    "accent_color": "hex or description of the secondary color",
    "accent_description": "what element uses this accent color",
    "text_color_headline": "hex or description of headline text color",
    "text_color_subheadline": "hex or description of subheadline text color",
    "cta_color": "hex or description of CTA element, or 'none'"
  },
  "typography": {
    "headline_weight": "bold, medium, or light",
    "headline_case": "uppercase, title case, or lowercase",
    "headline_size": "describe relative size: small, medium, large, very large",
    "font_style": "sans-serif, serif, or script",
    "headline_line_breaks": "describe how text wraps e.g. one long line, stacked short lines",
    "subheadline_weight": "bold, medium, or regular",
    "subheadline_size": "describe relative size compared to headline"
  },
  "mood": {
    "tone": "choose: clinical, warm, urgent, premium, playful, calm, trustworthy",
    "energy_level": "choose: high energy urgent, medium, low energy quiet confident",
    "lighting": "flat natural, bright studio, dark moody, gradient soft, dramatic",
    "overall_feel": "write one sentence describing the brand feel"
  },
  "composition": {
    "style": "minimalist, balanced, busy, or asymmetric",
    "has_human": true or false,
    "has_background_scene": true or false,
    "background_type": "flat solid color, gradient, real photograph, illustrated scene",
    "overlay_style": "none, gradient overlay, solid color block",
    "props": "list any supporting props e.g. glass of water, leaves, none",
    "product_style": "how the product appears: floating, standing on surface, held by hand"
  },
  "graphic_elements": [
    "list every non-text/non-product graphic element visible, with its position",
    "e.g. 'arrow pointing from headline to product, top-left to center'",
    "e.g. 'circular badge/sticker top-right corner with discount text'",
    "e.g. 'pointer/callout line connecting label to product feature'",
    "e.g. 'icon row of checkmarks/bullets along the bottom'",
    "if none, return an empty array"
  ],
  "visual_hierarchy": {
    "first_attention": "what catches the eye first",
    "second_attention": "what the eye moves to second",
    "third_attention": "third focal point",
    "fourth_attention": "fourth focal point or 'none'"
  },
  "forbidden_elements": [
    "list specific visual elements that are NOT present in this ad and should NOT appear in any recreation",
    "e.g. NO speech bubbles, NO sunburst background, NO certification badges",
    "e.g. NO gradient on background, NO warm orange tones, NO cluttered layout"
  ],
  "size": "detected or assumed canvas size e.g. 1080x1350 portrait or 1080x1080 square"
}"""

    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(image_path)
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",          raw.strip())
        style = json.loads(raw)
        print(f"[StyleExtract] OK — tone={style.get('mood',{}).get('tone')} "
              f"structure={style.get('layout',{}).get('structure','')[:40]}")
        return style
    except Exception as e:
        print(f"[StyleExtract Error] {e}")
        return None


def score_generated_ad(style_json: dict, generated_image_path: str) -> dict:
    """
    STEP 5: Compare the generated ad against the extracted style brief.
    Returns {match_score, biggest_mismatch, verdict} or None on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not style_json or not os.path.exists(generated_image_path):
        return None
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(generated_image_path)
        prompt = (f"Compare these two things:\n"
                  f"1. This style brief (JSON): {json.dumps(style_json)}\n"
                  f"2. The generated ad image I am sending you\n\n"
                  f"Rate how well the generated ad matches the style brief.\n"
                  f"Return ONLY raw JSON, no explanation:\n"
                  f'{{"match_score": (number 1-10), '
                  f'"biggest_mismatch": "one sentence", '
                  f'"verdict": "good" or "regenerate"}}')
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",          raw.strip())
        result = json.loads(raw)
        print(f"[MatchScore] {result.get('match_score')}/10 — {result.get('verdict')}")
        return result
    except Exception as e:
        print(f"[MatchScore Error] {e}")
        return None


def _vision_call(prompt_text: str, image_path: str, timeout: int = 30) -> str:
    """Shared helper: send one image + prompt to the vision model, return text."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not os.path.exists(image_path):
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    b64, mime = img_to_b64(image_path)
    payload = json.dumps({"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": b64}},
        {"text": prompt_text}
    ]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        return next((p["text"] for p in parts if "text" in p), "")
    except Exception as e:
        # Previously silent — a failed vision call (timeout, rate limit, bad image)
        # meant product_visual/product_section silently fell back to a much weaker
        # generic description with no visible sign anything went wrong. Logging the
        # real reason makes "Gemini picked the wrong product" cases diagnosable.
        print(f"[vision] FAILED for {Path(image_path).name}: {e}")
        return ""

def analyze_ad_layout(image_path: str) -> str:
    """
    Extract ONLY color palette and visual mood from winning ad.
    This description goes into the Gemini prompt as TEXT — the image itself
    is NOT sent to Gemini, to prevent Gemini from copying text from the reference.
    """
    return _vision_call(
        "Describe ONLY the visual style of this ad — colors, mood, background. "
        "Do NOT describe any text, words, or typography in the image. "
        "Cover these 4 things only:\n"
        "1. BACKGROUND: exact color(s), gradient direction, texture (e.g. 'light blue gradient top-to-bottom, soft')\n"
        "2. COLOR PALETTE: list every distinct background/accent color as hex values (e.g. #E8F4FD, #2B5F8E)\n"
        "3. MOOD: one sentence describing the visual feel (e.g. 'clean, professional, medical, warm')\n"
        "4. PRODUCT PLACEMENT: where the product bottle sits (e.g. 'centered, large, white background behind it')\n"
        "Output these 4 points only. No mention of text, headlines, or copy.",
        image_path
    )

def analyze_product_visual(image_path: str) -> str:
    """Vision pass: describe the product bottle/packaging so the AI can render it exactly."""
    return _vision_call(
        "You are a product photographer's assistant. Describe this product image precisely "
        "so an AI image generator can render it identically in an ad. Cover:\n"
        "1. Container: shape, size, material (glass/plastic/HDPE), transparency\n"
        "2. Label: exact colors, font styles, all text visible on the label\n"
        "3. Cap/lid: shape, color, material\n"
        "4. Overall color mood: dominant colors in order\n"
        "5. Any icons, badges, or graphic elements on the packaging\n"
        "6. One-line product description (e.g. 'Squat translucent HDPE bottle, "
        "bright golden-yellow label, white ribbed screw cap')\n"
        "Be specific and literal — describe only what you see.",
        image_path
    )

# ── Persistent product-visual cache ─────────────────────────────────────────
# analyze_product_visual() is a paid vision call and, under a 429, returns "" —
# which silently drops the textual product grounding and spikes hallucination
# (a wrong/generic bottle instead of the real branded jar). Cache each photo's
# description to disk, keyed by path + mtime, so it's computed once ever and
# survives restarts. Critically: if a fresh vision call fails (rate limit), fall
# back to the last good description for that same photo instead of nothing — so
# a transient 429 can no longer degrade product fidelity.
PRODUCT_VISUAL_JSON = _TMP / "product_visuals.json"

def _load_product_visuals() -> dict:
    if PRODUCT_VISUAL_JSON.exists():
        try:
            with open(PRODUCT_VISUAL_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def product_visual_for(image_path: str) -> str:
    """Precise textual description of the product photo, cached to disk. On a
    failed/rate-limited vision call, returns the last good cached description for
    this photo so the prompt never loses product grounding."""
    if not image_path or not os.path.exists(image_path):
        return ""
    try:
        mtime = int(os.path.getmtime(image_path))
    except OSError:
        mtime = 0
    key = f"{image_path}::{mtime}"
    cache = _load_product_visuals()
    if cache.get(key):
        return cache[key]
    desc = analyze_product_visual(image_path) or ""
    if desc:
        cache[key] = desc
        atomic_write_json(PRODUCT_VISUAL_JSON, cache)
        return desc
    # Vision call failed (e.g. 429). Reuse the most recent good description for
    # this same photo path (any mtime) rather than dropping grounding entirely.
    fallback = next((v for k, v in cache.items()
                     if k.rsplit("::", 1)[0] == image_path and v), "")
    if fallback:
        print(f"[vision] using cached fallback description for {Path(image_path).name}")
    return fallback

# ── Product photo ↔ template auto-matching ──────────────────────────────────
# Fixed vocabulary both sides get bucketed into, so matching a product photo to
# a template's product presentation is a simple category lookup rather than
# fragile free-text comparison.
PRODUCT_PRESENTATION_CATEGORIES = [
    "bottle_alone", "poured_glass", "held_hand", "group_bottles", "lifestyle_scene",
]

def _bucket_presentation(text: str) -> str:
    """Map a free-text presentation description (from either a template's
    extracted product_style or a photo's own classification) into one of
    PRODUCT_PRESENTATION_CATEGORIES, so the two sides can be compared directly."""
    t = (text or "").lower()
    if any(w in t for w in ("pour", "glass", "cup", "tumbler")):
        return "poured_glass"
    if any(w in t for w in ("hand", "held", "holding")):
        return "held_hand"
    if any(w in t for w in ("group", "multiple", "several", "set of")):
        return "group_bottles"
    if any(w in t for w in ("prop", "lifestyle", "scene", "spoon", "ice", "table setting")):
        return "lifestyle_scene"
    return "bottle_alone"

def load_photo_styles() -> dict:
    if PHOTO_STYLE_JSON.exists():
        with open(PHOTO_STYLE_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_photo_styles(data: dict):
    # Written from parallel generation slots (classify_product_photo), and this
    # file is in SNAPSHOT_PATHS — a bare open("w") could truncate it.
    atomic_write_json(PHOTO_STYLE_JSON, data)

def classify_product_photo(image_path: str) -> str:
    """Cheap vision pass: bucket how this product photo presents the product
    (bottle alone / poured into a glass / held in a hand / group / lifestyle
    scene with props) so it can be auto-matched against a template's own
    presentation style. Cached to disk — only runs once per photo ever."""
    cache = load_photo_styles()
    if image_path in cache:
        return cache[image_path]
    raw = _vision_call(
        "Look at this product photo. In 2-4 words, describe how the product is presented. "
        "Use language like: 'standing alone', 'poured into a glass', 'held in a hand', "
        "'group of bottles', 'lifestyle scene with props'. Reply with ONLY the short phrase.",
        image_path, timeout=15
    )
    category = _bucket_presentation(raw)
    cache[image_path] = category
    save_photo_styles(cache)
    return category

def _photo_is_cutout(path: str) -> bool:
    """True if this photo is a clean isolated cut-out — either a real transparent
    (alpha) image or a filename that marks one. A cut-out is the ideal IMAGE 1
    identity reference: nothing else in frame for the model to copy."""
    if not path:
        return False
    try:
        from PIL import Image as _Img
        with _Img.open(path) as im:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                return True
    except Exception:
        pass
    n = Path(path).name.lower()
    return any(w in n for w in ("removebg", "cutout", "cut-out", "transparent", "nobg", "no-bg"))

def _best_product_identity_photo(photos: list) -> str:
    """Choose the best product-IDENTITY reference (the IMAGE 1 slot) for fidelity —
    NOT for matching a template's scene. An isolated cut-out (transparent
    background) is ideal: nothing else in frame for the model to copy, so it locks
    onto the packaging. Falls back to filename hints, then the first photo."""
    if not photos:
        return ""
    # 1) a transparent cut-out (alpha channel or filename hint)
    for p in photos:
        if _photo_is_cutout(p):
            return p
    # 2) last resort — first photo
    return photos[0]

def pick_product_photo_for_template(photos: list, template_product_style: str) -> str:
    """Auto-match: given the candidate product photos and the chosen template's
    own product presentation style, pick whichever photo's classified category
    matches. Falls back to the first photo if nothing matches or classification
    is unavailable — never blocks generation on this being a bit uncertain."""
    if not photos:
        return ""
    if len(photos) == 1 or not template_product_style:
        return photos[0]
    target = _bucket_presentation(template_product_style)
    for p in photos:
        if classify_product_photo(p) == target:
            print(f"[photo-match] template wants '{target}' → picked {Path(p).name}")
            return p
    print(f"[photo-match] no photo matches '{target}' — using first photo as fallback")
    return photos[0]

def fetch_product_facts(product_url: str) -> str:
    """
    Fetch live product facts from the product URL.
    Tries Shopify JSON first (richest data), falls back to HTML scraping.
    Returns a formatted string of facts to ground copy generation.
    """
    if not product_url or not product_url.startswith("http"):
        return ""

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    facts   = []

    # ── Try 1: Shopify product JSON endpoint ──────────────────────────────────
    try:
        json_url = product_url.rstrip("/") + ".json"
        req = urllib.request.Request(json_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        prod = data.get("product", {})
        if prod.get("title"):
            facts.append(f"PRODUCT NAME: {prod['title']}")
        if prod.get("body_html"):
            clean = re.sub(r"<[^>]+>", " ", prod["body_html"])
            clean = re.sub(r"\s+", " ", clean).strip()
            facts.append(f"DESCRIPTION: {clean[:800]}")
        tags = prod.get("tags", "")
        if tags:
            facts.append(f"TAGS/KEYWORDS: {tags[:200]}")
        for v in prod.get("variants", [])[:2]:
            if v.get("title") and v["title"] != "Default Title":
                facts.append(f"VARIANT: {v['title']}")
        print(f"[web-fetch] Shopify JSON OK — {len(facts)} facts from {json_url}")
    except Exception as e:
        print(f"[web-fetch] Shopify JSON failed ({e}) — trying HTML")

    # ── Try 2: Raw HTML scraping if Shopify JSON failed or had little data ────
    if len(facts) < 2:
        try:
            req = urllib.request.Request(product_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Page title
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m: facts.append(f"PAGE TITLE: {m.group(1).strip()[:150]}")

            # Meta description
            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{10,})',
                html, re.I)
            if m: facts.append(f"META DESC: {m.group(1).strip()[:300]}")

            # Open Graph description
            m = re.search(
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{10,})',
                html, re.I)
            if m: facts.append(f"OG DESC: {m.group(1).strip()[:300]}")

            # Look for JSON-LD product schema
            ld = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                           html, re.S | re.I)
            if ld:
                try:
                    schema = json.loads(ld.group(1))
                    if isinstance(schema, dict):
                        if schema.get("description"):
                            facts.append(f"SCHEMA DESC: {schema['description'][:400]}")
                        if schema.get("name"):
                            facts.append(f"SCHEMA NAME: {schema['name']}")
                except Exception:
                    pass

            print(f"[web-fetch] HTML scrape OK — {len(facts)} facts from {product_url}")
        except Exception as e:
            print(f"[web-fetch] HTML scrape failed: {e}")

    if not facts:
        print(f"[web-fetch] No facts retrieved from {product_url}")
        return ""

    result = "\n".join(facts)
    print(f"[web-fetch] Total: {len(result)} chars of live product data")
    return result


def _text_call(prompt_text: str, timeout: int = 30) -> str:
    """Call the text model and return the response text."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt_text}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        return next((p["text"] for p in parts if "text" in p), "")
    except Exception as e:
        print(f"[text-call] Error: {e}")
        return ""


def verify_and_clean_copy(copy_data: dict, product_facts: str) -> dict:
    """
    Typo & fact verification pass.
    Runs the generated copy through the text model to:
    1. Fix all Bahasa Malaysia spelling errors
    2. Ensure all claims match real product facts
    3. Remove any hallucinated words or phrases
    Returns a corrected copy_data dict.
    """
    if not copy_data:
        return copy_data

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return copy_data

    # Serialize current copy for verification
    copy_json = json.dumps(copy_data, ensure_ascii=False, indent=2)

    facts_section = f"""
FAKTA PRODUK SEBENAR (dari laman web):
{product_facts}
""" if product_facts else ""

    prompt = f"""Kamu adalah editor iklan Malaysia yang pakar dalam Bahasa Malaysia dan Bahasa Inggeris.

TUGASAN: Semak dan betulkan copy iklan di bawah.

{facts_section}

COPY UNTUK DISEMAK:
{copy_json}

ARAHAN SEMAKAN:
1. EJAAN — betulkan SEMUA ejaan Bahasa Malaysia yang salah.
   Contoh kesalahan biasa: "Ingredielas"→"Ingredien", "Nidiak"→"Tidak", "Sembuataa"→"Semulajadi"
2. PERKATAAN REKAAN — buang mana-mana perkataan yang tidak wujud dalam Bahasa Malaysia
3. FAKTA — pastikan semua tuntutan sepadan dengan fakta produk di atas
4. AYAT — pastikan setiap ayat adalah Bahasa Malaysia yang betul dan boleh difahami
5. PENDEK — setiap bullet point maksimum 5 patah perkataan
6. JANGAN ubah struktur JSON atau nama kunci (keys)
7. JANGAN ubah teks yang sudah betul tanpa sebab

Kembalikan JSON yang telah dibetulkan sahaja (tiada teks lain, tiada ``` fences):"""

    raw = _text_call(prompt, timeout=30)
    if not raw:
        return copy_data

    # Strip code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$",          "", raw.strip())

    try:
        verified = json.loads(raw)
        print(f"[verify-copy] Verified — h1='{verified.get('h1','')}' | left_bullets={verified.get('left_bullets',[])[:1]}")
        return verified
    except Exception as e:
        print(f"[verify-copy] JSON parse error: {e} — using original copy")
        return copy_data


def _get_logo_path() -> str:
    """Read logo path from Brand Setup. Returns path string or ''."""
    try:
        brand = load_brand()
        path  = brand.get("logo_path", "")
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return ""


def _get_zone_text_color(img, x1, y1, x2, y2):
    """
    Sample background brightness in a zone and return a contrasting dark color.
    Light background → near-black. Dark background → also dark (with white backing).
    Returns (text_color, needs_white_backing).
    """
    try:
        from PIL import Image as PILImage
        region = img.crop((x1, y1, x2, y2)).convert("L")
        pixels = list(region.getdata())
        avg    = sum(pixels) / max(len(pixels), 1)
        # Always use dark text — just vary the backing opacity
        if avg < 100:        # very dark bg → white backing, dark text
            return (15, 15, 15, 255), True
        elif avg < 160:      # medium bg → semi-white backing, dark text
            return (20, 20, 20, 255), True
        else:                # light bg → no backing needed, dark text
            return (25, 25, 25, 255), False
    except Exception:
        return (20, 20, 20, 255), True


def _zone_flatness(img, x1, y1, x2, y2):
    """
    Standard deviation of luminance in a region — LOW value means a flat/simple
    area (good for placing text), HIGH value means busy/detailed (product, etc.).
    """
    try:
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(img.width, int(x2)), min(img.height, int(y2))
        if x2 <= x1 or y2 <= y1:
            return 999
        region = img.crop((x1, y1, x2, y2)).convert("L")
        pixels = list(region.getdata())
        n = len(pixels)
        if n == 0:
            return 999
        avg = sum(pixels) / n
        var = sum((p - avg) ** 2 for p in pixels) / n
        return var ** 0.5
    except Exception:
        return 999


def apply_text_overlay(img_path: str, copy_data: dict, out_path: str,
                       img_idx: int = 0, brand_colors: dict = None,
                       layout_template: int = None, extracted_style: dict = None) -> bool:
    """
    PIL text overlay — single pass, zero duplicates.

    Text placement is chosen FREELY/DYNAMICALLY per image: PIL scans candidate
    regions of the generated image and picks whichever is flattest/cleanest
    (least busy) for the headline block, then picks the cleanest bottom corner
    (or full bottom strip) for the CTA — so placement adapts to wherever Gemini
    actually left empty space, instead of a fixed template.

    Headline candidates:
      0 — top_center:       centered headline top, CTA full-width bottom strip
      1 — left_block:       left-aligned headline block, CTA pill bottom-left/right
      2 — top_left_compact: compact top-left headline, CTA pill bottom-left/right

    Fonts: Montserrat-Bold for H1, Poppins for everything else.
    Colors: dark/black, auto-contrasted against Gemini background.
    Logo: loaded from Brand Setup, pasted as-is with transparency preserved.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[pil] Pillow not available"); return False

    if not copy_data:
        return False

    # ── Only H1, H2, CTA — no bullets, no comparison columns ─────────────────
    h1  = (copy_data.get("h1")  or "").strip()
    h2  = (copy_data.get("h2")  or "").strip()
    cta = (copy_data.get("cta") or "").strip()

    if not any([h1, h2, cta]):
        return False

    # ── Pull text direction/style from the winning ad's extracted style ────────
    headline_placement = ""
    headline_case      = ""
    headline_size_word = ""
    h1_color_override  = None
    if extracted_style:
        layout_b = extracted_style.get("layout", {}) or {}
        typo_b   = extracted_style.get("typography", {}) or {}
        colors_b = extracted_style.get("colors", {}) or {}
        headline_placement = (layout_b.get("headline_placement") or "").lower()
        headline_case      = (typo_b.get("headline_case") or "").lower()
        headline_size_word = (typo_b.get("headline_size") or "").lower()

        hex_candidate = (colors_b.get("text_color_headline") or "").strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", hex_candidate):
            h1_color_override = hex_candidate if hex_candidate.startswith("#") else f"#{hex_candidate}"

    if headline_case == "uppercase":
        h1 = h1.upper()
    elif headline_case == "lowercase":
        h1 = h1.lower()

    # Scale headline size to match the winning ad's relative headline size
    SIZE_SCALE = {"small": 0.78, "medium": 0.9, "large": 1.0, "very large": 1.15}
    h1_scale = next((v for k, v in SIZE_SCALE.items() if k in headline_size_word), 1.0)

    # ── Font loader — Montserrat-Bold for H1, Poppins for everything else ─────
    def fnt(path, size):
        for p in [path, FONT_FALLBACK]:
            if p and os.path.exists(p):
                try: return ImageFont.truetype(p, int(size))
                except Exception: pass
        return ImageFont.load_default()

    # ── Text measurement using textbbox — no hardcoded sizes ──────────────────
    def measure(font, text):
        """Returns (width, height) of text using textbbox for accuracy."""
        try:
            bb = font.getbbox(text)
            return bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            sz = getattr(font, 'size', 14)
            return len(text) * sz // 2, sz

    def text_height(font, text="Ag"):
        """Accurate height of a line of text."""
        try: return font.getbbox(text)[3]
        except Exception: return getattr(font, 'size', 14)

    def text_width(font, text):
        try: return int(font.getlength(text))
        except Exception: return measure(font, text)[0]

    def wrap(text, font, max_w):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if text_width(font, t) <= max_w: cur = t
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines or [text]

    def put_text_backed(d, x, y, text, font, color, bpad=8, radius=8):
        """Draw text with white semi-transparent backing for readability."""
        w, h = measure(font, text)
        d.rounded_rectangle(
            [x - bpad, y - bpad // 2, x + w + bpad, y + h + bpad // 2],
            radius=radius, fill=(255, 255, 255, 185)
        )
        d.text((x, y), text, font=font, fill=color)

    # ── Load Gemini image ──────────────────────────────────────────────────────
    img  = Image.open(img_path).convert("RGBA")
    W, H = img.size
    cv   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d    = ImageDraw.Draw(cv)
    PAD  = int(W * 0.055)
    DARK = (18, 18, 18, 255)

    # ── Brand colors for H1 — alternate Primary / Secondary per image ────────
    bc = brand_colors or {}

    def hex_to_rgba(hex_str, alpha=255):
        """Convert #RRGGBB to (R,G,B,A)."""
        h = hex_str.lstrip("#")
        if len(h) == 6:
            return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16), alpha)
        return (18, 18, 18, alpha)

    primary_hex   = bc.get("primary_color",   "#F5A800")
    secondary_hex = bc.get("secondary_color", "#D94F00")

    # Even images → Primary (#F5A800 gold), Odd images → Secondary (#D94F00 red-orange)
    # If the winning ad's headline color was extracted as a real hex, prefer that.
    if h1_color_override:
        h1_color = hex_to_rgba(h1_color_override)
    else:
        h1_color = hex_to_rgba(primary_hex) if img_idx % 2 == 0 else hex_to_rgba(secondary_hex)

    # ── Derive headline & CTA regions directly from the winning ad's layout
    # description — no fixed top-center/left-block/top-left templates. ────────
    cta_placement = ((extracted_style or {}).get("layout", {}) or {}).get("cta_placement", "").lower()

    def region_from(desc, default_y_frac):
        desc = desc or ""
        if "bottom" in desc:
            y_frac = 0.60
        elif "middle" in desc or "centre" in desc or ("center" in desc and "top" not in desc and "bottom" not in desc):
            y_frac = 0.38
        else:
            y_frac = default_y_frac
        if "right" in desc:
            align = "right"
        elif "center" in desc or "centre" in desc:
            align = "center"
        else:
            align = "left"
        return y_frac, align

    h_y_frac, h_align = region_from(headline_placement, default_y_frac=0.05)
    TXT_W = (W - PAD * 2) if h_align == "center" else int(W * 0.56) - PAD

    print(f"[pil] Image #{img_idx+1} H1 color: "
          f"{'override ' + h1_color_override if h1_color_override else ('Primary ' + primary_hex if img_idx % 2 == 0 else 'Secondary ' + secondary_hex)} "
          f"| headline_placement='{headline_placement}' -> y_frac={h_y_frac} align={h_align}")

    def draw_pill(text_str, font, anchor_x, anchor_y, align="left",
                   bg=(245, 168, 0, 248), fg=(18, 18, 18, 255), pad_h=22, pad_v=12):
        """Draw a rounded-rect CTA pill. anchor_x/anchor_y = top-left if align='left',
        or top-right corner (anchor_x = right edge) if align='right'."""
        tw = text_width(font, text_str)
        th = text_height(font, text_str)
        bw = tw + pad_h * 2
        bh = th + pad_v * 2
        if align == "right":
            x0 = anchor_x - bw
        else:
            x0 = anchor_x
        y0 = anchor_y
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh // 2, fill=bg)
        d.text((x0 + pad_h, y0 + pad_v), text_str, font=font, fill=fg)
        return bw, bh

    # ── Headline (H1) ──────────────────────────────────────────────────────────
    cur_y = int(H * h_y_frac) + (4 if h_y_frac > 0.05 else 20)
    h1_bottom = cur_y
    if h1:
        sz_h1 = max(30, int(H * 0.060 * h1_scale))
        f_h1  = fnt(FONT_H1, sz_h1)
        for line in wrap(h1, f_h1, TXT_W):
            lw = text_width(f_h1, line)
            lh = text_height(f_h1, line)
            if h_align == "center":
                x = (W - lw) // 2
            elif h_align == "right":
                x = W - PAD - lw
            else:
                x = PAD
            shadow_offset = max(2, int(lh * 0.06))
            d.text((x + shadow_offset, cur_y + shadow_offset), line, font=f_h1, fill=(0, 0, 0, 160))
            d.text((x, cur_y), line, font=f_h1, fill=h1_color)
            cur_y += lh + 6
        h1_bottom = cur_y

    # ── Subheadline (H2) ─────────────────────────────────────────────────────
    c2, _ = _get_zone_text_color(img, PAD, int(H * h_y_frac), W - PAD, min(H, h1_bottom + int(H * 0.18)))
    cur_y = h1_bottom + 14
    if h2:
        sz_h2 = max(18, int(H * 0.032))
        f_h2  = fnt(FONT_BODY, sz_h2)
        for line in wrap(h2, f_h2, TXT_W):
            lw = text_width(f_h2, line)
            lh = text_height(f_h2, line)
            if h_align == "center":
                x = (W - lw) // 2
            elif h_align == "right":
                x = W - PAD - lw
            else:
                x = PAD
            d.text((x + 2, cur_y + 2), line, font=f_h2, fill=(0, 0, 0, 120))
            d.text((x, cur_y), line, font=f_h2, fill=c2)
            cur_y += lh + 4

    # ── CTA — placement follows the winning ad's cta_placement ────────────────
    CTA_H, CTA_TOP = int(H * 0.115), 0
    if cta:
        sz_cta = max(22, int(H * 0.034))
        f_cta  = fnt(FONT_BOLD, sz_cta)
        if "right" in cta_placement:
            draw_pill(cta, f_cta, W - PAD, H - int(H*0.10) - PAD, align="right")
        elif any(k in cta_placement for k in ("center", "centre", "full", "bar", "strip", "bottom")) or not cta_placement:
            CTA_H   = int(H * 0.115)
            CTA_TOP = H - CTA_H
            d.rectangle([0, CTA_TOP, W, H], fill=(245, 168, 0, 248))
            cw_cta = text_width(f_cta, cta)
            ch_cta = text_height(f_cta, cta)
            d.text(((W - cw_cta) // 2, CTA_TOP + (CTA_H - ch_cta) // 2), cta, font=f_cta, fill=(18, 18, 18, 255))
        else:
            draw_pill(cta, f_cta, PAD, H - int(H*0.10) - PAD, align="left")

    # ── Logo — opposite corner from the headline ───────────────────────────────
    logo_path = _get_logo_path()
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            target_w = 110
            ratio  = target_w / logo.width
            logo_w, logo_h = target_w, int(logo.height * ratio)
            logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
            logo_x = PAD if h_align == "right" else W - logo_w - 16
            logo_y = 16
            if CTA_TOP and (logo_y + logo_h) > CTA_TOP - 4:
                logo_y = CTA_TOP - logo_h - 10
            cv.paste(logo, (logo_x, logo_y), logo)
        except Exception as e:
            print(f"[pil] Logo skipped: {e}")

    # ── Composite — called exactly ONCE ───────────────────────────────────────
    result = Image.alpha_composite(img, cv).convert("RGB")
    result.save(out_path, "PNG", optimize=True)
    print(f"[pil] Done — H1={bool(h1)} H2={bool(h2)} CTA={bool(cta)} layout={layout_template}")
    return True


def extract_benefits_from_knowledge(benefits_text: str, problem: str, keywords: list = None,
                                     exclude_keywords: list = None) -> list:
    """
    Extract product benefit bullets DIRECTLY from the user's Brand Setup text.
    No AI involved — copies exactly what the user wrote.

    Brand Setup benefit text varies in format across products:
    - Blank-line-separated blocks with a (often Malay) title line + plain
      description lines, no bullet markers (e.g. Jeli Gamat)
    - Blocks with English ALL-CAPS-ish headers ending in ":" and "-" bulleted
      lines (e.g. Gel Gamat)
    - A single flat "-" bulleted list with no title/header at all (e.g. Minyak Urut)

    `keywords`: explicit lowercase substrings (from PRODUCT_USE_CASES'
    benefit_keywords) to match against candidate bullets — far more reliable than
    matching the English use-case label against Malay-language headers.
    Falls back to bullets from everywhere if no keyword match (or none given),
    so this never returns empty as long as the user wrote ANY benefit text.

    `exclude_keywords`: other use cases' benefit_keywords — when the fallback has
    to pad with bullets from blocks this use case didn't directly match, it skips
    any block/bullet that belongs to a DIFFERENT specific use case (matches
    exclude_keywords), so e.g. a gastric ad never gets padded with accident-
    recovery bullets just because gastric has no dedicated block of its own.
    """
    if not benefits_text:
        return []

    keywords = [k.lower() for k in (keywords or [])]

    def _strip_marker(line: str) -> str:
        return line.lstrip("-•*").strip()

    # Meta-commentary lines (not real benefits) — exclude by known phrases or length,
    # since these tend to be long explanatory sentences rather than short bullets.
    META_MARKERS = ("observed time-to-results", "note:", "limited effect")
    def _is_meta(line: str) -> bool:
        low = line.lower()
        return any(m in low for m in META_MARKERS) or len(line) > 130

    # ── Split into blank-line-separated blocks ───────────────────────────────────
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", benefits_text) if b.strip()]
    blocks = []   # list of (title, [bullet_lines])
    for raw in raw_blocks:
        block_lines = [l.strip() for l in raw.split("\n") if l.strip() and not _is_meta(l.strip())]
        if not block_lines:
            continue
        all_bulleted = all(l.startswith(("-", "•", "*")) for l in block_lines)
        if all_bulleted:
            # Flat bulleted list, no separate title line (e.g. Minyak Urut) —
            # every line is a real bullet, none should be treated as a title.
            title, body = "", [_strip_marker(l) for l in block_lines]
        else:
            # First line is a title/header (e.g. "Kesihatan Sendi" or "PRIMARY (...):")
            title = block_lines[0]
            body  = [_strip_marker(l) for l in block_lines[1:]]
        body = [b for b in body if len(b) > 3]
        if body:
            blocks.append((title, body))

    if not blocks:
        bullets = [_strip_marker(l) for l in benefits_text.split("\n")
                   if len(_strip_marker(l)) > 3 and not _is_meta(l)]
        return bullets[:5]

    # ── Prefer bullets that match the explicit keyword list (checked against both
    # the block title AND each individual bullet, since granularity differs by product) ──
    matched_bullets = []
    if keywords:
        for title, body in blocks:
            title_low = title.lower()
            if any(kw in title_low for kw in keywords):
                matched_bullets.extend(body)
            else:
                for b in body:
                    if any(kw in b.lower() for kw in keywords):
                        matched_bullets.append(b)

    if len(matched_bullets) >= 3:
        return matched_bullets[:5]

    # ── Fallback: no clean match (or too few) — use bullets from every block,
    # EXCEPT blocks/bullets that belong to a different specific use case ────────
    exclude = [k.lower() for k in (exclude_keywords or [])]
    def _belongs_to_other_use_case(text_low):
        return exclude and any(kw in text_low for kw in exclude)

    all_bullets = []
    for title, body in blocks:
        if _belongs_to_other_use_case(title.lower()):
            continue
        for b in body:
            if _belongs_to_other_use_case(b.lower()):
                continue
            if b not in all_bullets:
                all_bullets.append(b)
    merged = matched_bullets + [b for b in all_bullets if b not in matched_bullets]
    return merged[:5]


def generate_copy_from_knowledge(framework_hint, problem, problem_ctx, brand,
                                  copy_h1="", copy_h2="", copy_cta="",
                                  product_facts="", vary_copy=False,
                                  previous_headlines=None,
                                  persona_key=None, angle_key=None, style_key=None) -> dict:
    """
    Step 1 of 2: Use the text model to generate typo-free, accurate ad copy
    from the brand knowledge base BEFORE image generation.
    Returns a structured dict with h1, h2, cta, left_bullets, right_bullets etc.
    Any fields the user already filled in manually are kept as-is.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"h1": copy_h1, "h2": copy_h2, "cta": copy_cta}

    product_name = brand.get("_active_product_name") or brand.get("brand_name") or "produk ini"
    brand_name   = brand.get("brand_name") or product_name
    kb           = brand.get("knowledge", {})
    price        = brand.get("price", "")
    certs        = ", ".join(brand.get("certifications", [])) or ""
    claims       = "\n".join(f"- {c}" for c in brand.get("key_claims", []))
    benefits_full = (kb.get("benefits")    or "").strip()
    # Redact mentions of OTHER use cases' specific conditions (e.g. strip "sendi"
    # out of a C-section ad's context) — these fields are general background
    # included in every generation regardless of use case, so a condition named
    # prominently here (even truthfully, as a general product fact) can pull the
    # headline off-topic. See redact_other_conditions() for why this is safer
    # than relying on an instruction alone.
    product_txt   = redact_other_conditions((kb.get("product")     or "").strip()[:500], product_name, problem)
    comp_txt      = redact_other_conditions((kb.get("competitors") or "").strip()[:400], product_name, problem)
    proof_txt     = redact_other_conditions((kb.get("proof")       or "").strip()[:300], product_name, problem)
    audience_txt  = (kb.get("audience")    or "").strip()[:300]

    # ── Benefits: prefer authored ground-truth bullets (e.g. from a supplied Word
    # doc) over fuzzy-matching Brand Setup text — authored bullets are exact and
    # can never cross-contaminate between use cases. Falls back to keyword
    # matching only for use cases with no authored content yet. ─────────────────
    authored_benefits = get_authored_benefits(product_name, problem)
    if authored_benefits:
        direct_benefits = random.sample(authored_benefits, min(5, len(authored_benefits)))
        print(f"[copy-gen] Authored benefits for '{problem}': {direct_benefits}")
    else:
        benefit_keywords = get_benefit_keywords(product_name, problem)
        other_keywords   = get_other_benefit_keywords(product_name, problem)
        direct_benefits = extract_benefits_from_knowledge(benefits_full, problem, keywords=benefit_keywords,
                                                            exclude_keywords=other_keywords)
        print(f"[copy-gen] Direct benefits from Brand Setup (keywords={benefit_keywords}): {direct_benefits}")

    # Format benefits for the prompt — built from the correctly use-case-scoped
    # direct_benefits above, NOT the raw full Benefits field. This used to take the
    # first 600 chars of the ENTIRE multi-segment Benefits text regardless of which
    # use case was active — since that field is now organized use-case-by-use-case
    # (Sakit Sendi first), it was mislabeling joint-pain content as e.g. "BENEFITS
    # for Pemulihan Selepas Bersalin Caesar", directly contradicting the correctly
    # scoped bullets elsewhere in the same prompt. This was the actual root cause
    # of copy pivoting to joint pain regardless of the persona/use case selected.
    benefits_txt = "\n".join(f"- {b}" for b in direct_benefits) if direct_benefits else ""

    # ── Build JSON template based on framework ───────────────────────────────
    is_vs_them = framework_hint == "US_VS_THEM"

    # Field hints point back at the ANGLE STRUCTURE block below when an angle is
    # selected, instead of a generic "short powerful headline" phrase — otherwise
    # the model tends to follow the generic hint and the angle's actual structural
    # formula (myth-vs-truth, trust-signal-first, question-not-answer, etc.) gets
    # diluted into a same-shaped headline with different wording.
    _has_angle = bool(angle_key and angle_key in ANGLES)
    _has_style = bool(style_key and style_key in STYLES)
    if _has_angle or _has_style:
        _ref = " and ".join(filter(None, ["ANGLE STRUCTURE" if _has_angle else "", "CREATIVE STYLE" if _has_style else ""]))
        h1_hint  = f"Headline — follow the {_ref} below exactly, max 7 words"
        h2_hint  = f"Subheadline — follow the {_ref} below exactly, max 10 words"
        cta_hint = f"CTA — follow the {_ref} below exactly, max 5 words (no price)"
    else:
        h1_hint  = "Short powerful headline max 7 words"
        h2_hint  = "Supporting subheadline max 10 words"
        cta_hint = "Call to action max 5 words (no price)"

    if is_vs_them:
        json_template = f"""{{
  "h1": "{h1_hint}",
  "h2": "{h2_hint}",
  "cta": "{cta_hint}",
  "left_header": "JENAMA LAIN",
  "left_bullets": ["competitor weakness 1 (max 5 words)", "weakness 2", "weakness 3", "weakness 4"],
  "right_header": "{brand_name}",
  "right_bullets": ["REAL benefit 1 from Brand Setup", "benefit 2", "benefit 3", "benefit 4"],
  "benefit_bullets": []
}}"""
    else:
        # All other frameworks: single column benefits, no comparison table
        json_template = f"""{{
  "h1": "{h1_hint}",
  "h2": "{h2_hint}",
  "cta": "{cta_hint}",
  "left_header": "",
  "left_bullets": [],
  "right_header": "",
  "right_bullets": [],
  "benefit_bullets": [
    "REAL benefit 1 from Brand Setup (max 6 words)",
    "REAL benefit 2 from Brand Setup",
    "REAL benefit 3 from Brand Setup",
    "REAL benefit 4 from Brand Setup"
  ]
}}"""

    framework_task = {
        "US_VS_THEM":    "US VS THEM comparison — two columns: competitor weaknesses vs brand benefits",
        "TESTIMONIAL":   "Testimonial style — focus on a customer result/story and key benefits",
        "PROBLEM_HOOK":  "Problem Hook — headline states the problem, subheadline is the solution, bullets are benefits",
        "BENEFIT":       "Benefit showcase — headline is bold claim, bullets list real product benefits",
    }.get(framework_hint, "Benefit showcase")

    # ── Build persona copy guidance block ───────────────────────────────────
    persona_copy_block = ""
    if persona_key and persona_key in PERSONAS:
        p = PERSONAS[persona_key]
        persona_copy_block = f"""
PERSONA CONTEXT — write for this specific audience:
{p['copy_voice']}
"""

    # ── Build persuasion-angle copy guidance block — independent of persona ──
    # This is the PRIMARY structural directive when an angle is selected — it defines
    # the STRUCTURE of h1/h2/cta (what role each field plays), not just a tone/flavor
    # to sprinkle on top of an otherwise-generic headline. Different angles must
    # produce genuinely different message shapes, not the same message reworded.
    angle_copy_block = ""
    if angle_key and angle_key in ANGLES:
        a = ANGLES[angle_key]
        angle_copy_block = f"""
━━━ ANGLE STRUCTURE — defines h1/h2/cta, not just tone ━━━
{a['label']} ({a['psychology']}): {a['copy_instruction']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # ── Creative Style — Clarity vs Curiosity Gap. Independent of angle: angle is
    # WHAT the message argues, style is HOW directly that message is presented.
    # From the "Static Ads That Scale" field manual — ads that sit in the middle
    # of this spectrum (too vague to be clear, too obvious to build curiosity) lose.
    style_copy_block = ""
    if style_key and style_key in STYLES:
        st = STYLES[style_key]
        style_copy_block = f"""
━━━ CREATIVE STYLE — {st['label']} ({st['subtitle']}) ━━━
{st['copy_instruction']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # ── Fallback emotional structure — only applies when NO angle is selected.
    # Each angle above now carries its own opening logic (Problem Agitate opens on
    # pain, Curiosity opens on a question, Social Proof opens on trust, etc.) — this
    # generic "feel the pain first" rule would otherwise contradict angles that are
    # deliberately NOT pain-first, so it only fires as a sane default with no angle.
    pain_first_block = ""
    if not angle_copy_block:
        pain_point = (p['narrative'] if (persona_key and persona_key in PERSONAS) else problem_ctx) or problem
        pain_first_block = f"""
EMOTIONAL STRUCTURE: h1/h2 must surface the reader's real problem first (not the resolved outcome) —
{brand_name} as the relief belongs in the CTA, not the opening line.
PROBLEM TO SURFACE: {pain_point}
"""

    prompt = f"""Write ad copy for {product_name} by {brand_name}.

━━━ PRODUCT KNOWLEDGE (use ONLY these facts — never invent) ━━━
DESCRIPTION: {product_txt or '(not provided)'}
BENEFITS for {problem}: {benefits_txt or '(not provided)'}
CERTIFICATIONS: {certs or 'Halal JAKIM, KKM Approved, GMP Certified'}
{f"LIVE PRODUCT FACTS: {product_facts}" if product_facts else ""}
{persona_copy_block}
{angle_copy_block}
{style_copy_block}
{pain_first_block}
━━━ YOUR TASK ━━━
FRAMEWORK: {framework_task}
USE CASE: {problem}

SYNTHESIS — read this before writing a single word:
{"Apply the ANGLE STRUCTURE above as the format. Then ask: what is THIS PERSONA's specific version of that angle? Use their exact situation, their language, their daily reality from PERSONA CONTEXT above. The copy should feel like it was written for one specific person, not a general customer." if angle_copy_block and persona_copy_block else ("Apply the ANGLE STRUCTURE above to this specific use case ({problem}). Every line must match the structural formula — not just the topic." if angle_copy_block else ("Write for the specific person described in PERSONA CONTEXT above. Use their words, their worry, their daily reality." if persona_copy_block else f"Write benefit-focused copy for {problem}."))}
Bullets come from BENEFITS above — pick the 3-4 most relevant to {problem} and to this persona's situation.

{f'''VARIATION REQUIRED: This is one of several variations for the same use case.
Write a FRESH, DISTINCT headline — different hook, structure, or emphasis than:
{chr(10).join(f"- {h}" for h in previous_headlines)}''' if vary_copy and previous_headlines else ""}

RULES:
1. Use ONLY product facts above — never invent claims or statistics
2. Correct Bahasa Malaysia — no invented or blended words
3. Bullets max 5 words each
4. No price, no RM amounts
5. {"h1/h2/cta must follow the ANGLE STRUCTURE formula exactly" if angle_copy_block else "h1 must surface the problem first — solution belongs in CTA"}
6. {"benefit_bullets" if not is_vs_them else "right_bullets"} must be REAL benefits from BENEFITS section above
7. This ad is ONLY about {problem} — ignore any other conditions mentioned in DESCRIPTION

Return JSON only:
{json_template}"""

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        # Strip markdown code fences if present
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",        raw.strip())
        copy_data = json.loads(raw)

        # ── User-provided fields always win (exact text, no changes) ──────────
        if copy_h1:  copy_data["h1"]  = copy_h1
        if copy_h2:  copy_data["h2"]  = copy_h2
        if copy_cta: copy_data["cta"] = copy_cta

        # ── Enforce minimum 3 benefits from Brand Setup ───────────────────────
        # Direct benefits (extracted from Brand Setup text) take priority over AI-generated ones.
        # This guarantees the ad always shows real product benefits the user wrote themselves.
        if direct_benefits:
            ai_bullets    = copy_data.get("right_bullets", [])
            # Start with direct benefits (user's exact words), pad with AI bullets if needed
            merged = list(direct_benefits)   # user's exact words from Brand Setup
            for b in ai_bullets:
                if len(merged) >= 5: break
                if b not in merged: merged.append(b)
            copy_data["right_bullets"] = merged[:5]
            print(f"[copy-gen] Benefits enforced from Brand Setup: {merged[:3]}")
        else:
            print("[copy-gen] WARNING: no benefits found in Brand Setup — using AI-generated bullets")

        # Remove price from CTA if it slipped in
        cta_val = copy_data.get("cta", "")
        copy_data["cta"] = re.sub(r'RM\s*\d+', '', cta_val).strip().rstrip("—- ")

        print(f"[copy-gen] Final — h1='{copy_data.get('h1','')}' | benefits={copy_data.get('right_bullets',[])[:2]}")
        return copy_data
    except Exception as e:
        print(f"[copy-gen] Error: {e}")
        # Even on error, return direct benefits if we have them
        return {
            "h1": copy_h1, "h2": copy_h2, "cta": copy_cta,
            "right_bullets": direct_benefits,
            "right_header": brand_name,
        }


VIBE_MODIFIERS = [
    "warm and energetic — golden hour lighting, rich warm tones, vibrant and inviting",
    "clean and professional — bright white light, crisp sharp edges, minimal and trustworthy",
    "bold and dramatic — deep contrast, strong shadows, high-impact and powerful",
    "soft and calming — pastel tones, gentle gradients, soothing and natural",
    "fresh and modern — cool blue-white tones, sleek, contemporary health brand feel",
    "rich and premium — deep luxurious tones, elegant, premium product presentation",
]


def build_prompt(layout_analysis, product_visual, problem, problem_ctx,
                 has_style_ref, has_product, brand,
                 copy_h1="", copy_h2="", copy_cta="", copy_data=None,
                 vibe_idx=0, extracted_style=None, skip_overlay=False, tweak_instructions="",
                 persona_desc="", persona_style_cues="", angle_visual="", style_visual=""):
    """
    Replication brief: keep exact winning-ad layout, swap in the correct product + copy.
    product_visual = vision analysis of the product photo (describes bottle precisely).
    """
    brand_block  = brand_to_prompt_block(brand)
    product_name = brand.get("_active_product_name") or brand.get("brand_name") or "this product"

    # ── Product identity section (most important — AI must render this exactly) ──
    if product_visual:
        product_section = f"""━━━ YOUR PRODUCT — MATCH IMAGE 1 EXACTLY ━━━
IMAGE 1 is a photo of your real product. No matter what form it takes in the final ad — standing
alone, poured into a glass, held in a hand — it must still clearly be THIS exact product: same
bottle/jar, same label, same colors, same cap. (See the HOW TO SHOW YOUR PRODUCT section below for
which of these forms to actually use.)

What IMAGE 1 actually looks like:
{product_visual}

Product name on label: {product_name}
RULES — violating any of these makes the generation wrong:
- Bottle/jar shape, silhouette, proportions: IDENTICAL to IMAGE 1 — trace it
- Label text, logo, colors, layout: IDENTICAL to IMAGE 1, character-for-character. Do NOT paraphrase,
  re-typeset, translate, or "clean up" the label — copy the exact wording and lettering you see
- Cap/lid shape, color, material: IDENTICAL to IMAGE 1
- Material/finish: IDENTICAL to IMAGE 1. If IMAGE 1's bottle is CLEAR/TRANSPARENT, keep it transparent
  with the same liquid colour and fill level — do NOT make it an opaque or coloured bottle
- If the logo/label is metallic, foil, or holographic in IMAGE 1, keep it metallic/foil — do not
  flatten it to plain print
- Never swap in a generic dark amber/brown "herbal tonic" bottle — the real product looks different
- Never invent a random unbranded container that isn't grounded in IMAGE 1 (or IMAGE 3 for liquid look)
- The product in IMAGE 2 belongs to a DIFFERENT brand — never copy IMAGE 2's bottle, label, or cap.
  Only IMAGE 1's product may appear in the output.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    else:
        product_section = f"""━━━ YOUR PRODUCT — REPRODUCE IMAGE 1 EXACTLY ━━━
IMAGE 1 is a photo of your real product. Reproduce it exactly, character-for-character:
- Bottle/jar shape, silhouette, cap: IDENTICAL to IMAGE 1
- Label text, logo, colours, layout: IDENTICAL to IMAGE 1 — copy the exact wording, do not paraphrase
- Finish: IDENTICAL to IMAGE 1. If IMAGE 1's bottle is CLEAR/TRANSPARENT, keep it transparent with the
  same liquid colour and fill level; if the logo is metallic/foil/holographic, keep it that way
- Do NOT substitute a generic bottle, glass, cup, or tin, and do NOT invent label text
- The product in IMAGE 2 is a DIFFERENT brand — never copy it; only IMAGE 1's product may appear
Product name on label: {product_name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # layout_section removed — main prompt uses CREATIVE DIRECTION block instead

    # ── User copy — these override everything. User fills H1/H2/CTA → used verbatim.
    # If blank → use AI-generated copy_data. Either way, Gemini gets exact strings.
    brand_name = brand.get("brand_name", "Serigama")

    # ── Simple per-slot variation — camera angle rotates across batch ───────────
    slot_num = vibe_idx + 1
    _ANGLES = [
        "straight-on, eye-level",
        "3/4 angle from the left",
        "3/4 angle from the right, slightly elevated",
        "slightly from above",
        "tight close-up on product",
        "wide shot, product smaller in frame",
    ]
    variation_note = _ANGLES[vibe_idx % len(_ANGLES)]

    # ── Copy block — Gemini renders this text directly onto the ad ───────────
    h1  = copy_h1  or (copy_data or {}).get("h1", "")
    h2  = copy_h2  or (copy_data or {}).get("h2", "")
    cta = copy_cta or (copy_data or {}).get("cta", "")
    copy_lines = []
    if h1:
        copy_lines.append(f'- HEADLINE (large, bold, most prominent): "{h1}"')
    if h2:
        copy_lines.append(f'- SUBHEADLINE (smaller, below headline): "{h2}"')
    if cta:
        copy_lines.append(f'- CTA (button or pill, high contrast): "{cta}"')

    # Bullets were being silently dropped here — the ad layout (cloned from the
    # winning-ad template) calls for numbered bullet slots with icons, but Gemini
    # was never told what those bullets should say, so it invented placeholder text
    # to fill the slots (garbled, sometimes bilingual gibberish). Cover both bullet
    # shapes: benefit_bullets (single-column frameworks) and left/right_bullets
    # (US VS THEM comparison layout).
    cd              = copy_data or {}
    benefit_bullets = [b for b in (cd.get("benefit_bullets") or []) if b]
    left_bullets    = [b for b in (cd.get("left_bullets") or []) if b]
    right_bullets   = [b for b in (cd.get("right_bullets") or []) if b]
    left_header     = cd.get("left_header") or ""
    right_header    = cd.get("right_header") or ""

    bullet_count = len(benefit_bullets) or (len(left_bullets) + len(right_bullets))

    if benefit_bullets:
        bullet_lines = "\n".join(f'  {i+1}. "{b}"' for i, b in enumerate(benefit_bullets))
        copy_lines.append(f'- BENEFIT BULLETS (one short line next to each icon/number, in order):\n{bullet_lines}')
    if left_bullets or right_bullets:
        if left_bullets:
            lb = "\n".join(f'  - "{b}"' for b in left_bullets)
            left_label = f'LEFT COLUMN header "{left_header}"' if left_header else "LEFT COLUMN"
            copy_lines.append(f'- {left_label}:\n{lb}')
        if right_bullets:
            rb = "\n".join(f'  - "{b}"' for b in right_bullets)
            right_label = f'RIGHT COLUMN header "{right_header}"' if right_header else "RIGHT COLUMN"
            copy_lines.append(f'- {right_label}:\n{rb}')

    copy_block = ""
    if copy_lines:
        typo          = (extracted_style or {}).get("typography", {}) or {}
        headline_font = typo.get("font_style") or "a strong display typeface"
        headline_wt   = typo.get("headline_weight") or "bold"
        headline_case = typo.get("headline_case") or ""
        case_note     = f", {headline_case}" if headline_case else ""
        copy_block = (
            "\n\nTEXT TO RENDER ON THE AD:\n"
            + "\n".join(copy_lines) +
            "\n\nTEXT STYLING:\n"
            f"- Headline: large, {headline_wt}, most prominent — use IMAGE 2's own font style: {headline_font}{case_note}\n"
            "- Subheadline: smaller, clean sans-serif, below headline\n"
            "- CTA: tappable button/pill with solid high-contrast background\n"
            "- Bullets: clean sans-serif, one line each next to icon/number\n"
            "- Follow IMAGE 2's text placement, zones, and hierarchy exactly\n"
            "- Strong contrast between text and background (shadow or scrim if needed)\n"
            f"- Render EXACTLY {bullet_count} bullet slot{'s' if bullet_count != 1 else ''} — "
            "delete any extra icon slots from IMAGE 2's layout so the count matches"
        )

    color_block        = ""
    layout_block       = ""
    human_block        = ""
    presentation_block = ""
    if extracted_style:
        colors = extracted_style.get("colors", {}) or {}
        mood   = extracted_style.get("mood", {}) or {}
        comp   = extracted_style.get("composition", {}) or {}
        lay    = extracted_style.get("layout", {}) or {}
        # ── Carry over the winning ad's full color palette, lighting, and mood.
        # Brand colors are NOT forced — the template IS the brand direction for this slot.
        # Brand colors may appear in font/badge/CTA only if they fit naturally.
        # NOTE: this all describes IMAGE 2 (the template) — do not relabel as IMAGE 1,
        # which is reserved for the product photo throughout this whole prompt.
        color_bits = []
        if colors.get("background_description"):
            color_bits.append(f"background: {colors['background_description']}")
        if colors.get("palette"):
            color_bits.append(f"color palette: copy IMAGE 2's actual colors — {colors['palette']}")
        if mood.get("lighting"):
            color_bits.append(f"lighting: {mood['lighting']}")
        if mood.get("tone"):
            color_bits.append(f"overall energy/tone: {mood['tone']}")
        if color_bits:
            color_block = ("\n\nCOLOR & LIGHTING (copy this from IMAGE 2 — do NOT switch to brand colors):\n- "
                            + "\n- ".join(color_bits) +
                            "\n\n⚠️ SCOPE LIMIT: the above applies ONLY to background, backdrop, panels, badges, "
                            "icons, and text colors. It NEVER applies to the product's own label/packaging color — "
                            "that comes ONLY from IMAGE 1 (see PRODUCT rules) and must stay exactly as IMAGE 1 shows "
                            "it, even if IMAGE 2's own original product or packaging was a completely different "
                            "color (e.g. IMAGE 2's background is green — the background becomes green, but IMAGE 1's "
                            "label does NOT become green just because it's near a green background).")

        layout_bits = []
        if lay.get("structure"):
            layout_bits.append(f"overall structure: {lay['structure']}")
        if lay.get("negative_space"):
            layout_bits.append(f"negative space: {lay['negative_space']}")
        if comp.get("style"):
            layout_bits.append(f"composition style: {comp['style']}")
        if comp.get("background_type"):
            layout_bits.append(f"background type: {comp['background_type']}")
        graphics = extracted_style.get("graphic_elements") or []
        if graphics:
            layout_bits.append("graphic elements to recreate: " + "; ".join(graphics))

        if layout_bits:
            layout_block = "\n\nLAYOUT — COPY THIS FROM IMAGE 2:\n- " + "\n- ".join(layout_bits)
            if graphics:
                layout_block += ("\n\nGraphic elements above (arrows, pointers, badges, callouts, icons): "
                                  "copy their POSITION/SHAPE/STYLE only.\n"
                                  "- Their meaning must match THIS ad's real benefits, not IMAGE 2's original topic\n"
                                  "- Flat icon/line-art style only — no extra photographic hand/bottle/person "
                                  "inside a callout; exactly ONE photographic hero shot in the whole ad\n"
                                  "- Any badge with a stat/claim (sold-count, timing, review score) needs a real "
                                  "matching fact from the brand knowledge below, or gets dropped — never invented\n"
                                  "- If IMAGE 2 has any certification/country badge (halal mark, quality seal, "
                                  "flag, etc.), do NOT copy IT — replace it with this brand's own certification "
                                  "badge per the HALAL LOGO RULE / CERTIFICATIONS above, or drop it entirely if "
                                  "this brand has no equivalent certification")

        # ── How the product itself should be PRESENTED — read straight from the
        # template so the product can take whatever form the winning ad actually
        # shows (bottle alone, poured into a glass, held in a hand), instead of a
        # rigid "always just the bottle standing there" rule that would block a
        # valid pour/glass shot the template itself calls for.
        presentation_bits = []
        if comp.get("product_style"):
            presentation_bits.append(f"IMAGE 2 shows its product like this: {comp['product_style']}")
        if lay.get("product_placement"):
            presentation_bits.append(f"IMAGE 2 places its product here: {lay['product_placement']}")
        if presentation_bits:
            presentation_block = (
                "\n\nHOW TO SHOW YOUR PRODUCT — match IMAGE 2's presentation:\n- "
                + "\n- ".join(presentation_bits) +
                "\n\nShow YOUR product the same way IMAGE 2 shows its product:\n"
                "- If IMAGE 2 shows the product poured into a glass or cup, pour YOUR product into a glass "
                "too — use IMAGE 3 for what the liquid actually looks like (color, clarity, thickness), "
                "never invent a random liquid color\n"
                "- If IMAGE 2 shows it held in a hand, hold YOUR product the same way\n"
                "- If IMAGE 2 just shows the bottle standing alone, do the same\n"
                "- Either way, your real bottle/jar from IMAGE 1 should still be visible somewhere in the "
                "shot, unless IMAGE 2's own shot is truly just a poured glass with no bottle in view"
            )

        # ── Hard constraints from the reference — these were extracted (cta_placement,
        # forbidden_elements) but previously only reached the post-generation QA check,
        # never the generation prompt itself, so Gemini had no way to know the reference
        # had no CTA or that certain elements (gradients, sunbursts, etc.) shouldn't appear.
        constraint_bits = []
        cta_placement = (lay.get("cta_placement") or "").strip().lower()
        if cta_placement in ("", "none", "n/a", "absent", "no cta"):
            constraint_bits.append(
                "NO CTA button or pill anywhere in the ad — IMAGE 2 has no CTA element. Adding one is a "
                "direct violation of this reference's layout."
            )
        bg_type = (comp.get("background_type") or "").lower()
        if "flat" in bg_type or "solid" in bg_type:
            constraint_bits.append(
                f"Background MUST be a flat solid color — NOT a gradient, NOT a sunburst, NOT textured. "
                f"IMAGE 2's background is: {comp.get('background_type')}."
            )
        for fe in (extracted_style.get("forbidden_elements") or []):
            if fe:
                constraint_bits.append(fe if fe.strip().upper().startswith("NO ") else f"NO {fe}")
        if constraint_bits:
            layout_block += (
                "\n\nHARD CONSTRAINTS FROM IMAGE 2 — VIOLATING ANY OF THESE MAKES THE GENERATION WRONG:\n- "
                + "\n- ".join(constraint_bits)
            )

        if comp.get("has_human"):
            human_block = ("\n\nHUMAN ELEMENT: IMAGE 2 features a person interacting with the product. "
                           "You MAY include a person in a similar role (e.g. holding/using the product), "
                           "but do not copy their identity, face, or outfit — generate a new person that "
                           "fits the same vibe.")
            # Default demographic anchor — only applies when no specific persona was
            # selected (personas already carry their own ethnicity/age specifics).
            # Without this, the model defaults to generic/Western stock-photo casting
            # instead of reflecting the brand's actual customer base.
            if not persona_desc:
                human_block += (
                    "\n\nDEFAULT CASTING (no specific persona selected): the person should generally reflect "
                    "this brand's core customer base — primarily Malay women (occasionally Chinese, ~10%), "
                    "age range roughly 25-70, with a clearly Asian/Malaysian appearance. Avoid generic "
                    "Western/Caucasian stock-photo casting unless the brand knowledge above specifically calls "
                    "for it. If the depicted person is a Malay woman, she should be WEARING A HIJAB/TUDUNG "
                    "(modest headscarf) by default, styled appropriately for the scene. If Chinese, no hijab "
                    "needed."
                )

    problem_block = ""
    if problem_ctx:
        problem_block = f"\n\nTARGET PROBLEM / ANGLE FOR THIS AD: {problem_ctx}\nUse this to inform mood, imagery cues, and the copy's emotional angle — the ad should feel relevant to someone facing this problem."

    # ── Creative Style — Clarity vs Curiosity Gap. Applies regardless of whether
    # a person is in the layout (unlike angle_visual, which lives inside the
    # persona casting block) since it governs the whole ad's execution style.
    style_block = ""
    if style_visual:
        style_block = f"\n\nCREATIVE STYLE FOR THIS AD: {style_visual}\nThis governs the overall execution — commit to this lane fully, don't blend obvious-ad and native/hidden styling."

    # Angle's visual/scene treatment must reach the image prompt whether or not a
    # persona is also selected — angle is documented as fully independent of
    # persona, so it can't be gated behind "only if a persona was picked."
    angle_visual_block = ""
    if angle_visual:
        angle_visual_block = f"""

SCENE/MOMENT FOR THIS ANGLE — vary the composition, not the person:
{angle_visual}
Pick this specific moment/framing instead of a default static portrait. The depicted person's age,
look, wardrobe, and relationship to others stays LOCKED to the persona description above — only the
scene, framing, and composition change to fit this angle."""

    persona_block = ""
    if persona_desc:
        persona_block = f"""

━━━ TARGET CUSTOMER PERSONA FOR THIS AD — APPLY ONE OF THE TWO PATHS BELOW ━━━
{persona_desc}

PATH A — if the ad layout (IMAGE 2) includes a person: cast and style them to match this persona exactly
(age, look, wardrobe, setting, emotional tone, body language/pose).
Show the person FULLY in frame — do not crop at the waist or leave half the frame as empty plain background.
Fill the space IMAGE 2 allocated to the person with the correctly cast persona from head to toe (or at least head to hip).
{angle_visual_block}

PATH B — if the ad layout (IMAGE 2) is a clean product shot with NO person: do NOT add a person.
Keep IMAGE 2's exact background, color palette, and lighting — do not shift colors to match the persona.
The persona only affects the emotional register of the copy, not the visual palette.

Either way, this only affects mood/casting/styling — it does NOT change the product, brand
identity, or the core layout structure being cloned from IMAGE 2.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    elif angle_visual:
        # No persona selected — the angle's scene/composition guidance still needs
        # to reach the prompt on its own.
        persona_block = f"""

━━━ SCENE/COMPOSITION FOR THIS AD'S ANGLE ━━━
{angle_visual}
Apply this to the scene, framing, and composition. This does not change the product, brand
identity, or the core layout structure being cloned from IMAGE 2.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    tweak_block = ""
    if tweak_instructions:
        tweak_block = f"""

━━━ USER REQUESTED EDIT — HIGHEST PRIORITY ━━━
The attached IMAGE 1 is the ad to fix/edit. Keep everything about it the same EXCEPT for the
following requested change. This instruction overrides any conflicting guidance above:
{tweak_instructions}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    if has_style_ref and has_product:
        return f"""Generate a 4:5 advertisement image.

{brand_block}{problem_block}{style_block}

CREATIVE DIRECTION — copy the whole look of IMAGE 2 (the winning ad template):
- BACKGROUND: same background as IMAGE 2 — its color, texture, gradient, or scene. Do NOT replace it with yellow, cream, or any brand color.
- LAYOUT & COMPOSITION: same structure as IMAGE 2 — zones, panels, where the text and product sit.
- GRAPHIC ELEMENTS: same badges, icons, dividers, CTA pill shape and position as IMAGE 2.
- COLORS: same color palette as IMAGE 2 for the background and graphic elements. Brand colors (from BRAND & PRODUCT KNOWLEDGE) only apply to the CTA button text and headline — not the background or overall palette.
- FONTS: same font style as IMAGE 2 (see TEXT STYLING below for specifics).
The only two things that change from IMAGE 2: the product (swap it for IMAGE 1's product — see below) and all the text (use the copy below).{
" UNLESS CREATIVE STYLE above calls for native/organic — then strip the polished-ad elements (badges, "
"CTA pill, commercial framing) the template would normally clone; style wins over exact cloning here."
if (style_visual and "native" in style_visual.lower()) else ""}{color_block}{layout_block}{presentation_block}

PRODUCT: use IMAGE 1 — your real product — exactly as it appears.
⚠️ IMAGE 1 IS THE SINGLE SOURCE OF TRUTH FOR THE PRODUCT:
- Container shape: same silhouette, proportions, neck/cap shape as IMAGE 1
- Label: same layout, text, colors, and logo as IMAGE 1 — never invent or simplify it
- Color of packaging: same as IMAGE 1 — never substitute a different color or finish, and never
  recolor the label/packaging to match IMAGE 2's background or IMAGE 2's own original product color
  (e.g. if IMAGE 2's scene is green, the SCENE becomes green — the product label stays IMAGE 1's
  real color regardless)
- See HOW TO SHOW YOUR PRODUCT above for whether it should stand alone, be poured into a glass,
  or be held — whichever form IMAGE 2 uses, but always still recognizably IMAGE 1's real product
{product_section}

CAMERA ANGLE VARIATION (subtle — keep IMAGE 2's overall framing): {variation_note}{human_block}{persona_block}{copy_block}

OUTPUT RULES:
- No watermarks or signatures
- No invented text other than the headline, subheadline, and CTA specified above
- Clean, professional Meta ad quality
- 4:5 aspect ratio{tweak_block}"""

    elif has_product:
        return f"""Create a 4:5 advertisement image.

{brand_block}{problem_block}{style_block}{persona_block}

PRODUCT: Feature only the product from the PRODUCT image. Do not change or replace it.
{product_section}
BACKGROUND: Clean soft gradient, professional health product style.
PRODUCT PLACEMENT: centered, large, clear hero shot.

Variation for this slot: {variation_note}{copy_block}

Rules:
- Product centered and clearly visible
- Clean, professional ad quality{tweak_block}"""

    else:
        return f"""Create a clean 4:5 advertisement background for {brand_name}.

{brand_block}{problem_block}{style_block}

Professional health product style, soft gradient.{copy_block}{tweak_block}"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(SKILLS_DIR, "ui.html")

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True})

# ── Brand kit endpoints ───────────────────────────────────────────────────────
@app.route("/api/brand-kit", methods=["GET"])
def get_brand_kit():
    return jsonify(load_brand())

@app.route("/api/brand-kit", methods=["POST"])
def save_brand_kit():
    """Save text fields. Files handled separately."""
    data = request.json or {}
    brand = load_brand()
    for key in ["brand_name","tagline","brand_voice","primary_color","secondary_color",
                "accent_color","price","product_url","key_claims","certifications",
                "extra_notes","knowledge","products","active_product_id"]:
        if key in data:
            brand[key] = data[key]
    save_brand(brand)
    return jsonify({"ok": True, "brand": brand})

@app.route("/api/brand-kit/upload-logo", methods=["POST"])
def upload_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400
    ext  = Path(safe_name(f.filename, "logo.png")).suffix
    dest = BRAND_DIR / f"logo{ext}"
    f.save(str(dest))
    brand = load_brand()
    brand["logo_path"] = str(dest)
    save_brand(brand)
    return jsonify({"ok": True, "logo_url": f"/api/brand-kit/asset/logo{ext}"})

@app.route("/api/brand-kit/upload-product", methods=["POST"])
def upload_brand_product():
    """Upload to global product_photos pool (legacy / fallback)."""
    saved = []
    for f in request.files.getlist("photos"):
        if not f.filename: continue
        name = f"product_{len(list(BRAND_DIR.glob('product_*')))}_{safe_name(f.filename)}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    brand = load_brand()
    brand["product_photos"] = brand.get("product_photos",[]) + [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "photos": saved})

@app.route("/api/brand-kit/upload-product-photo/<int:product_id>", methods=["POST"])
def upload_product_photo(product_id):
    """Upload photos scoped to a specific product in the products array."""
    brand    = load_brand()
    products = brand.get("products", [])
    prod     = next((p for p in products if p.get("id") == product_id), None)
    if prod is None:
        return jsonify({"error": "Product not found"}), 404
    saved = []
    for f in request.files.getlist("photos"):
        if not f.filename: continue
        name = f"prod{product_id}_{len(list(BRAND_DIR.glob(f'prod{product_id}_*')))}_{safe_name(f.filename)}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    prod.setdefault("photos", [])
    prod["photos"] += [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "photos": saved})

@app.route("/api/brand-kit/delete-product-photo", methods=["POST"])
def delete_product_photo():
    """Remove a photo from a specific product's photos list."""
    data       = request.json or {}
    product_id = int(data.get("product_id", 0))
    filepath   = data.get("path", "")
    brand      = load_brand()
    products   = brand.get("products", [])
    prod       = next((p for p in products if p.get("id") == product_id), None)
    if prod and isinstance(prod.get("photos"), list):
        prod["photos"] = [p for p in prod["photos"] if p != filepath]
    save_brand(brand)
    try:
        if filepath and Path(filepath).exists():
            Path(filepath).unlink()
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/brand-kit/upload-creative", methods=["POST"])
def upload_brand_creative():
    """Upload brand creatives / existing ads as style references."""
    saved = []
    for f in request.files.getlist("creatives"):
        if not f.filename: continue
        name = f"creative_{len(list(BRAND_DIR.glob('creative_*')))}_{safe_name(f.filename)}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    brand = load_brand()
    brand["creative_refs"] = brand.get("creative_refs",[]) + [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "creatives": saved})

@app.route("/api/brand-kit/delete-asset", methods=["POST"])
def delete_brand_asset():
    data     = request.json or {}
    filepath = data.get("path","")
    brand    = load_brand()
    # Remove from any list it appears in
    for key in ("product_photos","creative_refs"):
        if isinstance(brand.get(key), list):
            brand[key] = [p for p in brand[key] if p != filepath]
    if brand.get("logo_path") == filepath:
        brand["logo_path"] = ""
    save_brand(brand)
    try:
        if filepath and Path(filepath).exists():
            Path(filepath).unlink()
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/brand-kit/asset/<filename>")
def brand_asset(filename):
    return send_from_directory(BRAND_DIR, filename)

# ── Campaign upload + generate ────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    session_id  = str(uuid.uuid4())[:8]
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(exist_ok=True)
    ad_paths = []
    for f in request.files.getlist("ads"):
        if f.filename:
            dest = session_dir / f"ad_{len(ad_paths):02d}_{safe_name(f.filename)}"
            f.save(str(dest))
            ad_paths.append(str(dest))
    product_path = ""
    pf = request.files.get("product")
    if pf and pf.filename:
        dest = session_dir / f"product_{safe_name(pf.filename)}"
        pf.save(str(dest))
        product_path = str(dest)
    else:
        # User picked a specific existing brand-kit photo in the sidebar instead of
        # uploading a new one — only accept paths that actually live in the brand
        # kit folder (never trust an arbitrary client-supplied filesystem path).
        picked = request.form.get("product_photo_path", "")
        if picked:
            try:
                picked_resolved = Path(picked).resolve()
                if picked_resolved.is_relative_to(BRAND_DIR.resolve()) and picked_resolved.exists():
                    product_path = str(picked_resolved)
            except (OSError, ValueError):
                pass

    # Optional explicit texture reference — a photo showing the product's actual
    # contents (liquid/jelly/gel) when poured/exposed, since the primary identity
    # photo is often a sealed bottle that doesn't reveal this. Same brand-kit-only
    # path validation as product_photo_path above.
    texture_path = ""
    picked_texture = request.form.get("texture_photo_path", "")
    if picked_texture:
        try:
            texture_resolved = Path(picked_texture).resolve()
            if texture_resolved.is_relative_to(BRAND_DIR.resolve()) and texture_resolved.exists():
                texture_path = str(texture_resolved)
        except (OSError, ValueError):
            pass

    meta = {"session_id": session_id, "ad_paths": ad_paths,
            "product_path": product_path,
            "texture_path": texture_path,
            "problem": request.form.get("problem","Selepas Bersalin"),
            "count": int(request.form.get("count",6))}
    atomic_write_json(session_dir / "meta.json", meta, indent=None)
    return jsonify({"session_id": session_id, "ads_count": len(ad_paths),
                    "has_product": bool(product_path)})

@app.route("/api/image/<session_id>/<filename>")
def api_image(session_id, filename):
    # send_from_directory sanitises `filename` but NOT the directory it is given,
    # and session_id is user-supplied — reduce it to a bare name so it can only
    # ever address a direct child of sessions/.
    return send_from_directory(SESSIONS_DIR / Path(session_id).name, filename)

# ── Template library ──────────────────────────────────────────────────────────
def load_templates() -> list:
    if TEMPLATES_JSON.exists():
        with open(TEMPLATES_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_templates(tpls: list):
    atomic_write_json(TEMPLATES_JSON, tpls)

def next_tpl_id(tpls: list) -> int:
    return max((t["id"] for t in tpls), default=0) + 1

@app.route("/api/templates", methods=["GET"])
def get_templates():
    tpls = load_templates()
    # Attach URL for each
    for t in tpls:
        t["url"] = f"/api/templates/image/{Path(t['path']).name}"
    return jsonify(tpls)

@app.route("/api/templates/image/<filename>")
def template_image(filename):
    return send_from_directory(TEMPLATES_DIR, filename)

@app.route("/api/templates/upload", methods=["POST"])
def upload_templates():
    """Accept a PDF or image files, extract/save as numbered templates."""
    tpls = load_templates()
    added = []

    # ── PDF upload ──
    pdf_file = request.files.get("pdf")
    if pdf_file and pdf_file.filename:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return jsonify({"error": "PyMuPDF not installed. Run: pip install pymupdf"}), 500

        pdf_bytes = pdf_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        # Detect heading font (same logic as extract.py)
        from collections import defaultdict
        font_page_sets = defaultdict(set)
        font_sizes     = defaultdict(list)
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span["text"].strip()
                        if 0 < len(txt) < 60:
                            font_page_sets[span["font"]].add(page.number)
                            font_sizes[span["font"]].append(span["size"])

        heading_font = None
        if font_page_sets:
            candidates = {f: p for f, p in font_page_sets.items() if len(p) >= 2}
            pool = candidates if candidates else font_page_sets
            heading_font = max(pool, key=lambda f: (
                len(font_page_sets[f]),
                sum(font_sizes[f]) / len(font_sizes[f])
            ))

        avg_size = (sum(font_sizes[heading_font]) / len(font_sizes[heading_font])
                    if heading_font else 0)

        # Build page → section heading map
        page_section = {}
        current_section = "Winning Ad"
        for page_num, page in enumerate(doc):
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span["text"].strip()
                        is_heading = (
                            (heading_font and span["font"] == heading_font) or
                            span["size"] >= avg_size * 0.85
                        ) and 2 < len(txt) < 60
                        if is_heading:
                            current_section = txt
            page_section[page_num] = current_section

        # Extract images
        seen_xrefs = set()
        for page_num, page in enumerate(doc):
            section = page_section.get(page_num, "Winning Ad")
            for img_idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    base_img = doc.extract_image(xref)
                except Exception:
                    continue
                if len(base_img["image"]) < 8000:   # skip tiny icons
                    continue
                tid      = next_tpl_id(tpls + added)
                filename = f"tpl_{tid:04d}.png"
                dest     = TEMPLATES_DIR / filename
                with open(dest, "wb") as fp:
                    fp.write(base_img["image"])
                entry = {
                    "id":      tid,
                    "path":    str(dest),
                    "label":   f"Winning Ad #{tid}",
                    "section": section,
                    "source":  pdf_file.filename,
                    "page":    page_num,
                }
                added.append(entry)

        doc.close()

    # ── Image files upload ──
    for img_file in request.files.getlist("images"):
        if not img_file.filename:
            continue
        tid      = next_tpl_id(tpls + added)
        ext      = Path(safe_name(img_file.filename)).suffix or ".png"
        filename = f"tpl_{tid:04d}{ext}"
        dest     = TEMPLATES_DIR / filename
        img_file.save(str(dest))
        added.append({
            "id":      tid,
            "path":    str(dest),
            "label":   f"Winning Ad #{tid}",
            "section": "Uploaded",
            "source":  img_file.filename,
            "page":    0,
        })

    tpls.extend(added)
    save_templates(tpls)

    # ── STEP 2: Extract visual style from each newly added template ────────────
    # Run in background-friendly way — failures are non-fatal
    for t in added:
        if not t.get("extracted_style"):
            style = extract_winning_ad_style(t["path"])
            if style:
                t["extracted_style"] = style
                # Persist style into templates.json
                for saved in tpls:
                    if saved["id"] == t["id"]:
                        saved["extracted_style"] = style
                        break
    save_templates(tpls)

    for t in added:
        t["url"] = f"/api/templates/image/{Path(t['path']).name}"
    return jsonify({"ok": True, "added": len(added), "templates": added})


@app.route("/api/templates/upload-url", methods=["POST"])
def upload_template_from_url():
    """Fetch an image from a URL (dragged from browser) and save as a template."""
    image_url = (request.form.get("image_url") or "").strip()
    if not image_url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            img_bytes = resp.read()
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400

    # Guess extension from content-type
    ext = ".jpg"
    if "png" in content_type:   ext = ".png"
    elif "webp" in content_type: ext = ".webp"
    elif "gif" in content_type:  ext = ".gif"

    tpls = load_templates()
    tid  = next_tpl_id(tpls)
    dest = TEMPLATES_DIR / f"tpl_{tid:05d}{ext}"
    dest.write_bytes(img_bytes)

    entry = {
        "id":      tid,
        "path":    str(dest),
        "label":   f"Winning Ad #{tid}",
        "section": "Uploaded",
        "source":  image_url,
        "page":    0,
    }
    style = extract_winning_ad_style(str(dest))
    if style:
        entry["extracted_style"] = style

    tpls.append(entry)
    save_templates(tpls)
    entry["url"] = f"/api/templates/image/{dest.name}"
    return jsonify({"ok": True, "templates": [entry]})


@app.route("/api/templates/style/<int:tpl_id>", methods=["GET"])
def get_template_style(tpl_id):
    """Return the extracted style JSON for a specific template."""
    tpls  = load_templates()
    tpl   = next((t for t in tpls if t["id"] == tpl_id), None)
    if not tpl:
        return jsonify({"error": "Template not found"}), 404
    style = tpl.get("extracted_style")
    if not style:
        # Try extracting now if not done yet
        style = extract_winning_ad_style(tpl["path"])
        if style:
            tpl["extracted_style"] = style
            save_templates(tpls)
    return jsonify({"style": style, "tpl_id": tpl_id})

@app.route("/api/templates/rename", methods=["POST"])
def rename_template():
    data  = request.json or {}
    tid   = int(data.get("id", 0))
    label = data.get("label","").strip()
    tpls  = load_templates()
    for t in tpls:
        if t["id"] == tid:
            t["label"] = label
            break
    save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/api/templates/delete", methods=["POST"])
def delete_template():
    data = request.json or {}
    tid  = int(data.get("id", 0))
    tpls = load_templates()
    tpl  = next((t for t in tpls if t["id"] == tid), None)
    if tpl:
        try:
            Path(tpl["path"]).unlink(missing_ok=True)
        except Exception:
            pass
        tpls = [t for t in tpls if t["id"] != tid]
        save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/api/templates/delete-all", methods=["POST"])
def delete_all_templates():
    tpls = load_templates()
    for t in tpls:
        try: Path(t["path"]).unlink(missing_ok=True)
        except Exception: pass
    save_templates([])
    return jsonify({"ok": True})

def _gemini_pick_best_template(candidates: list, persona_label: str, angle_label: str) -> dict:
    """Send thumbnail images of candidate templates to Gemini and ask it to pick
    the one whose visual style best matches the given persona + angle.
    Falls back to the first candidate if the API call fails."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or len(candidates) <= 1:
        return candidates[0]
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        parts = []
        valid = []
        for i, t in enumerate(candidates):
            if os.path.exists(t["path"]):
                b64, mime = img_to_b64(t["path"])
                parts.append({"text": f"=== TEMPLATE {i+1} ==="})
                parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                valid.append((i, t))
        if not valid:
            return candidates[0]
        parts.append({"text": (
            f"You are choosing a winning ad template for an image generation task.\n\n"
            f"PERSONA: {persona_label}\n"
            f"PERSUASION ANGLE: {angle_label}\n\n"
            f"Look at the {len(valid)} templates above. Pick the one whose visual style, "
            f"layout, mood, and composition would best suit this persona and angle.\n"
            f"Consider: does the layout feel right for the emotional tone? "
            f"Is the energy (warm/bold/clean/aspirational) a good match?\n\n"
            f"Reply with ONLY a single number — the template number (1 to {len(valid)}) "
            f"that is the best match. No explanation."
        )})
        payload = json.dumps({"contents": [{"parts": parts}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        raw = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        picked_num = int(''.join(c for c in raw if c.isdigit())[:2]) - 1
        if 0 <= picked_num < len(valid):
            chosen = valid[picked_num][1]
            print(f"[tpl-pick] Gemini chose template {picked_num+1} → '{Path(chosen['path']).name}'")
            return chosen
    except Exception as e:
        print(f"[tpl-pick] Gemini pick failed ({e}), using first candidate")
    return candidates[0]


# ── Auto-template rotation ────────────────────────────────────────────────────
# Guarantees every render gets a DIFFERENT template design: it walks a persistent,
# category-diverse ordering of the whole library one step per image, so the entire
# library is used before any template repeats — instead of an independent random
# roll that can (and did) land the same design on back-to-back renders.
_ROTATION_LOCK = threading.Lock()

def _build_diverse_order(all_lib, seed=None):
    """Order the library so consecutive entries come from DIFFERENT section/framework
    categories (US vs Them → Testimonial → Product highlight → …) before repeating a
    category — so successive renders differ in concept, not just in file."""
    rng = random.Random(seed)
    by_section = {}
    for t in all_lib:
        by_section.setdefault(t.get("section") or "Uncategorized", []).append(t)
    sections = list(by_section.keys())
    rng.shuffle(sections)
    for sec in sections:
        rng.shuffle(by_section[sec])
    order, max_len = [], max(len(v) for v in by_section.values())
    for r in range(max_len):
        for sec in sections:
            if r < len(by_section[sec]):
                order.append(by_section[sec][r]["path"])
    return order

def next_auto_templates(all_lib, batch_nonce, count):
    """Return `count` distinct template paths for one batch, continuing a persistent
    rotation across batches. All slots of a batch (fired in parallel) share one
    batch_nonce, so the FIRST to arrive claims a base offset for the batch and the
    rest reuse it — guarded by an in-process lock so parallel slots never collide.
    Returns the batch's ordered list; caller indexes it by (idx-1)."""
    paths = [t["path"] for t in all_lib]
    sig = str(len(paths)) + "|" + "|".join(sorted(paths))
    with _ROTATION_LOCK:
        state = {}
        if ROTATION_JSON.exists():
            try:
                with open(ROTATION_JSON, encoding="utf-8") as _rf:
                    state = json.load(_rf)
            except Exception:
                state = {}
        order  = state.get("order") or []
        cursor = int(state.get("cursor", 0))
        batches = state.get("batches", {})
        # rebuild the ordering if the library changed or the order is stale/exhausted
        valid = set(paths)
        if (state.get("sig") != sig or not order
                or any(p not in valid for p in order)):
            order = _build_diverse_order(all_lib, seed=uuid.uuid4().int)
            cursor, batches = 0, {}
        # claim (or reuse) a base offset for this batch
        if batch_nonce in batches:
            base = int(batches[batch_nonce])
        else:
            if cursor >= len(order):   # finished a full pass — reshuffle for a fresh cycle
                last = order[-1] if order else None
                order = _build_diverse_order(all_lib, seed=uuid.uuid4().int)
                if last and len(order) > 1 and order[0] == last:
                    order.append(order.pop(0))   # avoid an immediate cross-cycle repeat
                cursor = 0
            base = cursor
            cursor += max(1, count)
            batches[batch_nonce] = base
            # keep only the most recent handful of batch claims
            if len(batches) > 24:
                for k in list(batches.keys())[:-24]:
                    batches.pop(k, None)
        state = {"sig": sig, "order": order, "cursor": cursor, "batches": batches}
        # atomic_write_json, not a bare open("w") — this file is in SNAPSHOT_PATHS
        # (it is real state), and a truncated write loses the whole rotation.
        try: atomic_write_json(ROTATION_JSON, state, indent=None)
        except Exception as e: print(f"[auto-tpl] rotation save failed: {e}")
    n = len(order)
    return [order[(base + i) % n] for i in range(max(1, count))]

# ── Updated generate-one: accept template_ids ─────────────────────────────────
@app.route("/api/generate-one", methods=["POST"])
def api_generate_one():
    data         = request.json or {}
    # Bare name only: session_id is used to build a filesystem path below.
    session_id   = Path(str(data.get("session_id", ""))).name
    if not session_id or not (SESSIONS_DIR / session_id / "meta.json").exists():
        return jsonify({"error": "Unknown session — please upload again"}), 400
    idx          = int(data.get("index", 1))
    quality      = data.get("quality", "draft")
    problem      = data.get("problem", "Selepas Bersalin")
    problem_ids  = data.get("problem_ids", [])    # list of use-case labels, e.g. ["Gastric / GERD Relief", ...]
    total        = int(data.get("count", 6))
    template_ids = data.get("template_ids", [])   # list of int template IDs
    persona_ids  = data.get("persona_ids", [])    # list of persona keys, e.g. ["knee","accident"]
    angle_ids    = data.get("angle_ids", [])      # list of angle keys, e.g. ["social_proof","curiosity"]
    style_ids    = data.get("style_ids", [])      # list of style keys, e.g. ["clarity","curiosity_gap"]
    campaign_id  = data.get("campaign_id", None)
    campaign_name= data.get("campaign_name", "")
    batch_nonce  = data.get("batch_nonce") or session_id

    # ── Multi use-case × multi persona × multi angle: cycle through every combination
    # across the batch, so each image slot gets a different (use case, persona, angle)
    # triple rather than every image looking/reading the same. Single-select still
    # works — combos length 1. A batch always costs exactly `total` images regardless
    # of how many combos exist — combos are cycled round-robin, never materialized in
    # full, so combinatorics only affects coverage, not cost. The combo order is
    # shuffled with a session-stable seed so small batches sample a diverse spread
    # instead of always front-loading the first few nested-loop combinations.
    _problems_for_combo = problem_ids if problem_ids else [problem]
    _personas_for_combo = persona_ids if persona_ids else [None]
    _angles_for_combo   = angle_ids if angle_ids else [None]
    _styles_for_combo   = style_ids if style_ids else [None]
    if persona_ids:
        # Each persona owns its own use case(s) — pair a persona ONLY with a problem
        # from its own use_cases, never with another persona's. Cross-multiplying use
        # case and persona as fully independent axes (the old behaviour) could pair
        # e.g. the C-Section persona with the Joint Pain use case whenever two personas
        # with different use cases were selected together, producing copy that talks
        # about the wrong health problem for the persona actually being depicted.
        _combos = []
        for per in _personas_for_combo:
            per_use_cases = (PERSONAS.get(per, {}) or {}).get("use_cases") or []
            per_problems  = [p for p in per_use_cases if p in _problems_for_combo] or per_use_cases or _problems_for_combo
            for p in per_problems:
                for ang in _angles_for_combo:
                    for sty in _styles_for_combo:
                        _combos.append((p, per, ang, sty))
    else:
        _combos = [(p, None, ang, sty) for p in _problems_for_combo
                                        for ang in _angles_for_combo
                                        for sty in _styles_for_combo]
    random.Random(batch_nonce).shuffle(_combos)
    problem, persona_key_for_idx, angle_key_for_idx, style_key_for_idx = _combos[(idx - 1) % len(_combos)]
    print(f"[combo] idx={idx} -> use_case='{problem}' persona={persona_key_for_idx} angle={angle_key_for_idx} style={style_key_for_idx}")
    # active_product_id comes as a JS integer — normalise to int for comparison
    _apid = data.get("active_product_id", None)
    active_product_id = int(_apid) if _apid is not None else None
    copy_h1      = (data.get("copy_h1")  or "").strip()
    copy_h2      = (data.get("copy_h2")  or "").strip()
    copy_cta     = (data.get("copy_cta") or "").strip()
    skip_overlay = bool(data.get("skip_overlay", False))
    vary_copy    = bool(data.get("vary_copy", False))   # if True, generate fresh copy per image instead of reusing cache

    session_dir = SESSIONS_DIR / session_id
    with open(session_dir / "meta.json") as fp:
        meta = json.load(fp)

    brand        = load_brand()

    # ── Merge active product knowledge — this is what feeds Gemini ──────────────
    # If no active_product_id sent, fall back to the saved active_product_id
    if active_product_id is None:
        active_product_id = brand.get("active_product_id")
        if active_product_id:
            active_product_id = int(active_product_id)

    if active_product_id is not None:
        products = brand.get("products", [])
        active_prod = next((p for p in products if p.get("id") == active_product_id), None)
        if active_prod:
            if active_prod.get("product_url"): brand["product_url"] = active_prod["product_url"]
            if active_prod.get("knowledge"):   brand["knowledge"]   = active_prod["knowledge"]
            brand["_active_product_name"] = active_prod.get("name", "")
            kb = active_prod.get("knowledge", {})
            filled = [k for k in ("product","audience","benefits","proof","objections","competitors")
                      if (kb.get(k) or "").strip()]
            print(f"[generate] Product: '{active_prod.get('name')}' | "
                  f"Knowledge sections: {filled} | idx={idx}")
        else:
            print(f"[generate] WARNING: active_product_id={active_product_id} not found in products list")
    else:
        print(f"[generate] WARNING: no active_product_id — no product knowledge will be used!")
    ad_paths     = meta.get("ad_paths", [])
    product_path = meta.get("product_path", "")

    # ── Resolve reference images ──
    # Priority: selected templates > session-uploaded ads > brand creative refs
    tpl_map  = {t["id"]: t for t in load_templates()}
    tpl_paths = [tpl_map[tid]["path"] for tid in template_ids
                 if tid in tpl_map and os.path.exists(tpl_map[tid]["path"])]

    brand_creatives     = [p for p in brand.get("creative_refs",[])  if os.path.exists(p)]
    brand_product_photos= [p for p in brand.get("product_photos",[]  ) if os.path.exists(p)]

    # Per-product photos take priority over global product_photos pool
    all_products = brand.get("products", [])
    active_prod   = None
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod and active_prod.get("photos"):
            candidate = [p for p in active_prod["photos"] if os.path.exists(p)]
            if candidate:
                brand_product_photos = candidate
            else:
                # Active product photos don't exist on disk — fall through to any product with photos
                active_prod = None

    # If still no photos, search ALL products for one that has photos
    if not brand_product_photos:
        for prod in all_products:
            candidate = [p for p in prod.get("photos", []) if os.path.exists(p)]
            if candidate:
                brand_product_photos = candidate
                print(f"[generate] Using photos from product '{prod.get('name')}' as fallback")
                break

    # Auto-mode: assign a template per slot from a batch-wide, CATEGORY-DIVERSE
    # roster — not an independent random roll per slot. The old version seeded
    # the shuffle per (persona, angle), so each image in a batch picked from its
    # own unrelated random ordering of the full library with zero coordination.
    # Since 55%+ of the template library sits in just two visually-similar
    # categories ("Winning Ad", "Product highlight"), independent rolls could —
    # and did — land 2-3 slots in a row in the same category, so a batch of
    # different persona/angle combos still looked like the same ad reskinned.
    # This roster instead spreads picks across every section/framework category
    # (US vs Them, Testimonial, Rage Bait, Product highlight, etc.) before ever
    # repeating one, so consecutive images are genuinely different ad concepts,
    # not just different files from the same visual family. Seeded by
    # batch_nonce only (same for the whole batch) so the roster is consistent
    # across all slots — persona/angle no longer influence which template a
    # slot gets, only which copy/scene content fills it.
    if not tpl_paths:
        all_lib = [t for t in load_templates() if os.path.exists(t["path"])]
        if all_lib:
            # Persistent rotation: distinct template per slot within the batch AND
            # continues across batches so designs don't repeat until the whole
            # library has been used. See next_auto_templates().
            _batch_tpls = next_auto_templates(all_lib, batch_nonce, total)
            _pick = _batch_tpls[(idx - 1) % len(_batch_tpls)]
            tpl_paths = [_pick]
            _sec = next((t.get("section") for t in all_lib if t["path"] == _pick), "")
            print(f"[auto-tpl] idx={idx}/{total} → '{Path(_pick).name}' "
                  f"(section={_sec}) [rotation]")

    # Style references: selected/auto templates first, then session uploads, then brand creatives
    style_refs = tpl_paths or ad_paths or brand_creatives
    # Cycle so each slot uses a different winning ad reference
    style_ref  = [style_refs[(idx-1) % len(style_refs)]] if style_refs else []

    # ── Look up the resolved template's own product presentation style (bottle
    # alone / poured into a glass / held in a hand), if it's already been
    # analyzed and cached — used below to auto-pick whichever of this product's
    # photos best matches, instead of always using the same static default.
    _tpl_product_style = ""
    if style_ref:
        _matched_tpl = next((t for t in load_templates() if t.get("path") == style_ref[0]), None)
        if _matched_tpl:
            _tpl_product_style = ((_matched_tpl.get("extracted_style") or {})
                                   .get("composition", {}) or {}).get("product_style", "")

    # Product ref priority: session upload/pick (this generation only) > a photo
    # auto-matched to the chosen template's presentation style > saved per-product
    # default (persists across sessions, set once via the 🧪/✓ picker) > first-
    # uploaded photo as a last-resort guess. File size is deliberately NOT used as
    # a signal — a big lifestyle/mockup photo (e.g. a glass and spoon with no
    # product in frame) can easily outweigh a small, clean, isolated product PNG,
    # which is exactly what caused Gemini to faithfully render a random glass
    # instead of the actual bottle — it was never given the right reference image.
    saved_default_photo = (active_prod or {}).get("default_photo_path", "")
    if saved_default_photo and not os.path.exists(saved_default_photo):
        saved_default_photo = ""
    if brand_product_photos:
        # PRIORITY (fixes product hallucination from a wrong reference):
        #   1. saved default photo — the user's deliberate "this is the product" pick
        #   2. the clearest identity shot (isolated cut-out preferred)
        #   3. first photo — last resort
        # The template-presentation auto-matcher is intentionally NOT allowed to
        # override 1 or 2. Matching the IMAGE 1 reference to the template's scene
        # (poured/held/lifestyle) was pulling in photos where the real bottle is
        # small or obscured, which is exactly what made Gemini render the wrong
        # vessel. Presentation is handled by the prompt + IMAGE 3, never by
        # swapping the identity reference.
        best_photo = saved_default_photo or _best_product_identity_photo(brand_product_photos)
    else:
        best_photo = ""
    product_ref = product_path or best_photo
    print(f"[generate] Using product photo: {Path(product_ref).name if product_ref else 'NONE'}")

    # ── Texture reference: shows the product's actual contents (liquid/jelly/gel)
    # when poured/exposed — a sealed bottle photo often can't show this, and Gemini
    # will otherwise default to inventing a generic (often wrong, e.g. white/milky)
    # liquid. Priority: this generation's explicit pick > saved per-product default
    # (persists across sessions) > second brand-kit photo as a last-resort guess.
    texture_path = meta.get("texture_path", "")
    saved_default_texture = (active_prod or {}).get("default_texture_path", "")
    if saved_default_texture and not os.path.exists(saved_default_texture):
        saved_default_texture = ""
    if texture_path and os.path.exists(texture_path):
        texture_ref_photo = texture_path
    elif saved_default_texture:
        texture_ref_photo = saved_default_texture
    else:
        _texture_candidates = [p for p in brand_product_photos if p != product_ref]
        texture_ref_photo = _texture_candidates[0] if _texture_candidates else None
    print(f"[generate] Using texture reference: {Path(texture_ref_photo).name if texture_ref_photo else 'NONE'}")

    # ── Vision-ground the ACTUAL product photo so Gemini has an explicit textual
    # description (label text, colors, container shape) to lock onto, not just the
    # raw reference image — this is what actually prevents product hallucination
    # (e.g. rendering a generic glass/vessel instead of the real branded jar).
    # Cached per session/product so a multi-image batch only pays for this once.
    product_visual = ""
    if product_ref:
        # Global, disk-backed cache with a last-good-description fallback (see
        # product_visual_for) — so a 429 on the vision call can no longer silently
        # drop grounding and spike product hallucination.
        product_visual = product_visual_for(product_ref)
        print(f"[generate] product_visual {'OK' if product_visual else 'MISSING'} "
              f"({len(product_visual)} chars)")

    # ── CRITICAL: product photo goes FIRST so the model weights it highest ──
    # Order: [product_photo, winning_ad_style_ref]
    # The model treats the first image as the primary subject
    print(f"[generate idx={idx}] product_ref={product_ref}  style_refs={style_ref}")
    if not product_ref:
        print("[generate] WARNING: no product photo found — check Brand Setup -> Assets")
    # Send BOTH product photo AND winning ad to Gemini
    # Order: [winning ad first = primary layout reference, product photo second]
    # Winning ad tells Gemini the visual design. Product photo tells it what to show.
    ref_images = []
    for sr in style_ref:
        if sr and os.path.exists(sr):
            ref_images.append(sr)        # winning ad first
    if product_ref and os.path.exists(product_ref):
        ref_images.append(product_ref)  # product photo second

    problem_ctx = PROBLEM_CONTEXT.get(problem, problem)

    # ── Detect framework from selected template's section label ───────────────────
    # This controls what copy structure and PIL layout to use
    detected_framework = "BENEFIT"  # safe default — single column, no VS
    if style_ref:
        tpls = load_templates()
        selected_tpl = next(
            (t for t in tpls if t.get("path") == style_ref[0]), None
        )
        if selected_tpl:
            section = (selected_tpl.get("section") or "").lower()
            if any(k in section for k in ["us vs them", "versus", "compare", "perbandingan"]):
                detected_framework = "US_VS_THEM"
            elif any(k in section for k in ["testimonial", "testimoni", "review"]):
                detected_framework = "TESTIMONIAL"
            elif any(k in section for k in ["rage", "problem", "hook", "masalah"]):
                detected_framework = "PROBLEM_HOOK"
            elif any(k in section for k in ["benefit", "faedah", "stat", "product detail"]):
                detected_framework = "BENEFIT"
            else:
                detected_framework = "BENEFIT"
            print(f"[framework] Template section='{section}' -> framework={detected_framework}")

    # ── SPEED: kick off style extraction in background thread immediately ─────────
    # Style extraction (~20s) and copy generation (~30s) are independent — run them
    # concurrently so total wait ≈ max(~30s) instead of sum (~50s).
    _style_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def _extract_style_for_ref():
        if not style_ref:
            return None
        tpls = load_templates()
        selected_tpl = next((t for t in tpls if t.get("path") == style_ref[0]), None)
        if selected_tpl:
            cached = selected_tpl.get("extracted_style")
            if cached:
                return cached   # instant — already cached
            # Skip on-demand extraction to keep generation fast.
            # The winning ad image itself is sent to Gemini directly as IMAGE 2.
            return None
        return None

    _style_future = _style_executor.submit(_extract_style_for_ref)

    # ── SPEED: copy generated ONCE per (use case) within a session, cached per-problem ──
    # When multiple use cases are selected, each one needs its own copy (different
    # headline/benefits angle) — but repeats of the same use case across the batch
    # reuse the cached copy for that use case. Saves ~10-20s per repeated image.
    # EXCEPTION: if vary_copy is on, skip the cache entirely — every image gets its
    # own freshly-generated copy (slower, but distinct headline/angle each time),
    # and previously-used headlines for this use case are fed back in so the model
    # writes something genuinely different rather than a near-duplicate.
    # Cache key includes persona + angle, not just use case — otherwise a same-use-case
    # slot with a different persona/angle would silently reuse the wrong cached copy
    # and the persona/angle selection would have no visible effect on repeated slots.
    _copy_key = f"{problem}||{persona_key_for_idx or ''}||{angle_key_for_idx or ''}||{style_key_for_idx or ''}"
    session_meta_path = session_dir / "meta.json"
    cached_copy_map = meta.get("cached_copy_by_problem", {})
    headline_history = meta.get("headline_history_by_problem", {})
    cached_copy = None if vary_copy else cached_copy_map.get(_copy_key)

    if cached_copy:
        # Reuse copy from a previous image in this batch that used the same use case
        copy_data = cached_copy
        # Still apply manual overrides per-image if user provided them
        if copy_h1:  copy_data["h1"]  = copy_h1
        if copy_h2:  copy_data["h2"]  = copy_h2
        if copy_cta: copy_data["cta"] = copy_cta
        print(f"[copy] Reusing cached copy for idx={idx} — skipping text model call")
    else:
        # First image in batch — fetch web facts + generate copy, then cache

        # ── ALWAYS fetch live product facts from web ──────────────────────────
        # Even when H1/H2/CTA are manually filled, web facts enrich the bullets
        product_url = brand.get("product_url", "") or ""
        if not product_url and active_product_id is not None:
            ap = next((p for p in brand.get("products", []) if p.get("id") == active_product_id), None)
            if ap: product_url = ap.get("product_url", "") or ""

        # Skip web fetch if Brand Setup knowledge base is already rich (saves ~24s)
        kb = brand.get("knowledge", {})
        active_kb = {}
        if active_product_id is not None:
            ap2 = next((p for p in brand.get("products", []) if p.get("id") == active_product_id), None)
            if ap2: active_kb = ap2.get("knowledge", {})
        combined_kb = {**kb, **active_kb}
        kb_text = " ".join(str(v) for v in combined_kb.values() if v)
        knowledge_is_rich = len(kb_text.strip()) > 300

        product_facts = ""
        if product_url and not knowledge_is_rich:
            print(f"[web-fetch] Knowledge sparse — fetching live product facts from {product_url}")
            product_facts = fetch_product_facts(product_url)
            if product_facts:
                print(f"[web-fetch] Got {len(product_facts)} chars of live product data")
            else:
                print("[web-fetch] Nothing fetched — using Brand Setup knowledge only")
        elif knowledge_is_rich:
            print(f"[web-fetch] Skipping — Brand Setup knowledge is rich ({len(kb_text)} chars)")
        else:
            print("[web-fetch] No product URL set — add one in Brand Setup -> Products")

        all_copy_manual = bool(copy_h1 and copy_h2 and copy_cta)

        prior_headlines = headline_history.get(_copy_key, []) if vary_copy else []

        if all_copy_manual:
            # H1/H2/CTA provided — use them exactly, but still generate bullets
            # from Brand Setup + web facts so benefits are always present
            print("[copy] Manual H1/H2/CTA — generating bullets from knowledge + web facts")
            framework_hint = detected_framework
            copy_data = generate_copy_from_knowledge(
                framework_hint, problem, problem_ctx, brand,
                copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                product_facts=product_facts,
                vary_copy=vary_copy, previous_headlines=prior_headlines,
                persona_key=persona_key_for_idx, angle_key=angle_key_for_idx, style_key=style_key_for_idx,
            )
        else:
            framework_hint = detected_framework
            print(f"[copy-gen] Generating copy from Brand Setup + web facts...")
            copy_data = generate_copy_from_knowledge(
                framework_hint, problem, problem_ctx, brand,
                copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                product_facts=product_facts,
                vary_copy=vary_copy, previous_headlines=prior_headlines,
                persona_key=persona_key_for_idx, angle_key=angle_key_for_idx, style_key=style_key_for_idx,
            )

        # Typo/spelling/fact verification pass — runs on every fresh generation
        # (manual or AI-written copy, draft or final) since typos are a correctness
        # issue, not a final-quality-only nicety. Cached-copy reuse above skips this
        # because it was already verified the first time that combo was generated.
        copy_data = verify_and_clean_copy(copy_data, product_facts)
        # Re-apply manual H1/H2/CTA verbatim — the verification pass must never
        # silently rewrite text the user typed themselves, only AI-generated parts.
        if copy_h1:  copy_data["h1"]  = copy_h1
        if copy_h2:  copy_data["h2"]  = copy_h2
        if copy_cta: copy_data["cta"] = copy_cta

        # Tag copy_data with framework so build_prompt can use it
        copy_data["_framework"] = detected_framework

        # ── Enforce correct copy structure based on detected framework ──────────
        # If NOT US_VS_THEM: clear left/right columns, move benefits to benefit_bullets
        if detected_framework != "US_VS_THEM":
            benefits = (copy_data.get("benefit_bullets") or
                        copy_data.get("right_bullets")   or [])
            copy_data["left_bullets"]   = []    # no comparison left column
            copy_data["right_bullets"]  = []    # no comparison right column
            copy_data["left_header"]    = ""
            copy_data["right_header"]   = ""
            copy_data["benefit_bullets"]= benefits
            print(f"[framework] {detected_framework} — using single-column benefits: {benefits[:2]}")

        # Merge this slot's result into meta.json under a lock, re-reading from disk
        # first. Every slot of the batch shares one session and reached this point
        # from a copy of meta loaded ~30s ago (before the Gemini calls), so writing
        # that stale copy back wholesale dropped whatever the other slots cached in
        # the meantime — costing a repeat copy-generation call (real money) and
        # losing headline history, which is what stops vary_copy repeating itself.
        # The bare open("w") was also non-atomic, so a kill mid-write truncated
        # meta.json — and board_repair depends on it to recover lost creatives.
        with _META_LOCK:
            disk_meta = {}
            try:
                with open(session_meta_path, encoding="utf-8") as fp:
                    disk_meta = json.load(fp)
            except Exception:
                disk_meta = meta
            if vary_copy:
                hist = disk_meta.get("headline_history_by_problem", {})
                hist.setdefault(_copy_key, []).append(copy_data.get("h1", ""))
                disk_meta["headline_history_by_problem"] = hist
            else:
                cmap = disk_meta.get("cached_copy_by_problem", {})
                cmap[_copy_key] = copy_data
                disk_meta["cached_copy_by_problem"] = cmap
            atomic_write_json(session_meta_path, disk_meta, indent=None)
            meta = disk_meta

    layout_analysis = ""

    # ── Step 3: Collect style result (ran in parallel with copy gen above) ────────
    extracted_style = None
    try:
        extracted_style = _style_future.result(timeout=30)
    except Exception as e:
        print(f"[StyleExtract] Failed or timed out: {e}")
    finally:
        _style_executor.shutdown(wait=False)

    # ── Persona description for this slot's chosen persona (from the combo above) ──
    # Gemini infers casting (ethnicity/wardrobe/hijab/setting) directly from the raw
    # persona narrative baked into persona["prompt"] — no per-use-case special-casing.
    persona_desc = ""
    persona_style_cues = ""
    if persona_key_for_idx:
        persona = PERSONAS.get(persona_key_for_idx)
        if persona:
            persona_desc = persona["prompt"]
            persona_style_cues = persona.get("style_cues", "")

    # ── Step 4: Build prompt with extracted style ────────────────────────────────
    prompt = build_prompt(layout_analysis, product_visual, problem, problem_ctx,
                          bool(style_refs), bool(product_ref), brand,
                          copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                          copy_data=copy_data,
                          vibe_idx=idx - 1,
                          extracted_style=extracted_style,
                          skip_overlay=skip_overlay,
                          persona_desc=persona_desc,
                          persona_style_cues=persona_style_cues,
                          angle_visual=ANGLES.get(angle_key_for_idx, {}).get("visual_treatment", "") if angle_key_for_idx else "",
                          style_visual=STYLES.get(style_key_for_idx, {}).get("visual_treatment", "") if style_key_for_idx else "")

    # Pass winning_ad and product separately so Gemini knows exactly what each image is
    winning_ad_img  = style_ref[0] if style_ref else None
    product_img_ref = product_ref if product_ref else None
    # IMAGE 3 (liquid/contents texture) is only meaningful when the ad actually shows
    # the product poured into a glass — the template's presentation tells us that.
    # Sending it on every render (bottle-alone, held-in-hand, etc.) just adds a third
    # full image for the model to ingest with no benefit, slowing the render. So only
    # attach it for poured/glass shots; skip it otherwise (the common case) for speed.
    _is_poured = _bucket_presentation(_tpl_product_style) == "poured_glass"
    texture_ref = texture_ref_photo if _is_poured else None
    if texture_ref_photo and not _is_poured:
        print("[generate] skipping IMAGE 3 texture ref — template is not a poured/glass shot")

    # Always use nano-banana-pro-preview, regardless of the requested `quality`
    # flag or whether a winning ad is attached — it handles multi-image style
    # transfer and text rendering far more reliably than flash-image, and is more
    # resistant to product hallucination. Product/layout fidelity wins every time.
    # Track which model ACTUALLY ran — cost must reflect this, not the requested
    # `quality` flag, since every generation now runs FINAL_MODEL regardless.
    actual_quality_used = "final"
    img_bytes, err = call_gemini_image(prompt, ref_images, FINAL_MODEL,
                                       winning_ad_path=winning_ad_img,
                                       product_image_path=product_img_ref,
                                       texture_ref_path=texture_ref)
    # If the first call failed on billing/quota, every other model will fail the
    # same way (shared account quota) — don't waste ~2s per slot churning through
    # fallbacks; surface the real reason immediately with a 402 (payment required).
    if err and err.startswith(QUOTA_ERR_PREFIX):
        print(f"[generate] quota/billing failure — skipping fallbacks: {err}")
        return jsonify({"error": err[len(QUOTA_ERR_PREFIX):], "quota": True}), 402
    if err:
        img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                           winning_ad_path=winning_ad_img,
                                           product_image_path=product_img_ref,
                                           texture_ref_path=texture_ref)
        actual_quality_used = "draft"
        if err and err.startswith(QUOTA_ERR_PREFIX):
            return jsonify({"error": err[len(QUOTA_ERR_PREFIX):], "quota": True}), 402
        if err:
            img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_FALL,
                                               winning_ad_path=winning_ad_img,
                                               product_image_path=product_img_ref,
                                               texture_ref_path=texture_ref)

    if err:
        code = 402 if err.startswith(QUOTA_ERR_PREFIX) else 500
        return jsonify({"error": err.replace(QUOTA_ERR_PREFIX, ""),
                        "quota": err.startswith(QUOTA_ERR_PREFIX)}), code

    out_name = f"img_{idx:02d}_{quality}.png"
    out_path = session_dir / out_name
    with open(out_path, "wb") as fp:
        fp.write(img_bytes)

    # ── Save clean Gemini visual (no text) as background reference ───────────────
    bg_name = out_name.replace(".png", "_bg.png")
    bg_path = session_dir / bg_name
    shutil.copy2(str(out_path), str(bg_path))

    has_copy = False
    if has_copy:
        text_path = str(out_path).replace(".png", "_t.png")
        ok = apply_text_overlay(str(out_path), copy_data, text_path,
                                img_idx=idx - 1,
                                brand_colors={
                                    "primary_color":   brand.get("primary_color",   "#FFFFFF"),
                                    "secondary_color": brand.get("secondary_color", "#FFFFFF"),
                                },
                                layout_template=(idx - 1) % 3,
                                extracted_style=extracted_style)
        if ok and os.path.exists(text_path):
            shutil.move(text_path, str(out_path))
            print(f"[pil] Text rendered onto image — zero duplicates guaranteed")
        else:
            print("[pil] Overlay skipped (no copy data or PIL error)")

    # ── Resolved persona/angle labels for this slot — persisted + returned so cards
    # can tag use case, persona, and angle distinctly (three separate axes now).
    _persona_label = PERSONAS.get(persona_key_for_idx, {}).get("label", "") if persona_key_for_idx else ""
    _angle_label   = ANGLES.get(angle_key_for_idx, {}).get("label", "") if angle_key_for_idx else ""
    _style_label   = STYLES.get(style_key_for_idx, {}).get("label", "") if style_key_for_idx else ""

    # Auto-save to Board. Every slot of a batch reaches here in parallel, so the
    # whole claim-id -> copy-image -> append -> save sequence has to be atomic as
    # a unit, not just per-write (see _BOARD_LOCK).
    prod_name = brand.get("_active_product_name", "")
    with _BOARD_LOCK:
        board     = load_board()
        bid       = max((b["id"] for b in board), default=0) + 1
        b_filename= f"board_{bid:05d}.png"
        b_dest    = BOARD_DIR / b_filename
        shutil.copy2(str(out_path), str(b_dest))
        board.insert(0, {
            "id":           bid,
            "filename":     b_filename,
            "label":        f"Ad {idx}" + (f" — {prod_name}" if prod_name else ""),
            "problem":      problem,
            "persona":      _persona_label,
            "angle":        _angle_label,
            "style":        _style_label,
            "quality":      quality,
            "session_id":   session_id,
            "campaign_id":  campaign_id,
            "campaign_name":campaign_name,
            "created_at":   __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "copy_data":    copy_data if isinstance(copy_data, dict) else {},
        })
        # Update campaign image count
        if campaign_id:
            camps = load_campaigns()
            for c in camps:
                if c["id"] == campaign_id:
                    c["image_count"] = c.get("image_count", 0) + 1
                    break
            save_campaigns(camps)
        save_board(board)
    snapshot_async("new-image")   # commit right away, don't wait for the timer

    # ── STEP 5: Match score — non-fatal, skip silently if it fails ────────────────
    match_result = None
    if extracted_style and quality == "draft":   # only score drafts to save cost
        match_result = score_generated_ad(extracted_style, str(out_path))

    this_cost_usd  = COSTS[actual_quality_used]
    total_spend_usd = add_spend(this_cost_usd)

    _cd      = copy_data if isinstance(copy_data, dict) else {}
    _h1      = _cd.get("h1", "").strip()
    _h2      = _cd.get("h2", "").strip()
    _cta     = _cd.get("cta", "").strip()
    _bullets = [b for b in (_cd.get("benefit_bullets") or _cd.get("right_bullets") or []) if b]
    # Derive META copy instantly from existing fields — no extra API call
    _hook    = _h2 or _h1
    _proof   = _bullets[0] if _bullets else ""
    _primary = f"{_hook} {_proof}." if _proof else _hook
    _headline    = _h1
    _description = _cta
    return jsonify({
        "image_url":    f"/api/image/{session_id}/{out_name}",
        "board_url":    f"/api/board/image/{b_filename}",
        "board_id":     bid,
        "filename":     out_name,
        "label":        f"Ad {idx}",
        "use_case":     problem,
        "persona":      _persona_label,
        "angle":        _angle_label,
        "style":        _style_label,
        "cost_usd":     this_cost_usd,
        "cost_myr":     round(this_cost_usd * MYR_RATE, 2),
        "total_spend_usd": round(total_spend_usd, 4),
        "total_spend_myr": round(total_spend_usd * MYR_RATE, 2),
        "match_score":  match_result.get("match_score")      if match_result else None,
        "mismatch":     match_result.get("biggest_mismatch") if match_result else None,
        "verdict":      match_result.get("verdict")          if match_result else None,
        "meta_primary_text": _primary,
        "meta_headline":     _headline,
        "meta_description":  _description,
    })

# ── Campaigns ────────────────────────────────────────────────────────────────
CAMPAIGNS_JSON = _TMP / "campaigns.json"

def load_campaigns() -> list:
    if CAMPAIGNS_JSON.exists():
        with open(CAMPAIGNS_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_campaigns(items: list):
    atomic_write_json(CAMPAIGNS_JSON, items)

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    return jsonify(load_campaigns())

@app.route("/api/campaigns/create", methods=["POST"])
def create_campaign():
    data  = request.json or {}
    name  = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    camps = load_campaigns()
    cid   = max((c["id"] for c in camps), default=0) + 1
    entry = {
        "id":         cid,
        "name":       name,
        "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "image_count": 0,
    }
    camps.insert(0, entry)
    save_campaigns(camps)
    return jsonify({"ok": True, "campaign": entry})

@app.route("/api/campaigns/rename", methods=["POST"])
def rename_campaign():
    data  = request.json or {}
    cid   = int(data.get("id", 0))
    name  = data.get("name", "").strip()
    camps = load_campaigns()
    for c in camps:
        if c["id"] == cid:
            c["name"] = name
            break
    save_campaigns(camps)
    return jsonify({"ok": True})

@app.route("/api/campaigns/delete", methods=["POST"])
def delete_campaign():
    data  = request.json or {}
    cid   = int(data.get("id", 0))
    camps = load_campaigns()
    camps = [c for c in camps if c["id"] != cid]
    save_campaigns(camps)
    return jsonify({"ok": True})

# ── Board — persistent creative storage ──────────────────────────────────────
BOARD_DIR  = _TMP / "board"
BOARD_JSON = BOARD_DIR  / "board.json"
BOARD_DIR.mkdir(exist_ok=True)

def load_board() -> list:
    if BOARD_JSON.exists():
        with open(BOARD_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_board(items: list):
    atomic_write_json(BOARD_JSON, items)

@app.route("/api/spend", methods=["GET"])
def get_spend():
    """Cumulative real $ spent so far, across all sessions. NOT a Gemini account
    balance — Gemini's API has no balance/credits endpoint; this is a local running
    total computed from the actual cost of every generation made through this app."""
    total = load_spend()
    return jsonify({"total_usd": round(total, 4), "total_myr": round(total * MYR_RATE, 2)})

@app.route("/api/personas", methods=["GET"])
def get_personas():
    return jsonify([{"id": k, **v} for k, v in PERSONAS.items()])

def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return slug or uuid.uuid4().hex[:8]

@app.route("/api/personas", methods=["POST"])
def save_persona():
    """Upsert a persona from the Personas & Angles tab. Only the human-editable
    fields are accepted — prompt/copy_voice are re-derived from narrative here so
    editing a persona never requires touching prompt-engineering text."""
    data = request.json or {}
    pid  = (data.get("id") or "").strip() or _slugify(data.get("label", ""))
    entry = {
        "label":     data.get("label", "").strip() or pid,
        "emoji":     data.get("emoji", "🙂"),
        "subtitle":  data.get("subtitle", ""),
        "narrative": data.get("narrative", "").strip(),
        "use_cases": data.get("use_cases", []),
        "style_cues": data.get("style_cues") or DEFAULT_PERSONA_STYLE_CUES,
        # Preserve the hidden flag across edits — editing a persona must not
        # silently un-hide it from the Campaign Brief picker.
        "hidden":    data.get("hidden", PERSONAS.get(pid, {}).get("hidden", False)),
    }
    PERSONAS[pid] = _hydrate_persona(entry)
    save_personas(PERSONAS)
    return jsonify({"ok": True, "id": pid, "persona": PERSONAS[pid]})

@app.route("/api/personas/hide", methods=["POST"])
def hide_persona():
    """Toggle (or set) a persona's hidden flag. Hidden personas stay in the
    management list but are removed from the Campaign Brief customer-persona
    picker, so the user can retire a persona without deleting it."""
    data = request.json or {}
    pid  = data.get("id", "")
    if pid not in PERSONAS:
        return jsonify({"error": "not found"}), 404
    PERSONAS[pid]["hidden"] = bool(data["hidden"]) if "hidden" in data \
                              else not PERSONAS[pid].get("hidden", False)
    save_personas(PERSONAS)
    return jsonify({"ok": True, "id": pid, "hidden": PERSONAS[pid]["hidden"]})

@app.route("/api/personas/delete", methods=["POST"])
def delete_persona():
    data = request.json or {}
    pid  = data.get("id", "")
    PERSONAS.pop(pid, None)
    save_personas(PERSONAS)
    return jsonify({"ok": True})

@app.route("/api/angles", methods=["GET"])
def get_angles():
    return jsonify([{"id": k, **v} for k, v in ANGLES.items()])

@app.route("/api/angles", methods=["POST"])
def save_angle():
    """Upsert an angle from the Personas & Angles tab."""
    data = request.json or {}
    aid  = (data.get("id") or "").strip() or _slugify(data.get("label", ""))
    ANGLES[aid] = {
        "label":            data.get("label", "").strip() or aid,
        "psychology":       data.get("psychology", ""),
        "copy_instruction": data.get("copy_instruction", ""),
        "visual_treatment": data.get("visual_treatment", ""),
    }
    save_angles(ANGLES)
    return jsonify({"ok": True, "id": aid, "angle": ANGLES[aid]})

@app.route("/api/angles/delete", methods=["POST"])
def delete_angle():
    data = request.json or {}
    aid  = data.get("id", "")
    ANGLES.pop(aid, None)
    save_angles(ANGLES)
    return jsonify({"ok": True})

@app.route("/api/styles", methods=["GET"])
def get_styles():
    return jsonify([{"id": k, **v} for k, v in STYLES.items()])

@app.route("/api/styles", methods=["POST"])
def save_style():
    """Upsert a creative style (Clarity / Curiosity Gap / custom) from the Personas & Angles tab."""
    data = request.json or {}
    sid  = (data.get("id") or "").strip() or _slugify(data.get("label", ""))
    STYLES[sid] = {
        "label":            data.get("label", "").strip() or sid,
        "subtitle":         data.get("subtitle", ""),
        "copy_instruction": data.get("copy_instruction", ""),
        "visual_treatment": data.get("visual_treatment", ""),
    }
    save_styles(STYLES)
    return jsonify({"ok": True, "id": sid, "style": STYLES[sid]})

@app.route("/api/styles/delete", methods=["POST"])
def delete_style():
    data = request.json or {}
    sid  = data.get("id", "")
    STYLES.pop(sid, None)
    save_styles(STYLES)
    return jsonify({"ok": True})

@app.route("/api/use-cases", methods=["GET"])
def get_use_cases():
    product_name = request.args.get("product_name", "")
    return jsonify(get_use_cases_for_product(product_name))

def board_sorted(board):
    """Newest first, by when the image was actually generated. Sorting here (not
    relying on insertion order) keeps the board correct even for entries added
    out of order — e.g. items recovered from sessions, which get appended."""
    return sorted(board,
                  key=lambda b: ((b.get("created_at") or ""), b.get("id", 0)),
                  reverse=True)

@app.route("/api/board", methods=["GET"])
def get_board():
    board = board_sorted(load_board())
    cid   = request.args.get("campaign_id")
    if cid:
        board = [b for b in board if str(b.get("campaign_id","")) == cid]
    return jsonify(board)

@app.route("/api/board/add", methods=["POST"])
def board_add():
    """Copy a generated image into the board folder and register it."""
    data      = request.json or {}
    src_path  = data.get("src_path", "")   # absolute path to session image
    label     = data.get("label", "Ad")
    problem   = data.get("problem", "")
    quality   = data.get("quality", "draft")
    session_id= data.get("session_id", "")

    # src_path is client-supplied. Only ever accept a file that already lives in
    # sessions/ or board/ — otherwise this endpoint copies ANY file on disk into
    # board/, where /api/board/image/ then serves it back out.
    safe_src = safe_path_under(src_path, SESSIONS_DIR, BOARD_DIR)
    if not safe_src or not safe_src.exists():
        return jsonify({"error": "Source file not found"}), 400

    with _BOARD_LOCK:
        board    = load_board()
        bid      = max((b["id"] for b in board), default=0) + 1
        filename = f"board_{bid:05d}.png"
        dest     = BOARD_DIR / filename
        shutil.copy2(str(safe_src), str(dest))

        entry = {
            "id":         bid,
            "filename":   filename,
            "label":      label,
            "problem":    problem,
            "quality":    quality,
            "session_id": session_id,
            "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }
        board.insert(0, entry)
        save_board(board)
    return jsonify({"ok": True, "entry": entry,
                    "url": f"/api/board/image/{filename}"})

@app.route("/api/board/delete", methods=["POST"])
def board_delete():
    data = request.json or {}
    ids  = set(int(i) for i in data.get("ids", []))
    with _BOARD_LOCK:
        board = load_board()
        to_del = [b for b in board if b["id"] in ids]
        for b in to_del:
            try: (BOARD_DIR / Path(b["filename"]).name).unlink(missing_ok=True)
            except Exception: pass
        board = [b for b in board if b["id"] not in ids]
        save_board(board)
    return jsonify({"ok": True, "deleted": len(to_del)})

@app.route("/api/board/image/<filename>")
def board_image(filename):
    return send_from_directory(BOARD_DIR, filename)

# ── Export to "Download Board" ────────────────────────────────────────────────
# A browser <a download> always lands in ~/Downloads and the page cannot change
# that, which is how exported ads ended up mixed in with everything else. The
# server has real disk access, so exporting server-side writes straight into a
# dated folder with a meaningful filename and skips the browser entirely.
EXPORT_DIR = SKILLS_DIR / "Download Board"

def _clean_part(text: str, limit: int = 40) -> str:
    """Filename-safe fragment: keep letters/digits, collapse the rest to '-'."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "")).strip("-")
    return s[:limit].strip("-")

def _real_ext(path: Path) -> str:
    """Actual image format, not whatever the name claims. board/ holds a mix —
    some board_NNNNN.png files really contain JPEG data — and handing an ad
    platform a .png that is secretly a JPEG invites a rejected upload."""
    try:
        from PIL import Image as _I
        with _I.open(path) as im:
            return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                    "GIF": ".gif"}.get(im.format, ".png")
    except Exception:
        return ".png"

def _export_name(item: dict, ext: str = ".png") -> str:
    """<product>_<use-case>_<quality>_b<id><ext> — the board id at the end keeps
    every name unique, so two exports can never silently overwrite each other."""
    label = item.get("label") or ""
    # labels look like "Ad 01 — Minyak Urut Aromatik Gamat 75ML"; keep the product half
    product = label.split("—")[-1] if "—" in label else label
    parts = [_clean_part(product) or "Ad",
             _clean_part(item.get("problem"), 32),
             _clean_part(item.get("quality"), 8) or "draft",
             f"b{int(item.get('id', 0)):05d}"]
    return "_".join(p for p in parts if p) + ext

@app.route("/api/board/export", methods=["POST"])
def board_export():
    """Copy selected board images into Download Board/<YYYY-MM-DD>/ with readable
    names. Pass {"ids": [...]} or {"all": true}. Re-exporting is a no-op for files
    already present, so pressing it twice never duplicates."""
    data = request.json or {}
    ids  = set(int(i) for i in data.get("ids", []))
    board = load_board()
    items = board if data.get("all") else [b for b in board if b.get("id") in ids]
    if not items:
        return jsonify({"error": "Nothing selected to export"}), 400

    written, skipped, missing = [], 0, 0
    for it in items:
        src = BOARD_DIR / Path(it.get("filename", "")).name
        if not src.exists():
            missing += 1
            continue
        day = (it.get("created_at") or "")[:10] or "undated"
        day_dir = EXPORT_DIR / day
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / _export_name(it, _real_ext(src))
        # same id -> same name, so an existing file is the same image already exported
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            skipped += 1
            continue
        shutil.copy2(str(src), str(dest))
        written.append(f"{day}/{dest.name}")

    return jsonify({"ok": True, "exported": len(written), "skipped": skipped,
                    "missing": missing, "folder": str(EXPORT_DIR),
                    "files": written[:20]})

@app.route("/api/board/open-export-folder", methods=["POST"])
def open_export_folder():
    """Reveal the export folder in Finder."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["open", str(EXPORT_DIR)], timeout=10)
        return jsonify({"ok": True, "folder": str(EXPORT_DIR)})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}",
                        "folder": str(EXPORT_DIR)}), 500

@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    """Manually force a data snapshot (same thing the timer does)."""
    ok, msg = snapshot_data("manual")
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/board/repair", methods=["POST"])
def board_repair():
    """Rebuild missing board entries from the generated images still in
    sessions/. Every generation is auto-added to the board, so any session image
    with no board entry means that entry was lost — this re-registers it.
    Pass {"dry_run": true} to only report what would be recovered."""
    import collections
    data     = request.json or {}
    dry_run  = bool(data.get("dry_run"))

    brand = load_brand()
    prods = {p.get("id"): p.get("name", "") for p in brand.get("products", [])}
    pname = prods.get(brand.get("active_product_id")) or brand.get("brand_name") or "Product"

    # Held across the whole repair: a generation finishing mid-repair would other-
    # wise claim an id this loop has already handed out, and one of the two images
    # would overwrite the other.
    with _BOARD_LOCK:
        board    = load_board()
        next_id  = (max([b["id"] for b in board]) + 1) if board else 1
        by_sess  = collections.Counter(b.get("session_id", "") for b in board)

        todo = []
        for sdir in sorted(SESSIONS_DIR.glob("*")):
            if not sdir.is_dir():
                continue
            imgs = sorted([p for p in sdir.glob("img_*.png") if not p.name.endswith("_bg.png")],
                          key=lambda p: p.name)
            gap = len(imgs) - by_sess.get(sdir.name, 0)
            if gap > 0:
                todo.extend(imgs[-gap:])
        todo.sort(key=lambda p: p.stat().st_mtime)

        if dry_run:
            return jsonify({"ok": True, "would_recover": len(todo),
                            "files": [f"{p.parent.name}/{p.name}" for p in todo[:50]]})

        recovered = 0
        for src in todo:
            meta = {}
            mp = src.parent / "meta.json"
            if mp.exists():
                try:
                    with open(mp, encoding="utf-8") as f: meta = json.load(f)
                except Exception:
                    meta = {}
            cached = meta.get("cached_copy_by_problem") or {}
            fname  = f"board_{next_id:05d}.png"
            shutil.copy2(str(src), str(BOARD_DIR / fname))
            board.append({
                "id": next_id, "filename": fname,
                "label": f"Ad {src.stem.split('_')[1]} — {pname}",
                "problem": meta.get("problem", ""),
                "persona": "", "angle": "", "style": "",
                "quality": "final" if "_final" in src.name else "draft",
                "session_id": src.parent.name,
                "campaign_id": "recovered", "campaign_name": "Recovered",
                "created_at": _dt.fromtimestamp(src.stat().st_mtime).isoformat(timespec="seconds"),
                "copy_data": next(iter(cached.values()), {}) if cached else {},
            })
            next_id += 1; recovered += 1

        if recovered:
            save_board(board)
        total = len(board)
    # Snapshot AFTER releasing the lock: it shells out to git over a ~550 MB repo,
    # and holding the board lock across that would stall every in-flight generation
    # for the whole commit.
    if recovered:
        snapshot_async("after-repair")
    return jsonify({"ok": True, "recovered": recovered, "board_total": total})

@app.route("/api/board/<int:board_id>/generate-meta", methods=["POST"])
def board_generate_meta(board_id):
    """Generate META copy (primary text, headline, description) for an existing board item
    using its saved copy_data. Returns the three fields and persists them to the board."""
    board = load_board()
    item  = next((b for b in board if b["id"] == board_id), None)
    if not item:
        return jsonify({"error": "Board item not found"}), 404

    cd      = item.get("copy_data") or {}
    h1      = cd.get("h1", "")
    h2      = cd.get("h2", "")
    cta     = cd.get("cta", "")
    bullets = cd.get("benefit_bullets") or cd.get("right_bullets") or []
    problem = item.get("problem", "")
    persona_label = item.get("persona", "")
    angle_label   = item.get("angle", "")
    brand   = load_brand()
    product_name = brand.get("_active_product_name") or brand.get("brand_name") or "produk ini"

    # Board items only ever stored the display label, not the underlying key —
    # look the full entry back up so we can use the persona's real narrative and
    # the angle's actual psychology/structure (the same ones that shaped the
    # on-image h1/h2/cta), instead of just the label string.
    persona_entry = next((v for v in PERSONAS.values() if v.get("label") == persona_label), None)
    angle_entry   = next((v for v in ANGLES.values()   if v.get("label") == angle_label),   None)
    persona_narrative = (persona_entry or {}).get("narrative", "")
    angle_psychology  = (angle_entry or {}).get("psychology", "")
    angle_instruction = (angle_entry or {}).get("copy_instruction", "")

    # Real brand facts so the hook/body can cite something concrete instead of
    # generic filler — same knowledge base that grounds the on-image copy.
    kb         = brand.get("knowledge", {})
    proof_txt  = (kb.get("proof")       or "").strip()[:400]
    objections = (kb.get("objections") or "").strip()[:400]
    certs      = ", ".join(brand.get("certifications", [])) or ""
    brand_voice= brand.get("brand_voice", "")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    bullets_str = "\n".join(f"- {b}" for b in bullets[:4]) if bullets else ""

    prompt = f"""You are a senior direct-response copywriter, writing the META (Facebook/Instagram) ad copy
that runs ALONGSIDE the visual below — not describing the visual, complementing it.

PRODUCT: {product_name}
USE CASE / PROBLEM: {problem}
BRAND VOICE: {brand_voice or 'warm, trustworthy, heritage'}

PERSONA — write in a voice that could only be for this exact person, not a generic customer:
{persona_narrative or persona_label or 'General audience'}

ANGLE — this is the SAME psychological strategy that already shaped the on-image copy below;
the primary text must extend it, not restate it or drift into a different strategy:
{angle_label or 'Benefit showcase'} ({angle_psychology or 'clear benefit'})
{angle_instruction or ''}

REAL PROOF POINTS (use verbatim if referenced — never invent a number, stat, or claim not listed here):
{proof_txt or '(none provided)'}
CERTIFICATIONS: {certs or 'none provided'}
COMMON OBJECTIONS + ANSWERS (defuse one if it fits naturally, don't force it):
{objections or '(none provided)'}

AD COPY ALREADY ON THE IMAGE (primary text must NOT repeat these lines — it runs above/around the
image, so its job is to add the persona's voice and proof the image can't show):
Headline (h1): {h1}
Subheadline (h2): {h2}
CTA: {cta}
Benefits shown:
{bullets_str}

━━━ HOW TO WRITE meta_primary_text ━━━
Structure: HOOK (must work standalone — this is all that shows before "See More" truncates it,
so it must be a complete, compelling thought on its own) + 1-3 short follow-up lines that build
the case, separated by line breaks like real native Facebook/Instagram copy (short punchy lines,
not one dense paragraph).

Pick ONE hook technique that fits the angle above, don't default to a generic opener:
- Question hook — ask the exact question this persona is silently asking themselves
- Stat/number hook — lead with a real, verbatim number from PROOF POINTS above
- Contrarian/pattern-interrupt — challenge a common assumption about this problem
- Direct callout — name the persona's exact situation ("Kalau awak pernah...")
- Mini-story opener — one line dropping the reader into the persona's specific moment

After the hook: 1-3 short lines that do real work — agitate the specific consequence of the
problem (not vague), tie in ONE real benefit or proof point, and defuse an objection if it fits.
End with a soft nudge toward the CTA already on the image — do not repeat the CTA text verbatim.

Length: hook alone should work in ~125 characters. Total primary text (hook + follow-up lines)
should run roughly 150-450 characters — long enough to actually build a case, never a single
thin sentence, never a dense wall of text. Bahasa Malaysia throughout, natural spoken tone (this
brand's actual customers talk this way — not stiff literal-translation copy).

━━━ HOW TO WRITE meta_headline ━━━
25-40 characters (aim for ≤30 so it displays in full on mobile). A punchy outcome, benefit, or
curiosity-driven claim — genuinely different from h1 above, not a rewording of it. Bahasa Malaysia.

━━━ HOW TO WRITE meta_description ━━━
~25 characters. One short proof point (a real stat/certification) or a secondary nudge — this
field only displays on a few placements, so it should add one small extra reason to believe,
not restate the headline. Bahasa Malaysia.

RULES:
1. Never invent a statistic, certification, or claim not present in PROOF POINTS/CERTIFICATIONS above
2. Never repeat h1, h2, or cta verbatim anywhere in the three fields
3. No clichés ("Jangan lepaskan peluang", "Dapatkan sekarang juga" as an opener, etc.) — write like
   a specific person talking to another specific person, not a generic ad

Return JSON only:
{{
  "meta_primary_text": "...",
  "meta_headline": "...",
  "meta_description": "..."
}}"""

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "", raw.strip())
        meta  = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"Generation failed: {e}"}), 500

    # Persist to board item. Re-read under the lock instead of writing back the
    # copy loaded before the (multi-second) Gemini call — generations that landed
    # in the meantime are in the fresh board and would be erased by the stale one.
    with _BOARD_LOCK:
        board = load_board()
        fresh = next((b for b in board if b["id"] == board_id), None)
        if fresh is None:
            return jsonify({"error": "Board item no longer exists"}), 404
        fresh.setdefault("copy_data", {}).update(meta)
        save_board(board)

    return jsonify({
        "meta_primary_text": meta.get("meta_primary_text", ""),
        "meta_headline":     meta.get("meta_headline", ""),
        "meta_description":  meta.get("meta_description", ""),
    })

@app.route("/api/board/variants/<int:board_id>")
def board_variants(board_id):
    """List previously generated tweak-variant files for a board item."""
    board = load_board()
    item  = next((b for b in board if b["id"] == board_id), None)
    if not item:
        return jsonify({"items": []})
    stem = Path(item["filename"]).stem
    files = sorted(BOARD_DIR.glob(f"{stem}_v*.png"))
    items = [{"filename": f.name, "board_url": f"/api/board/image/{f.name}"} for f in files]
    return jsonify({"items": items})

@app.route("/api/board/regenerate", methods=["POST"])
def board_regenerate():
    """Take an existing board image + a user 'tweak' instruction and ask Gemini
    to produce new variation(s) that fix/adjust it accordingly."""
    data     = request.json or {}
    board_id = int(data.get("id", 0))
    tweak    = (data.get("tweak") or "").strip()
    count    = max(1, min(int(data.get("count", 1)), 4))
    quality  = data.get("quality", "draft")

    if not tweak:
        return jsonify({"error": "Please describe what to change"}), 400

    board = load_board()
    item  = next((b for b in board if b["id"] == board_id), None)
    if not item:
        return jsonify({"error": "Board item not found"}), 404

    src_path = BOARD_DIR / item["filename"]
    if not src_path.exists():
        return jsonify({"error": "Source image not found"}), 404

    brand = load_brand()
    active_product_id = brand.get("active_product_id")
    if active_product_id is not None:
        active_product_id = int(active_product_id)
    all_products = brand.get("products", [])
    active_prod  = None
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod:
            if active_prod.get("product_url"): brand["product_url"] = active_prod["product_url"]
            if active_prod.get("knowledge"):   brand["knowledge"]   = active_prod["knowledge"]
            brand["_active_product_name"] = active_prod.get("name", "")

    # Resolve product photo (per-product first, then any product, then global pool)
    brand_product_photos = [p for p in brand.get("product_photos", []) if os.path.exists(p)]
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod and active_prod.get("photos"):
            cand = [p for p in active_prod["photos"] if os.path.exists(p)]
            if cand:
                brand_product_photos = cand
    if not brand_product_photos:
        for prod in all_products:
            cand = [p for p in prod.get("photos", []) if os.path.exists(p)]
            if cand:
                brand_product_photos = cand
                break
    # Same priority as api_generate_one(): saved per-product default first, then
    # first-uploaded photo as a last-resort guess (never largest-file — see there
    # for why that was actively wrong).
    _saved_photo   = (active_prod or {}).get("default_photo_path", "")
    _saved_texture = (active_prod or {}).get("default_texture_path", "")
    if _saved_photo and not os.path.exists(_saved_photo):
        _saved_photo = ""
    if _saved_texture and not os.path.exists(_saved_texture):
        _saved_texture = ""
    product_ref  = _saved_photo or (brand_product_photos[0] if brand_product_photos else "")
    if _saved_texture:
        texture_ref = _saved_texture
    else:
        _tex_candidates = [p for p in brand_product_photos if p != product_ref]
        texture_ref = _tex_candidates[0] if _tex_candidates else None

    problem     = item.get("problem", "Selepas Bersalin")
    problem_ctx = PROBLEM_CONTEXT.get(problem, problem)
    copy_data   = item.get("copy_data") or {}

    ref_images = [str(src_path)]
    if product_ref and os.path.exists(product_ref):
        ref_images.append(product_ref)

    brand_block = brand_to_prompt_block(brand)
    product_clause = ""
    if product_ref and os.path.exists(product_ref):
        product_clause = (
            "\n\nPRODUCT REFERENCE (IMAGE 2): If the product bottle/packaging is visible in the ad, "
            "it MUST match IMAGE 2 exactly — same label, colors, text, cap, and bottle shape. "
            "Do not invent or substitute a different product."
        )

    prompt = f"""The attached IMAGE 1 is a contact sheet showing several ad concepts arranged in a grid.
It is a REFERENCE ONLY for style, vibe, background, colors, product placement, and tone.

{brand_block}

━━━ YOUR TASK ━━━
Create ONE brand-new advertisement image (a single visual, NOT a grid, NOT a collage, NOT multiple
panels) that matches the same overall style/vibe/colors/background/product placement as the concepts
in IMAGE 1, but is a fresh variation per this instruction:
{tweak}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{product_clause}

OUTPUT RULES:
- Output is exactly ONE advertisement visual — a single scene, single headline, single CTA
- Do NOT reproduce IMAGE 1's grid/panel structure — that is reference material only, not the layout to copy
- 4:5 aspect ratio
- No watermarks or signatures
- Write any on-image text in Bahasa Malaysia, clean and legible"""

    # Always use nano-banana-pro-preview first, regardless of the requested
    # `quality` flag — see the matching comment in api_generate_one for why.
    results = []
    actual_qualities = []
    for _ in range(count):
        actual_quality = "final"
        img_bytes, err = call_gemini_image(prompt, ref_images, FINAL_MODEL,
                                            winning_ad_path=str(src_path),
                                            product_image_path=product_ref or None,
                                            texture_ref_path=texture_ref,
                                            is_tweak=True)
        if err:
            img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                                winning_ad_path=str(src_path),
                                                product_image_path=product_ref or None,
                                                texture_ref_path=texture_ref,
                                                is_tweak=True)
            actual_quality = "draft"
            if err:
                img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_FALL,
                                                    winning_ad_path=str(src_path),
                                                    product_image_path=product_ref or None,
                                                    texture_ref_path=texture_ref,
                                                    is_tweak=True)
        if err or not img_bytes:
            continue

        # Save as a variant file alongside the original — NOT added to the
        # main board grid, so the original board item stays unchanged.
        variant_idx = len(list(BOARD_DIR.glob(f"{src_path.stem}_v*.png"))) + 1
        v_filename  = f"{src_path.stem}_v{variant_idx}.png"
        with open(BOARD_DIR / v_filename, "wb") as fp:
            fp.write(img_bytes)
        # Tweak variants live ONLY here — there is no copy in sessions/ like there
        # is for normal generations — so snapshot immediately or an unlucky wipe
        # loses them permanently (which is exactly what happened on 21-22 Jul).
        snapshot_async("tweak-variant")

        results.append({"filename": v_filename,
                         "board_url": f"/api/board/image/{v_filename}",
                         "label": (item.get("label", "Ad") + " (edit)")})
        actual_qualities.append(actual_quality)

    if not results:
        return jsonify({"error": "Generation failed — please try again"}), 500
    # Cost reflects the model that ACTUALLY ran per image, not the requested flag.
    cost_usd = round(sum(COSTS[q] for q in actual_qualities), 2)
    total_spend_usd = add_spend(cost_usd)
    return jsonify({"ok": True, "items": results,
                     "cost_usd": cost_usd, "cost_myr": round(cost_usd * MYR_RATE, 2),
                     "total_spend_usd": round(total_spend_usd, 4),
                     "total_spend_myr": round(total_spend_usd * MYR_RATE, 2)})

@app.route("/api/board/delete-variant", methods=["POST"])
def board_delete_variant():
    """Delete a generated tweak-variant image file (not a real board entry)."""
    data     = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    path = BOARD_DIR / filename
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})

# ── Product management ────────────────────────────────────────────────────────
@app.route("/api/brand-kit/products", methods=["GET"])
def get_products():
    brand = load_brand()
    return jsonify(brand.get("products", []))

@app.route("/api/brand-kit/products/save", methods=["POST"])
def save_products():
    """Save full products list."""
    data = request.json or {}
    brand = load_brand()
    brand["products"] = data.get("products", [])
    brand["active_product_id"] = data.get("active_product_id", None)
    save_brand(brand)
    return jsonify({"ok": True})

@app.route("/api/brand-kit/products/<int:product_id>/default-photos", methods=["POST"])
def set_default_product_photos(product_id):
    """Persist which photo is the product-identity reference and which (if any) is
    the texture/contents reference for this product — so the picker choice made
    once in the sidebar becomes the default for every future generation, instead
    of resetting to an auto-guess every time the page reloads."""
    data  = request.json or {}
    brand = load_brand()
    products = brand.get("products", [])
    prod = next((p for p in products if p.get("id") == product_id), None)
    if prod is None:
        return jsonify({"error": "Product not found"}), 404

    def _validate(path):
        if not path:
            return ""
        try:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(BRAND_DIR.resolve()) and resolved.exists():
                return str(resolved)
        except (OSError, ValueError):
            pass
        return ""

    if "default_photo_path" in data:
        prod["default_photo_path"] = _validate(data.get("default_photo_path", ""))
    if "default_texture_path" in data:
        prod["default_texture_path"] = _validate(data.get("default_texture_path", ""))
    save_brand(brand)
    return jsonify({"ok": True, "product": prod})

def test_product_image():  # kept as debug utility (no route, called manually)
    """
    FIX 4: Test that the product image is being sent to Gemini correctly.
    Sends the product image and asks Gemini to describe what it sees.
    Returns Gemini's description — should say 'Jeli Gamat' if image is correct.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 400

    data        = request.json or {}
    product_id  = data.get("active_product_id")
    brand       = load_brand()

    # Find product photo
    product_photo = ""
    if product_id:
        products = brand.get("products", [])
        ap = next((p for p in products if p.get("id") == product_id), None)
        if ap:
            photos = [p for p in ap.get("photos", []) if os.path.exists(p)]
            if photos:
                product_photo = photos[0]

    if not product_photo:
        # Fall back to global product photos
        global_photos = [p for p in brand.get("product_photos", []) if os.path.exists(p)]
        if global_photos:
            product_photo = global_photos[0]

    if not product_photo:
        return jsonify({"error": "No product photo found. Upload one in Brand Setup → Assets."}), 400

    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(product_photo)
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": "What product is shown in this image? "
                     "Describe the label text, brand name, and product name exactly as you see them. "
                     "Be specific and literal."}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        description = next((p["text"] for p in parts if "text" in p), "No description")
        print(f"[ProductTest] Image: {Path(product_photo).name}")
        print(f"[ProductTest] Gemini says: {description[:200]}")
        return jsonify({
            "ok": True,
            "image_path": product_photo,
            "image_name": Path(product_photo).name,
            "gemini_description": description,
            "pass": any(k in description.lower() for k in ["jeli", "gamat", "serigama", "luxor"])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── .env persistence ──────────────────────────────────────────────────────────
ENV_PATH  = Path(__file__).parent / ".env"
_ENV_LOCK = threading.Lock()

def env_set(name: str, value):
    """Set (value given) or remove (value None) a key in .env AND in the live
    process env. Written via a temp file + os.replace for the same reason the JSON
    data files are: a crash or a second concurrent write during the old
    read-truncate-rewrite left .env holding neither API key."""
    with _ENV_LOCK:
        lines = []
        if ENV_PATH.exists():
            with open(ENV_PATH, encoding="utf-8") as f:
                lines = [l for l in f.readlines() if not l.startswith(f"{name}=")]
        if value is not None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{name}={value}\n")
        # with_suffix() on a dotfile produces ".env.env.tmp" (Path(".env").suffix is
        # empty), and that name is NOT matched by the ".env" line in .gitignore — a
        # crash mid-write would leave an untracked file holding both API keys, ready
        # to be swept into a commit by `git add -A`. Build the name explicitly.
        tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_PATH)
        os.chmod(ENV_PATH, 0o600)   # it holds API keys
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

@app.route("/api/set-key", methods=["POST"])
def set_api_key():
    key = (request.json or {}).get("key", "").strip()
    if not key:
        return jsonify({"error": "Empty key"}), 400
    # Persist to .env, not just os.environ — the server is launchd-managed and
    # restarts on its own, and an env-only key vanished on every restart, leaving
    # generation failing with "GEMINI_API_KEY not set" for no visible reason.
    env_set("GEMINI_API_KEY", key)
    return jsonify({"ok": True, "hint": key[:8] + "…"})

@app.route("/api/key-status")
def key_status():
    key = os.environ.get("GEMINI_API_KEY", "")
    return jsonify({"set": bool(key), "hint": (key[:8] + "…") if key else ""})

# ── Canva Connect API (Personal Access Token) ───────────────────────────────────
CANVA_API = "https://api.canva.com/rest/v1"

def _get_canva_token() -> str:
    """Return stored Canva PAT — checks env var first, then .env file."""
    token = os.environ.get("CANVA_API_TOKEN", "").strip()
    if not token:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("CANVA_API_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        os.environ["CANVA_API_TOKEN"] = token
                        break
    return token

@app.route("/api/canva-token", methods=["POST"])
def api_set_canva_token():
    """Save a Canva Connect API Personal Access Token to .env and env var."""
    token = (request.json or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "Token is empty"}), 400
    env_set("CANVA_API_TOKEN", token)
    return jsonify({"ok": True, "hint": token[:10] + "…"})

@app.route("/api/canva-token", methods=["DELETE"])
def api_delete_canva_token():
    """Remove the stored Canva PAT (disconnect)."""
    env_set("CANVA_API_TOKEN", None)
    return jsonify({"ok": True})

@app.route("/api/canva-token-status")
def api_canva_token_status():
    """Check if a Canva PAT is stored and currently valid."""
    token = _get_canva_token()
    if not token:
        return jsonify({"connected": False})
    try:
        req = urllib.request.Request(
            f"{CANVA_API}/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        display = (data.get("team_user", {}).get("user_id")
                   or data.get("display_name")
                   or token[:8] + "…")
        return jsonify({"connected": True, "display": display, "hint": token[:10] + "…"})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"connected": False, "error": "Token expired or invalid"})
        return jsonify({"connected": True, "hint": token[:10] + "…"})
    except Exception:
        return jsonify({"connected": True, "hint": token[:10] + "…"})

@app.route("/api/canva-queue", methods=["POST"])
def api_canva_queue():
    """Upload a generated image to the connected Canva account's asset library."""
    import base64 as _b64, time as _time

    data       = request.json or {}
    session_id = data.get("session_id", "").strip()
    filename   = data.get("filename",   "").strip()
    label      = (data.get("label", filename) or "Serigama Ad").strip()[:50]

    if not filename:
        return jsonify({"error": "Missing filename"}), 400

    # session_id and filename come straight from the request body and the file
    # they name gets UPLOADED to Canva. Without containment, {"filename": "../.env"}
    # ships the Gemini and Canva keys off the machine. Strip both to bare names and
    # confirm the result really is inside sessions/ or board/.
    session_id = Path(session_id).name
    filename   = Path(filename).name
    img_path   = safe_path_under(SESSIONS_DIR / session_id / filename,
                                 SESSIONS_DIR) if session_id else None
    if not img_path or not img_path.exists():
        img_path = safe_path_under(BOARD_DIR / filename, BOARD_DIR)
        if not img_path or not img_path.exists():
            return jsonify({"error": f"Image not found: {filename}"}), 404

    token = _get_canva_token()
    if not token:
        return jsonify({
            "error": "Canva not connected. Go to Brand Setup → Brand tab → paste your Canva Connect API token."
        }), 400

    with open(img_path, "rb") as f:
        img_bytes = f.read()

    name_b64 = _b64.b64encode(label.encode()).decode()
    metadata = json.dumps({"name_base64": name_b64})
    req = urllib.request.Request(
        f"{CANVA_API}/asset-uploads",
        data=img_bytes,
        method="POST",
        headers={
            "Authorization":         f"Bearer {token}",
            "Content-Type":          "application/octet-stream",
            "Asset-Upload-Metadata": metadata,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            job_data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            return jsonify({"error": "Canva token expired or invalid. Reconnect in Brand Setup."}), 401
        return jsonify({"error": f"Canva upload failed ({e.code}): {body[:300]}"}), 500
    except Exception as exc:
        return jsonify({"error": f"Upload failed: {exc}"}), 500

    job    = job_data.get("job", job_data)
    job_id = job.get("id", "")
    status = job.get("status", "")

    # Poll briefly if still in progress
    deadline = _time.time() + 10
    while status not in ("success", "failed") and job_id and _time.time() < deadline:
        try:
            r = urllib.request.Request(f"{CANVA_API}/asset-uploads/{job_id}",
                                        headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(r, timeout=8) as resp:
                job = json.loads(resp.read()).get("job", job)
            status = job.get("status", status)
            if status in ("success", "failed"):
                break
        except Exception:
            break
        _time.sleep(1.5)

    if status == "failed":
        return jsonify({"error": f"Canva upload failed: {job.get('error', {})}"}), 500

    return jsonify({
        "ok": True, "job_id": job_id, "label": label,
        "message": f"✅ '{label}' is now in your Canva library!",
    })

if __name__ == "__main__":
    import webbrowser, threading

    # ── Load API key from .env file on startup (survives server restarts) ──────
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file, encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if not os.environ.get(k.strip()):  # don't override if already set
                        os.environ[k.strip()] = v.strip()
        print("[startup] Loaded API keys from .env")

    if not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: GEMINI_API_KEY not set")

    # Snapshot on boot (captures anything created while the server was down),
    # then keep snapshotting on a timer so data is never left uncommitted.
    _ok, _msg = snapshot_data("startup")
    print(f"[snapshot] startup: {_msg}")
    start_snapshot_thread()

    print(f"\n  static-remix UI  ->  http://localhost:{PORT}\n")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    # Bind to loopback only — this is a local single-user app with NO authentication
    # on any route (including endpoints that spend real Gemini money and delete data).
    # Binding 0.0.0.0 exposed all of that to anyone on the same WiFi/LAN. Override
    # with HOST=0.0.0.0 only if you deliberately need LAN access.
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=PORT, debug=False)
