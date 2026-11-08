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
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import websockets

# ---------------------------------------------------------------- AYARLAR
API_KEY = os.getenv("AISSTREAM_API_KEY")
SENDER_EMAIL = os.getenv("GONDEREN_MAIL")
SENDER_PASSWORD = os.getenv("SIFRE")
RECEIVER_EMAIL = os.getenv("ALICI_MAIL")        # gemi mailleri + bakım bildirimi (müşteri)
ADMIN_EMAIL = os.getenv("HATA_ALICI_MAIL")      # teknik hata mailleri + tarama raporu (yönetici / sen)
RECEIVER_EMAIL_2 = os.getenv("ALICI_MAIL_2")    # ikinci adres (isteğe bağlı)
# İkinci adres neleri alsın?
#   "cikis" -> sadece gemi ÇIKIŞ (ayrıldı / sinyal koptu) mailleri (varsayılan)
#   "hepsi" -> ilk adresle aynı: giriş + çıkış + bakım bildirimleri
RECEIVER_2_KAPSAM = os.getenv("ALICI_MAIL_2_KAPSAM", "cikis").strip().lower()
# Sadece tankerler takip edilir (AIS tipi 80-89). Boyu bu değerden KÜÇÜK olan tankerler
# (ikmal tankeri, mavna vb.) elenir. Boy bilinmiyorsa gemi elenmez.
MIN_BOY_M = int(os.getenv("MIN_TANKER_BOY_M", "100"))

AIS_URL = "wss://stream.aisstream.io/v0/stream"

# Geniş kutu: gemi dar bölgeden çıkınca da görebilelim diye
ABONELIK_KUTUSU = [[41.20, 28.65], [41.42, 28.92]]
# Dar kutu: İGA Fuel Services iskele sahası
BOUNDING_BOX = [[41.280, 28.740], [41.340, 28.830]]

# Demirli gemiler yaklaşık 3 dakikada bir konum yayınlar,
# bu yüzden dinleme süresi 3 dakikadan uzun olmalı.
DINLEME_SURESI = int(os.getenv("DINLEME_SURESI", "240"))
DURUM_DOSYASI = os.getenv("DURUM_DOSYASI", "durum.json")

DISARI_ONAY_SN = 60      # Dışarıda bu kadar süre görülürse çıkış kesinleşir
KACIRMA_ESIGI = int(os.getenv("KACIRMA_ESIGI", "2"))   # Üst üste bu kadar taramada hiç görülmezse çıkış sayılır
UNUTMA_SURESI = 3 * 86400  # Bölgede olmayan gemi kayıtları 3 gün sonra silinir
# "Gemi bulunamadı" maili: en fazla kaç dakikada bir gönderilsin?
#   -1  -> hiç gönderme (varsayılan)
#   60  -> saatte bir
#   0   -> her taramada
BOS_MAIL_ARALIGI_DK = int(os.getenv("BOS_MAIL_ARALIGI_DK", "-1"))
# Üst üste bu kadar tarama başarısız olursa "sistem hatası" maili gönderilir
HATA_MAIL_ESIGI = int(os.getenv("HATA_MAIL_ESIGI", "2"))
# İsteğe bağlı: dışarıdan izleme servisi (ör. healthchecks.io) adresi
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "").strip()
# Her taramadan sonra yöneticiye (HATA_ALICI_MAIL) tarama raporu maili: 1=aç, 0=kapat
RAPOR_MAILI = os.getenv("RAPOR_MAILI", "1") == "1"
TARAMA_ARALIGI_DK = os.getenv("TARAMA_ARALIGI_DK", "?")   # sadece bilgi amaçlı (cron ile aynı olmalı)
YENIDEN_BEKLE = 5        # Bağlantı koparsa yeniden denemeden önce bekleme (sn)

TR = ZoneInfo("Europe/Istanbul")

# Bu imza değişince (kod ya da ayar güncellenince) ilk çalışmada bilgi maili gider
SURUM = (f"v7|mail2={RECEIVER_2_KAPSAM}|aralik={TARAMA_ARALIGI_DK}|dinleme={DINLEME_SURESI}"
         f"|kacirma={KACIRMA_ESIGI}|hata={HATA_MAIL_ESIGI}|sadece-tanker>={MIN_BOY_M}m")


