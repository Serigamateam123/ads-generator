"""
Plug product knowledge from Google Drive document into brand_kit.json.
Content sourced directly from:
https://docs.google.com/document/d/1A4Rka2Jtik88NatGnyXbDbf8Qzcvlm4nooYq53d--tI
"""
import json

BRAND_PATH = r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\brand_kit\brand_kit.json'

# ── Knowledge extracted verbatim from the Google Doc ─────────────────────────

JELI_GAMAT_PRODUCT = """Jeli Gamat adalah suplemen kesihatan semula jadi berasaskan ekstrak gamat (sea cucumber). Tersedia dalam dua saiz: 550ML dan 350ML. Dikilang secara GMP-certified, diluluskan oleh KKM, dan bersijil Halal JAKIM. Mengandungi glukosamin semula jadi, kolagen, dan sebatian anti-radang yang membantu pemulihan badan secara menyeluruh."""

JELI_GAMAT_BENEFITS = """LUKA PEMBEDAHAN (Post-Op / Surgical Wounds):
- Mempercepatkan proses penyembuhan luka
- Mengurangkan keradangan selepas pembedahan
- Meningkatkan daya ketahanan badan

SAKIT SENDI (Joint Pain):
- Mengurangkan sakit akibat osteoarthritis
- Merangsang pembentukan dan pemulihan tulang rawan
- Tinggi glucosamine untuk kesihatan tulang rawan
- Kurangkan bengkak dan kesakitan pada sendi

GASTRIK (Gastric):
- Sokong proses pemulihan tisu yang mengalami keradangan atau iritasi
- Sifat anti-radang yang membantu mengurangkan keradangan dalam badan
- Bantu mengekalkan kesihatan lapisan saluran pencernaan dan perut

BERSALIN (Post-Partum):
- Mempercepatkan pemulihan selepas bersalin
- Mengurangkan keradangan dan luka dalaman
- Meningkatkan tenaga ibu selepas bersalin
- Selamat untuk ibu menyusu

KEMALANGAN / PATAH TULANG (Accident):
- Mempercepatkan proses penyembuhan luka luar dan dalam
- Merangsang pembentukan tulang rawan
- Mengurangkan keradangan pada kawasan yang cedera

GOUT:
- Mengurangkan keradangan akibat gout
- Membantu pengurangan asid urik dalam badan"""

JELI_GAMAT_AUDIENCE = """Wanita Melayu berumur 28-55 tahun, ibu-ibu dan penjaga keluarga. Mereka mempercayai cadangan rakan dan keluarga. Aktif di TikTok dan Facebook. Prihatin tentang halal dan keselamatan produk. Pembeli yang teliti — akan bertanya tentang sijil dan kelulusan sebelum membeli. Pengguna sekunder: anak-anak dewasa yang membeli untuk ibu bapa yang mengalami sakit sendi atau selepas pembedahan."""

JELI_GAMAT_PROOF = """STATISTIK:
- Lebih 20 tahun di pasaran
- Lebih 10 juta botol terjual
- Dipercayai oleh pelanggan di seluruh Malaysia

SIJIL DAN KELULUSAN:
- Halal JAKIM certified
- KKM Approved
- GMP Certified Facility

TESTIMONI PELANGGAN:
- "Selepas minum Jeli Gamat, luka pembedahan saya sembuh lebih cepat dari jangkaan doktor." — Pelanggan dari Johor
- "Lutut saya dah tak sakit macam dulu. Alhamdulillah." — En. Hamid, 58 tahun
- "Saya bagi mak saya minum lepas operation. Doktor terkejut dia recover cepat." — Aisyah, KL"""

JELI_GAMAT_OBJECTIONS = """BANTAHAN: "Mahal sangat."
JAWAPAN: 550ML tahan 55 hari pada dos 10ml sehari. Itu hanya RM1.60 sehari — lebih murah dari teh tarik.

BANTAHAN: "Tak tahu halal ke tidak."
JAWAPAN: Bersijil Halal JAKIM. Nombor sijil tertera pada setiap botol.

BANTAHAN: "Supplement je, mana tahu berkesan?"
JAWAPAN: Diluluskan KKM, bukan sekadar supplement. Kilang bersijil GMP. Lebih 20 tahun keputusan terbukti.

BANTAHAN: "Dah cuba banyak ubat, semua tak jalan."
JAWAPAN: Gamat adalah bahan semula jadi yang bioavailable — badan menyerapnya terus. Bukan kimia sintetik."""

JELI_GAMAT_COMPETITORS = """VS JENAMA LAIN (Generic Supplements):
- Kebanyakan pesaing guna botol 350ML. Serigama Luxor 550ML — 50% lebih banyak nilai.
- Pesaing tidak nyatakan kandungan gamat dengan jelas.
- Serigama nyatakan kandungan gamat secara telus pada label.
- Pesaing kerap tiada kelulusan Halal JAKIM atau KKM.
- Serigama: Halal JAKIM + KKM Approved + GMP Certified.

VS JENAMA GAMAT LAIN:
- Serigama menggunakan gamat liar berkualiti tinggi.
- Rekod 20 tahun berbanding jenama baharu tanpa sejarah.
- Kilang bersijil GMP — kebanyakan jenama kecil tidak."""

