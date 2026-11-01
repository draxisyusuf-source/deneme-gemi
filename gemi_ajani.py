"""
Gemi Takip Ajanı - GitHub Actions sürümü

Her çalışmada:
  1. durum.json dosyasını okur (önceki çalışmadan kalan hafıza)
  2. AISStream'i DINLEME_SURESI saniye dinler
  3. Giriş / çıkış olaylarını hesaplar
  4. Mailleri gönderir (gönderilemeyenler bir sonraki çalışmaya kalır)
  5. durum.json dosyasını kaydeder ve kapanır
"""
import asyncio
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import websockets

# ---------------------------------------------------------------- AYARLAR
API_KEY = os.getenv("AISSTREAM_API_KEY")
SENDER_EMAIL = os.getenv("GONDEREN_MAIL")
SENDER_PASSWORD = os.getenv("SIFRE")
RECEIVER_EMAIL = os.getenv("ALICI_MAIL")

AIS_URL = "wss://stream.aisstream.io/v0/stream"

# Geniş kutu: gemi dar bölgeden çıkınca da görebilelim diye
ABONELIK_KUTUSU = [[51.80, 3.50], [52.10, 4.40]]   # TEST: Rotterdam açıkları (geniş)
# Dar kutu: İGA Fuel Services iskele sahası
BOUNDING_BOX = [[51.90, 3.95], [52.00, 4.20]]      # TEST: Europoort / Maasvlakte (dar)

# Demirli gemiler yaklaşık 3 dakikada bir konum yayınlar,
# bu yüzden dinleme süresi 3 dakikadan uzun olmalı.
DINLEME_SURESI = int(os.getenv("DINLEME_SURESI", "120"))
DURUM_DOSYASI = os.getenv("DURUM_DOSYASI", "durum.json")

DISARI_ONAY_SN = 60      # Dışarıda bu kadar süre görülürse çıkış kesinleşir
KACIRMA_ESIGI = 2        # Üst üste bu kadar çalışmada hiç görülmezse çıkış sayılır
UNUTMA_SURESI = 3 * 86400  # Bölgede olmayan gemi kayıtları 3 gün sonra silinir
MAKS_MAIL = 5            # TEST: bir çalışmada en fazla bu kadar gemi maili
YENIDEN_BEKLE = 5        # Bağlantı koparsa yeniden denemeden önce bekleme (sn)

TR = ZoneInfo("Europe/Istanbul")


# ---------------------------------------------------------------- YARDIMCILAR
def hedef_tip_mi(tip):
    """TEST SÜRÜMÜ: filtre kapalı, her gemi hedef sayılır."""
    return True


def bolgede_mi(lat, lon):
    min_lat, min_lon = BOUNDING_BOX[0]
    max_lat, max_lon = BOUNDING_BOX[1]
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def tarih_saat(ts):
    d = datetime.fromtimestamp(ts, TR)
    return d.strftime("%d-%m-%Y"), d.strftime("%H:%M:%S")


NOT = ("Not: Tarama periyodik yapıldığı için saatler geminin ilk tespit "
       "edildiği anı gösterir, birkaç dakika-saat sapma olabilir.")


def giris_maili(ad, mmsi, giris_ts):
    t, s = tarih_saat(giris_ts)
    konu = f"🚢 İGA Limanı Giriş: {ad} (MMSI: {mmsi})"
    govde = "\n".join([
        "İGA Fuel Services Limanı Bölgesine Hedef Gemi Girdi!",
        "",
        f"- Gemi Adı       : {ad}",
        f"- MMSI Numarası  : {mmsi}",
        f"- Giriş Tarihi   : {t}",
        f"- Giriş Saati    : {s}",
        "",
        NOT,
    ])
    return {"konu": konu, "govde": govde}


def cikis_maili(ad, mmsi, giris_ts, cikis_ts, sinyal_koptu):
    gt, gs = tarih_saat(giris_ts)
    ct, cs = tarih_saat(cikis_ts)
    ek = " (Sinyal Koptu)" if sinyal_koptu else ""
    baslik = ("İGA Fuel Services Limanı Bölgesinden Gemi Sinyali Kesti/Ayrıldı!"
              if sinyal_koptu else
              "İGA Fuel Services Limanı Bölgesinden Gemi Ayrıldı (Çıktı)!")
    konu = f"🏁 İGA Limanı Çıkış{ek}: {ad} (MMSI: {mmsi})"
    govde = "\n".join([
        baslik,
        "",
        f"- Gemi Adı       : {ad}",
        f"- MMSI Numarası  : {mmsi}",
        f"- Giriş Tarihi   : {gt}",
        f"- Giriş Saati    : {gs}",
        f"- Çıkış Tarihi   : {ct}",
        f"- Çıkış Saati    : {cs}",
        "",
        NOT,
    ])
    return {"konu": konu, "govde": govde}


# ---------------------------------------------------------------- DURUM DOSYASI
def bos_durum():
    return {"gemiler": {}, "bekleyen_mailler": []}