# ---------------------------------------------------------------- YARDIMCILAR
def hedef_tip_mi(tip):
    """Sadece tankerler (AIS tipi 80-89). Yük gemileri (70-79) artık takip edilmez."""
    return tip is not None and 80 <= tip <= 89


def hedef_gemi_mi(tip, boy):
    """Tanker VE (boy bilinmiyorsa ya da MIN_BOY_M'den kısa değilse)."""
    if not hedef_tip_mi(tip):
        return False
    return boy is None or boy >= MIN_BOY_M


def sessiz_sifirla(kayit):
    """Artık hedef olmayan geminin 'içeride' durumunu mail atmadan kapatır."""
    kayit["icerde"] = False
    kayit["giris_ts"] = None
    kayit["disarida_ts"] = None
    kayit["kacirilan"] = 0
    kayit["mail_giris"] = False


def bolgede_mi(lat, lon):
    min_lat, min_lon = BOUNDING_BOX[0]
    max_lat, max_lon = BOUNDING_BOX[1]
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


ISKELE_NOKTASI = (41.310, 28.788)   # ASENA'nın iskeledeki konumu (uzaklık hesabı için referans)


def iskele_uzaklik_km(lat, lon):
    from math import cos, radians, hypot
    dy = (lat - ISKELE_NOKTASI[0]) * 111.2
    dx = (lon - ISKELE_NOKTASI[1]) * 111.2 * cos(radians(ISKELE_NOKTASI[0]))
    return hypot(dx, dy)


def gozlem_sirala(gozlem):
    """Önce dar kutudakiler, sonra iskeleye en yakınlar. Konumsuzlar en sona."""
    satirlar = []
    for mmsi, g in gozlem.items():
        son = g.get("son")
        if son:
            lat, lon, sog = son
            satirlar.append((mmsi, g, bolgede_mi(lat, lon), iskele_uzaklik_km(lat, lon), lat, lon, sog))
        else:
            satirlar.append((mmsi, g, False, 1e9, None, None, None))
    satirlar.sort(key=lambda x: (not x[2], x[3]))
    return satirlar


def gozlem_satiri(x):
    mmsi, g, kutuda, km, lat, lon, sog = x
    if lat is None:
        return f"{g['ad'] or '?'} | {mmsi} | tip {g['tip']} | boy {g.get('boy') or '?'} m | konum yok"
    return (f"{g['ad'] or '?'} | {mmsi} | tip {g['tip']} | boy {g.get('boy') or '?'} m | "
            f"{lat:.4f}, {lon:.4f} | {sog} kn | "
            f"iskeleye {km:.1f} km | {'KUTUDA' if kutuda else 'dışarıda'}")


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