# ── Gel Gamat knowledge ───────────────────────────────────────────────────────

GEL_GAMAT_PRODUCT = """Gel Gamat Serigama adalah gel topikal berasaskan ekstrak gamat untuk rawatan kulit. Diformulasikan untuk pemulihan kulit semula jadi, rawatan luka bakar, dan penjagaan kulit."""

GEL_GAMAT_BENEFITS = """LUKA BAKAR (Burns):
- Rawat luka bakar akibat api, air panas, atau minyak panas
- Mempercepatkan pemulihan kulit semula jadi
- Mengurangkan kesakitan dan keradangan

KULIT (Skin Care):
- Mengurangkan parut dan menghaluskan kulit muka
- Bermanfaat untuk ulser mulut
- Sokong pemulihan kulit secara semula jadi"""

# ── Minyak Urut knowledge ─────────────────────────────────────────────────────

MINYAK_URUT_PRODUCT = """Minyak Urut Serigama adalah minyak urutan semula jadi untuk melegakan keletihan, ketegangan otot, dan kesakitan badan. Sesuai untuk semua peringkat umur termasuk bayi."""

MINYAK_URUT_BENEFITS = """KELETIHAN & SAKIT OTOT (Fatigue & Muscle):
- Melegakan keletihan dan ketegangan otot
- Mengurangkan sakit badan dan sengal-sengal
- Memudahkan proses urutan

BAYI (Infant):
- Mengurangkan kembung perut pada bayi
- Melegakan sakit perut bayi

SAKIT AM (General Pain):
- Melegakan sakit perut
- Membantu peredaran darah"""

# ── Load and update brand_kit.json ───────────────────────────────────────────

with open(BRAND_PATH, encoding='utf-8') as f:
    brand = json.load(f)

# Map product IDs to knowledge
KNOWLEDGE_MAP = {
    1780413207979: {  # Jeli Gamat 550ML
        'product':     JELI_GAMAT_PRODUCT,
        'audience':    JELI_GAMAT_AUDIENCE,
        'benefits':    JELI_GAMAT_BENEFITS,
        'proof':       JELI_GAMAT_PROOF,
        'objections':  JELI_GAMAT_OBJECTIONS,
        'competitors': JELI_GAMAT_COMPETITORS,
    },
    1780413733080: {  # Jeli Gamat 350ML — same knowledge as 550ML
        'product':     JELI_GAMAT_PRODUCT.replace('550ML dan 350ML', '350ML'),
        'audience':    JELI_GAMAT_AUDIENCE,
        'benefits':    JELI_GAMAT_BENEFITS,
        'proof':       JELI_GAMAT_PROOF,
        'objections':  JELI_GAMAT_OBJECTIONS.replace('550ML', '350ML').replace('RM1.60', 'sangat berbaloi'),
        'competitors': JELI_GAMAT_COMPETITORS.replace('550ML', '350ML'),
    },
    1780413806358: {  # Gel Gamat
        'product':    GEL_GAMAT_PRODUCT,
        'benefits':   GEL_GAMAT_BENEFITS,
        'audience':   'Sesiapa yang mengalami luka bakar, masalah kulit, atau parut. Ibu-ibu dengan bayi.',
        'proof':      'Produk Serigama yang dipercayai lebih 20 tahun. Bersijil Halal JAKIM.',
        'objections': '',
        'competitors':'',
    },
    1780414058969: {  # Minyak Urut
        'product':    MINYAK_URUT_PRODUCT,
        'benefits':   MINYAK_URUT_BENEFITS,
        'audience':   'Sesiapa yang mengalami keletihan, sakit otot. Ibu-ibu dengan bayi.',
        'proof':      'Minyak urut semula jadi Serigama. Halal JAKIM.',
        'objections': '',
        'competitors':'',
    },
}

updated = 0
for product in brand.get('products', []):
    pid = product.get('id')
    if pid in KNOWLEDGE_MAP:
        product['knowledge'] = KNOWLEDGE_MAP[pid]
        print(f"Updated: {product['name']} (ID: {pid})")
        updated += 1

# Set active product to Jeli Gamat 550ML
brand['active_product_id'] = 1780413207979

with open(BRAND_PATH, 'w', encoding='utf-8') as f:
    json.dump(brand, f, indent=2, ensure_ascii=False)

print(f"\nDone. Updated {updated} products. Active product set to Jeli Gamat 550ML.")
print("\nKnowledge summary:")
for p in brand['products']:
    kb = p.get('knowledge', {})
    filled = [k for k in ('product','audience','benefits','proof','objections','competitors') if (kb.get(k) or '').strip()]
    print(f"  {p['name']}: {len(filled)}/6 sections filled")