def durum_yukle():
    try:
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as f:
            durum = json.load(f)
        durum.setdefault("gemiler", {})
        durum.setdefault("bekleyen_mailler", [])
        return durum
    except FileNotFoundError:
        print("[DURUM] durum.json yok, sıfırdan başlanıyor.")
    except Exception as e:
        print(f"[DURUM] durum.json okunamadı ({e}), sıfırdan başlanıyor.")
    return bos_durum()


def durum_kaydet(durum):
    gecici = DURUM_DOSYASI + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(gecici, DURUM_DOSYASI)


# ---------------------------------------------------------------- MAIL
def mail_gonder(konu, govde):
    """Başarılıysa True döner. 3 kez dener."""
    if not (SENDER_EMAIL and SENDER_PASSWORD and RECEIVER_EMAIL):
        print("[MAIL] E-posta ayarları eksik, mail kuyrukta bekletiliyor.")
        return False

    for deneme in range(1, 4):
        try:
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = RECEIVER_EMAIL
            msg["Subject"] = konu
            msg.attach(MIMEText(govde, "plain", "utf-8"))
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
            print(f"[MAIL] Gönderildi: {konu}")
            return True
        except Exception as e:
            print(f"[MAIL] Deneme {deneme}/3 başarısız: {e}")
            if deneme < 3:
                time.sleep(5)
    return False


def bekleyenleri_gonder(durum):
    kalan = []
    for m in durum["bekleyen_mailler"]:
        if not mail_gonder(m["konu"], m["govde"]):
            kalan.append(m)
    durum["bekleyen_mailler"] = kalan


# ---------------------------------------------------------------- DİNLEME
def mesaji_isle(mesaj, gozlem, simdi):
    meta = mesaj.get("MetaData", {})
    mmsi = meta.get("MMSI")
    if not mmsi:
        return
    g = gozlem.setdefault(mmsi, {"ad": "", "tip": None, "pozlar": []})

    ad = (meta.get("ShipName") or "").strip()
    if ad:
        g["ad"] = ad

    tur = mesaj.get("MessageType")
    if tur == "ShipStaticData":
        tip = mesaj.get("Message", {}).get("ShipStaticData", {}).get("Type")
        if tip:
            g["tip"] = tip
    elif tur == "PositionReport":
        p = mesaj.get("Message", {}).get("PositionReport", {})
        lat, lon = p.get("Latitude"), p.get("Longitude")
        if lat is not None and lon is not None:
            g["pozlar"].append((simdi, lat, lon))
            g["son"] = (lat, lon, p.get("Sog"))