def rapor_maili(gozlem, basarili, baslangic_ts, durum, surum_notu=None):
    """Her taramadan sonra yöneticiye giden rapor: ne zaman başladı, ne görüldü, ne gönderilecek."""
    t, s_ = tarih_saat(baslangic_ts)
    iceri = [k["ad"] for k in durum["gemiler"].values() if k["icerde"]]
    gonderilecek = [m["konu"] for m in durum["bekleyen_mailler"] if m.get("hedef", "gemi").startswith("gemi")]
    if basarili:
        konu = f"📋 İGA Tarama Raporu: {len(gozlem)} gemi görüldü, iskele sahasında {len(iceri)}"
    else:
        konu = "⚠️ İGA Tarama Raporu: tarama BAŞARISIZ"
    satirlar = [
        f"- Tarama başladı        : {t} {s_}",
        f"- Sonuç                 : {'Başarılı' if basarili else 'BAŞARISIZ (AISStream bağlantı sorunu)'}",
        f"- Dinleme süresi        : {DINLEME_SURESI} sn (tarama aralığı: {TARAMA_ARALIGI_DK} dk)",
        f"- Geniş alanda veri alınan gemi sayısı : {len(gozlem)}",
        f"- İskele sahasında takipteki gemi      : {', '.join(iceri) if iceri else 'yok'}",
        "- Müşteriye giden bildirimler          : " + (("; ".join(gonderilecek)) if gonderilecek else "yok"),
    ]
    if surum_notu:
        satirlar += ["", surum_notu]
    if basarili and not gozlem:
        satirlar += ["", "Not: AISStream bu taramada bölgeden hiç veri vermedi. Gemi limanda olsa bile "
                         "bu olabilir (AISStream'in o kıyıdaki alıcı kapsaması sınırlı olabilir)."]
    if gozlem:
        sirali = gozlem_sirala(gozlem)
        kutuda_adet = sum(1 for x in sirali if x[2])
        satirlar += ["", f"Dar kutuda (iskele sahası) görülen gemi sayısı: {kutuda_adet}",
                     "Gemiler, önce kutudakiler sonra iskeleye en yakınlar olacak şekilde "
                     "(ad | MMSI | tip | konum | hız | iskeleye uzaklık | kutuda mı):"]
        for x in sirali[:20]:
            satirlar.append("  " + gozlem_satiri(x))
        if len(sirali) > 20:
            satirlar.append(f"  ... ve {len(sirali) - 20} gemi daha (iskeleden daha uzakta)")
    return {"konu": konu, "govde": "\n".join(satirlar)}


def hata_maili(adet, baslangic_ts, simdi):
    bt, bs = tarih_saat(baslangic_ts)
    t, s_ = tarih_saat(simdi)
    konu = "⚠️ İGA Gemi Takip Ajanı: SİSTEM HATASI"
    govde = "\n".join([
        f"Üst üste {adet} tarama AISStream bağlantısı kurulamadığı / hata alındığı için başarısız oldu.",
        "Bu süre içinde gemi giriş/çıkışları takip EDİLEMİYOR olabilir.",
        "",
        f"- İlk hata       : {bt} {bs}",
        f"- Şu an          : {t} {s_}",
        "",
        "Kontrol et: GitHub → Actions → son çalışmanın log'u. "
        "Olası sebepler: AISStream API anahtarı, AISStream kesintisi, ağ sorunu.",
        "Sistem düzelince ayrıca bilgi maili gelecek.",
    ])
    return {"konu": konu, "govde": govde}


def duzeldi_maili(baslangic_ts, simdi):
    bt, bs = tarih_saat(baslangic_ts)
    t, s_ = tarih_saat(simdi)
    konu = "✅ İGA Gemi Takip Ajanı: Sistem yeniden çalışıyor"
    govde = "\n".join([
        "Taramalar yeniden başarılı.",
        "",
        f"- Hata başlangıcı : {bt} {bs}",
        f"- Düzelme         : {t} {s_}",
    ])
    return {"konu": konu, "govde": govde}


def bakim_maili(simdi):
    t, s_ = tarih_saat(simdi)
    konu = "🛠️ İGA Gemi Takip Sistemi: Teknik bakım"
    govde = "\n".join([
        "Gemi takip sistemi geçici olarak teknik bakıma alınmıştır.",
        "Bu süre içinde gemi giriş/çıkış bildirimleri gelmeyebilir.",
        "",
        f"- Bildirim zamanı : {t} {s_}",
        "",
        "Çalışmalar tamamlandığında ayrıca bilgilendirileceksiniz.",
    ])
    return {"konu": konu, "govde": govde}


def bakim_bitti_maili(simdi):
    t, s_ = tarih_saat(simdi)
    konu = "✅ İGA Gemi Takip Sistemi: Bakım tamamlandı"
    govde = "\n".join([
        "Teknik bakım tamamlandı, gemi takip sistemi yeniden aktif.",
        "",
        f"- Bildirim zamanı : {t} {s_}",
    ])
    return {"konu": konu, "govde": govde}


def kuyruga_ekle(durum, mail, hedef="gemi"):
    """hedef:
      'gemi'       -> ALICI_MAIL (müşteri); ALICI_MAIL_2 sadece KAPSAM='hepsi' ise
      'gemi_cikis' -> ALICI_MAIL + ALICI_MAIL_2 (çıkış bildirimi)
      'yonetici'   -> HATA_ALICI_MAIL (teknik)"""
    mail = dict(mail)
    mail["hedef"] = hedef
    durum["bekleyen_mailler"].append(mail)


def hata_takibi(durum, basarili, simdi):
    """Üst üste başarısız taramaları sayar; eşiği aşınca bir kez hata maili, düzelince bir kez bilgi maili."""
    if basarili:
        if durum.get("hata_maili_gonderildi"):
            kuyruga_ekle(durum, duzeldi_maili(durum.get("hata_baslangic_ts") or simdi, simdi), "yonetici")
            kuyruga_ekle(durum, bakim_bitti_maili(simdi), "gemi")
            print("[SİSTEM] Taramalar düzeldi, bilgi maili kuyruğa alındı.")
        durum["ardisik_basarisiz"] = 0
        durum["hata_maili_gonderildi"] = False
        durum["hata_baslangic_ts"] = None
        return
    if not durum.get("ardisik_basarisiz"):
        durum["hata_baslangic_ts"] = simdi
    durum["ardisik_basarisiz"] = durum.get("ardisik_basarisiz", 0) + 1
    print(f"[SİSTEM] Başarısız tarama (üst üste {durum['ardisik_basarisiz']}).")
    if (HATA_MAIL_ESIGI > 0 and durum["ardisik_basarisiz"] >= HATA_MAIL_ESIGI
            and not durum.get("hata_maili_gonderildi")):
        kuyruga_ekle(durum, hata_maili(durum["ardisik_basarisiz"], durum["hata_baslangic_ts"], simdi), "yonetici")
        kuyruga_ekle(durum, bakim_maili(simdi), "gemi")
        durum["hata_maili_gonderildi"] = True
        print("[SİSTEM] Hata maili kuyruğa alındı.")


def saglik_bildir(basarili):
    """Başarılı tarama sonrası dışarıdaki izleme servisine 'çalışıyorum' sinyali gönderir."""
    if not HEALTHCHECK_URL or not basarili:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL, timeout=10).read()
        print("[SİSTEM] Sağlık sinyali gönderildi.")
    except Exception as e:
        print(f"[SİSTEM] Sağlık sinyali gönderilemedi: {e}")