async def dinle(sure):
    """(gözlemler, başarılı_mı) döner."""
    gozlem = {}
    bitis = time.time() + sure

    while time.time() < bitis:
        try:
            async with websockets.connect(AIS_URL) as ws:
                await ws.send(json.dumps({
                    "APIKey": API_KEY,
                    "BoundingBoxes": [ABONELIK_KUTUSU],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                print(f"[AIS] Bağlandı, {sure} sn dinleniyor...")
                while True:
                    kalan = bitis - time.time()
                    if kalan <= 0:
                        return gozlem, True
                    try:
                        ham = await asyncio.wait_for(ws.recv(), timeout=kalan)
                    except asyncio.TimeoutError:
                        return gozlem, True
                    try:
                        mesaj = json.loads(ham)
                    except Exception:
                        continue
                    if "error" in mesaj:
                        print(f"[AISSTREAM HATA] {mesaj['error']}")
                        return gozlem, False
                    mesaji_isle(mesaj, gozlem, time.time())
        except Exception as e:
            print(f"[AIS] Bağlantı sorunu: {e}")
            await asyncio.sleep(YENIDEN_BEKLE)

    return gozlem, False


def kesif_yaz(gozlem):
    """Görülen gemileri log'a yazar: kutuyu doğrulamak için."""
    print("[KEŞİF] Görülen gemiler (ad | MMSI | tip | enlem, boylam | hız knot | dar kutuda mı):")
    for mmsi, g in list(gozlem.items())[:60]:
        son = g.get("son")
        if son:
            lat, lon, sog = son
            konum = f"{lat:.4f}, {lon:.4f}"
            kutuda = "EVET" if bolgede_mi(lat, lon) else "hayır"
        else:
            konum, sog, kutuda = "konum yok", None, "-"
        print(f"[KEŞİF]   {g['ad'] or '?'} | {mmsi} | tip {g['tip']} | {konum} | {sog} kn | {kutuda}")


# ---------------------------------------------------------------- MANTIK
def yeni_kayit(mmsi, simdi):
    return {
        "ad": f"MMSI-{mmsi}",
        "tip": None,
        "icerde": False,
        "giris_ts": None,
        "son_gorulme_ts": None,   # bölge İÇİNDE son görülme
        "disarida_ts": None,      # bölge DIŞINDA ilk görülme (çıkış sayacı)
        "kacirilan": 0,
        "mail_giris": False,      # giriş maili kuyruğa alındı mı
        "son_genel_ts": simdi,
    }


def cikis_yap(durum, mmsi, kayit, cikis_ts, sinyal_koptu):
    if kayit["mail_giris"]:
        durum["bekleyen_mailler"].append(
            cikis_maili(kayit["ad"], mmsi, kayit["giris_ts"], cikis_ts, sinyal_koptu))
        print(f"[ÇIKIŞ] {kayit['ad']} ({'sinyal koptu' if sinyal_koptu else 'ayrıldı'})")
    kayit["icerde"] = False
    kayit["disarida_ts"] = None
    kayit["kacirilan"] = 0
    kayit["mail_giris"] = False


def giris_maili_kuyrukla(durum, mmsi, kayit):
    durum["bekleyen_mailler"].append(giris_maili(kayit["ad"], mmsi, kayit["giris_ts"]))
    kayit["mail_giris"] = True
    print(f"[GİRİŞ] {kayit['ad']}")


def degerlendir(durum, gozlem, simdi, basarili):
    gemiler = durum["gemiler"]
    konum_gorulenler = set()

    for mmsi, g in gozlem.items():
        anahtar = str(mmsi)
        kayit = gemiler.setdefault(anahtar, yeni_kayit(mmsi, simdi))
        kayit["son_genel_ts"] = simdi
        if g["ad"]:
            kayit["ad"] = g["ad"]
        if g["tip"] is not None:
            kayit["tip"] = g["tip"]

        tip = kayit["tip"]
        # Tipi biliniyor ve hedef dışı (balıkçı, yolcu, römorkör...) -> takip etme
        if tip not in (None, 0) and not hedef_tip_mi(tip):
            continue

        if g["pozlar"]:
            konum_gorulenler.add(anahtar)
            kayit["kacirilan"] = 0

        for ts, lat, lon in g["pozlar"]:
            if bolgede_mi(lat, lon):
                kayit["disarida_ts"] = None
                kayit["son_gorulme_ts"] = ts
                if not kayit["icerde"]:
                    kayit["icerde"] = True
                    kayit["giris_ts"] = ts
                    kayit["mail_giris"] = False
                    if hedef_tip_mi(kayit["tip"]):
                        giris_maili_kuyrukla(durum, mmsi, kayit)
            elif kayit["icerde"]:
                if kayit["disarida_ts"] is None:
                    kayit["disarida_ts"] = ts
                elif ts - kayit["disarida_ts"] >= DISARI_ONAY_SN:
                    cikis_yap(durum, mmsi, kayit, kayit["disarida_ts"], False)

        # Tip sonradan öğrenildiyse (içerideyken) giriş mailini şimdi at
        if kayit["icerde"] and hedef_tip_mi(kayit["tip"]) and not kayit["mail_giris"]:
            giris_maili_kuyrukla(durum, mmsi, kayit)

    # Bu çalışmada hiç konumu gelmeyen içerideki gemiler
    if basarili:
        for anahtar, kayit in gemiler.items():
            if kayit["icerde"] and anahtar not in konum_gorulenler:
                kayit["kacirilan"] += 1
                if kayit["kacirilan"] >= KACIRMA_ESIGI:
                    ref = kayit["disarida_ts"] or kayit["son_gorulme_ts"] or simdi
                    cikis_yap(durum, anahtar, kayit, ref, True)

    # Eski kayıtları temizle
    for anahtar in [k for k, v in gemiler.items()
                    if not v["icerde"] and simdi - v["son_genel_ts"] > UNUTMA_SURESI]:
        del gemiler[anahtar]


# ---------------------------------------------------------------- ANA
async def ana():
    if not API_KEY:
        print("[HATA] AISSTREAM_API_KEY bulunamadı!")
        return 1

    durum = durum_yukle()
    gozlem, basarili = await dinle(DINLEME_SURESI)
    print(f"[AIS] {len(gozlem)} gemiden veri alındı. Başarılı: {basarili}")
    kesif_yaz(gozlem)

    degerlendir(durum, gozlem, time.time(), basarili)

    # TEST: mail yağmurunu engelle
    if len(durum["bekleyen_mailler"]) > MAKS_MAIL:
        print(f"[TEST] {len(durum['bekleyen_mailler'])} gemi maili oluştu, "
              f"sadece ilk {MAKS_MAIL} tanesi gönderilecek.")
        durum["bekleyen_mailler"] = durum["bekleyen_mailler"][:MAKS_MAIL]
    # TEST: gemiden bağımsız, Gmail ayarını doğrulayan mail
    durum["bekleyen_mailler"].insert(0, {
        "konu": "✅ Gemi Ajanı TEST maili",
        "govde": ("Bu bir test mailidir. Bu maili aldıysan Gmail ayarların (uygulama şifresi, "
                  "alıcı adresi) doğru çalışıyor demektir.\n\n"
                  f"Bu çalışmada {len(gozlem)} gemiden veri alındı."),
    })
    bekleyenleri_gonder(durum)
    durum_kaydet(durum)

    icerde = [k["ad"] for k in durum["gemiler"].values() if k["icerde"]]
    print(f"[ÖZET] Bölgede takipteki gemi: {len(icerde)} {icerde} | "
          f"Bekleyen mail: {len(durum['bekleyen_mailler'])}")
    return 0 if basarili else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(ana()))