def bos_tarama_maili(ad_sayisi, simdi):
    t, s_ = tarih_saat(simdi)
    konu = "🔍 İGA Limanı Tarama: Gemi bulunamadı"
    if BOS_MAIL_ARALIGI_DK > 0:
        sikllik = f"Bu bilgi maili en fazla {BOS_MAIL_ARALIGI_DK} dakikada bir gönderilir."
    else:
        sikllik = "Bu bilgi maili her taramada gönderilir."
    govde = "\n".join([
        "Tarama tamamlandı, İGA Fuel Services iskele sahasında tanker/yük gemisi tespit edilmedi.",
        "",
        f"- Tarama Tarihi  : {t}",
        f"- Tarama Saati   : {s_}",
        f"- Dinleme Süresi : {DINLEME_SURESI} sn",
        f"- Geniş alanda veri alınan gemi sayısı : {ad_sayisi}",
        "- Bölgede takipteki gemi sayısı        : 0",
        "",
        sikllik,
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
def adresleri_ayir(deger):
    """'a@x.com, b@x.com' veya 'a@x.com;b@x.com' -> liste"""
    return [a.strip() for a in (deger or "").replace(";", ",").split(",") if a.strip()]


def alici_adresi(hedef):
    """'yonetici' -> hata adresi (yoksa ALICI_MAIL); 'gemi_cikis' -> ALICI_MAIL + ALICI_MAIL_2;
    'gemi' -> ALICI_MAIL (+ ALICI_MAIL_2 sadece KAPSAM='hepsi' ise)"""
    if hedef == "yonetici":
        return ADMIN_EMAIL or RECEIVER_EMAIL
    ikinci_alsin = hedef == "gemi_cikis" or RECEIVER_2_KAPSAM == "hepsi"
    return ", ".join(a for a in (RECEIVER_EMAIL, RECEIVER_EMAIL_2 if ikinci_alsin else None) if a)


def mail_gonder(konu, govde, alici=None):
    """Başarılıysa True döner. 3 kez dener."""
    adresler = list(dict.fromkeys(adresleri_ayir(alici if alici is not None else RECEIVER_EMAIL)))
    if not (SENDER_EMAIL and SENDER_PASSWORD and adresler):
        print("[MAIL] E-posta ayarları eksik, mail kuyrukta bekletiliyor.")
        return False

    for deneme in range(1, 4):
        try:
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = ", ".join(adresler)
            msg["Subject"] = konu
            msg.attach(MIMEText(govde, "plain", "utf-8"))
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                server.sendmail(SENDER_EMAIL, adresler, msg.as_string())
            print(f"[MAIL] Gönderildi ({', '.join(adresler)}): {konu}")
            return True
        except Exception as e:
            print(f"[MAIL] Deneme {deneme}/3 başarısız: {e}")
            if deneme < 3:
                time.sleep(5)
    return False


def bekleyenleri_gonder(durum):
    kalan = []
    for m in durum["bekleyen_mailler"]:
        if not mail_gonder(m["konu"], m["govde"], alici_adresi(m.get("hedef", "gemi"))):
            kalan.append(m)
    durum["bekleyen_mailler"] = kalan


# ---------------------------------------------------------------- DİNLEME
def mesaji_isle(mesaj, gozlem, simdi):
    meta = mesaj.get("MetaData", {})
    mmsi = meta.get("MMSI")
    if not mmsi:
        return
    g = gozlem.setdefault(mmsi, {"ad": "", "tip": None, "boy": None, "pozlar": []})

    ad = (meta.get("ShipName") or "").strip()
    if ad:
        g["ad"] = ad

    tur = mesaj.get("MessageType")
    if tur == "ShipStaticData":
        statik = mesaj.get("Message", {}).get("ShipStaticData", {})
        tip = statik.get("Type")
        if tip:
            g["tip"] = tip
        dim = statik.get("Dimension") or {}
        boy = (dim.get("A") or 0) + (dim.get("B") or 0)   # A+B = gemi boyu (m)
        if boy > 0:
            g["boy"] = boy
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
    for x in gozlem_sirala(gozlem)[:60]:
        print("[KEŞİF]   " + gozlem_satiri(x))


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
        kuyruga_ekle(
            durum,
            cikis_maili(kayit["ad"], mmsi, kayit["giris_ts"], cikis_ts, sinyal_koptu),
            "gemi_cikis")
        print(f"[ÇIKIŞ] {kayit['ad']} ({'sinyal koptu' if sinyal_koptu else 'ayrıldı'})")
    kayit["icerde"] = False
    kayit["disarida_ts"] = None
    kayit["kacirilan"] = 0
    kayit["mail_giris"] = False


def giris_maili_kuyrukla(durum, mmsi, kayit):
    kuyruga_ekle(durum, giris_maili(kayit["ad"], mmsi, kayit["giris_ts"]), "gemi")
    kayit["mail_giris"] = True
    print(f"[GİRİŞ] {kayit['ad']}")


def degerlendir(durum, gozlem, simdi, basarili):
    gemiler = durum["gemiler"]
    konum_gorulenler = set()

    # Eskiden takip edilen ama artık hedef olmayan (örn. yük gemisi) kayıtları sessizce kapat,
    # yoksa "kaçırıldı" sayılıp yanlış ÇIKIŞ maili giderdi.
    for v in gemiler.values():
        if v.get("tip") not in (None, 0) and not hedef_gemi_mi(v.get("tip"), v.get("boy")) and v["icerde"]:
            sessiz_sifirla(v)

    for mmsi, g in gozlem.items():
        anahtar = str(mmsi)
        kayit = gemiler.setdefault(anahtar, yeni_kayit(mmsi, simdi))
        # Depoya gereksiz commit düşmesin diye en fazla saatte bir güncellenir
        if simdi - kayit["son_genel_ts"] > 3600:
            kayit["son_genel_ts"] = simdi
        if g["ad"]:
            kayit["ad"] = g["ad"]
        if g["tip"] is not None:
            kayit["tip"] = g["tip"]
        if g.get("boy"):
            kayit["boy"] = g["boy"]

        tip = kayit["tip"]
        # Tipi biliniyor ve hedef dışı (tanker olmayan ya da çok kısa tanker) -> takip etme
        if tip not in (None, 0) and not hedef_gemi_mi(tip, kayit.get("boy")):
            if kayit["icerde"]:
                sessiz_sifirla(kayit)
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
                    if hedef_gemi_mi(kayit["tip"], kayit.get("boy")):
                        giris_maili_kuyrukla(durum, mmsi, kayit)
            elif kayit["icerde"]:
                if kayit["disarida_ts"] is None:
                    kayit["disarida_ts"] = ts
                elif ts - kayit["disarida_ts"] >= DISARI_ONAY_SN:
                    cikis_yap(durum, mmsi, kayit, kayit["disarida_ts"], False)

        # Tip sonradan öğrenildiyse (içerideyken) giriş mailini şimdi at
        if kayit["icerde"] and hedef_gemi_mi(kayit["tip"], kayit.get("boy")) and not kayit["mail_giris"]:
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


def bos_tarama_kontrol(durum, gozlem, simdi, basarili):
    """Tarama başarılıysa ve bölgede hiç gemi yoksa 'gemi bulunamadı' maili kuyruğa alır."""
    if BOS_MAIL_ARALIGI_DK < 0 or not basarili:
        return
    if any(k["icerde"] for k in durum["gemiler"].values()):
        return
    son = durum.get("son_bos_mail_ts") or 0
    if BOS_MAIL_ARALIGI_DK > 0 and simdi - son < BOS_MAIL_ARALIGI_DK * 60:
        return
    durum["bekleyen_mailler"].append(bos_tarama_maili(len(gozlem), simdi))
    durum["son_bos_mail_ts"] = simdi
    print("[TARAMA] Bölgede gemi yok, bilgi maili kuyruğa alındı.")


# ---------------------------------------------------------------- ANA
async def ana():
    t0 = time.time()
    durum = durum_yukle()
    if not ADMIN_EMAIL:
        print("[UYARI] HATA_ALICI_MAIL tanımlı değil, hata/test mailleri ALICI_MAIL adresine gidecek.")
    if not API_KEY:
        print("[HATA] AISSTREAM_API_KEY bulunamadı!")
        gozlem, basarili = {}, False
    else:
        gozlem, basarili = await dinle(DINLEME_SURESI)
    print(f"[AIS] {len(gozlem)} gemiden veri alındı. Başarılı: {basarili}")
    kesif_yaz(gozlem)

    simdi = time.time()
    hata_takibi(durum, basarili, simdi)
    surum_notu = None
    if durum.get("kurulu_surum") != SURUM:
        surum_notu = "Kod/ayarlar değişti: sistem yeni ayarlarla başlatıldı."
        durum["kurulu_surum"] = SURUM
    degerlendir(durum, gozlem, simdi, basarili)
    bos_tarama_kontrol(durum, gozlem, simdi, basarili)
    if RAPOR_MAILI:
        if ADMIN_EMAIL:
            kuyruga_ekle(durum, rapor_maili(gozlem, basarili, t0, durum, surum_notu), "yonetici")
        else:
            print("[UYARI] HATA_ALICI_MAIL tanımlı değil, tarama raporu maili gönderilmeyecek.")
    bekleyenleri_gonder(durum)
    durum_kaydet(durum)
    saglik_bildir(basarili)

    icerde = [k["ad"] for k in durum["gemiler"].values() if k["icerde"]]
    print(f"[ÖZET] Bölgede takipteki gemi: {len(icerde)} {icerde} | "
          f"Bekleyen mail: {len(durum['bekleyen_mailler'])}")
    return 0 if basarili else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(ana()))
