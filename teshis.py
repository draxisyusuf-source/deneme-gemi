#!/usr/bin/env python3
"""
TEŞHİS: AISStream limanı ve çevresini duyuyor mu?

Ana ajanı (gemi_ajani.py) ETKİLEMEZ: durum.json'a dokunmaz, müşteriye mail atmaz.
Geniş bir alanı (liman + Karadeniz kıyısı + Boğaz + Marmara) kısa süre dinler,
bölge bölge kaç mesaj/gemi geldiğini sayar ve sonucu SADECE yöneticiye mail atar.
Böylece hem AISStream kapsamasını hem de mail hattını aynı anda test eder.
"""
import asyncio
import json
import os
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import websockets

API_KEY = os.getenv("AISSTREAM_API_KEY")
SENDER_EMAIL = os.getenv("GONDEREN_MAIL")
SENDER_PASSWORD = os.getenv("SIFRE")
ADMIN_EMAIL = os.getenv("HATA_ALICI_MAIL") or SENDER_EMAIL   # müşteriye ASLA gitmez
SURE = int(os.getenv("TESHIS_SURESI", "150"))                  # sn

AIS_URL = "wss://stream.aisstream.io/v0/stream"
TR = ZoneInfo("Europe/Istanbul")

# Tüm test alanı (liman + Boğaz + Marmara kıyısı)
TEST_KUTUSU = [[40.85, 28.40], [41.50, 29.40]]

# (ad, min_lat, min_lon, max_lat, max_lon) -- ilk eşleşen kazanır
BOLGELER = [
    ("1) İskele sahası (ajanın dar kutusu)", 41.28, 28.74, 41.34, 28.83),
    ("2) Liman çevresi (ajanın geniş kutusu)", 41.20, 28.65, 41.42, 28.92),
    ("3) Karadeniz kıyısı (limanın doğusu/batısı)", 41.15, 28.40, 41.50, 29.40),
    ("4) Boğaz", 40.95, 28.90, 41.15, 29.40),
    ("5) Marmara kıyısı (Ambarlı-Yeşilköy)", 40.85, 28.40, 41.15, 29.40),
]
DISI = "6) Diğer"


def bolge_bul(lat, lon):
    for ad, a, b, c, d in BOLGELER:
        if a <= lat <= c and b <= lon <= d:
            return ad
    return DISI


def yeni_sayac():
    return {ad: {"mesaj": 0, "gemi": set()} for ad, *_ in BOLGELER} | {DISI: {"mesaj": 0, "gemi": set()}}


def isle(m, sayac, gemiler):
    tip = m.get("MessageType")
    meta = m.get("MetaData") or {}
    msg = m.get("Message") or {}
    mmsi = str(meta.get("MMSI") or "")
    if not mmsi:
        return
    g = gemiler.setdefault(mmsi, {"ad": "?", "tip": None, "sinif": "A", "bolge": None})
    ad = (meta.get("ShipName") or "").strip()
    if ad:
        g["ad"] = ad

    if tip in ("PositionReport", "StandardClassBPositionReport"):
        p = msg.get(tip) or {}
        lat, lon = p.get("Latitude"), p.get("Longitude")
        if lat is None or lon is None:
            return
        z = bolge_bul(lat, lon)
        sayac[z]["mesaj"] += 1
        sayac[z]["gemi"].add(mmsi)
        g.update(bolge=z, lat=lat, lon=lon, sog=p.get("Sog"),
                 sinif="B" if tip.startswith("Standard") else "A")
    elif tip == "ShipStaticData":
        s = msg.get("ShipStaticData") or {}
        g["tip"] = s.get("Type")


async def dinle():
    sayac, gemiler = yeni_sayac(), {}
    hata, toplam = None, 0
    bitis = time.time() + SURE
    while time.time() < bitis and hata is None:
        try:
            async with websockets.connect(AIS_URL) as ws:
                await ws.send(json.dumps({
                    "APIKey": API_KEY,
                    "BoundingBoxes": [TEST_KUTUSU],
                    "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport",
                                           "ShipStaticData"],
                }))
                print(f"[TEŞHİS] Bağlandı, {SURE} sn dinleniyor...")
                while True:
                    kalan = bitis - time.time()
                    if kalan <= 0:
                        return sayac, gemiler, toplam, None
                    try:
                        ham = await asyncio.wait_for(ws.recv(), timeout=kalan)
                    except asyncio.TimeoutError:
                        return sayac, gemiler, toplam, None
                    try:
                        m = json.loads(ham)
                    except Exception:
                        continue
                    if "error" in m:
                        return sayac, gemiler, toplam, str(m["error"])
                    toplam += 1
                    isle(m, sayac, gemiler)
        except Exception as e:
            print(f"[TEŞHİS] Bağlantı sorunu: {e}")
            await asyncio.sleep(5)
    return sayac, gemiler, toplam, hata


def rapor_hazirla(sayac, gemiler, toplam, hata):
    simdi = datetime.now(TR).strftime("%d.%m.%Y %H:%M")
    s = [f"AIS TEŞHİS RAPORU - {simdi} (TR)",
         f"Dinleme süresi: {SURE} sn | Alan: enlem 40.85-41.50, boylam 28.40-29.40", ""]
    if hata:
        s += [f"!! AISStream hata döndürdü: {hata}",
              "   (API anahtarı, kota ya da bağlantı sorunu olabilir.)", ""]
    s.append(f"Toplam gelen mesaj: {toplam}")
    s.append("")
    s.append("BÖLGE BAZINDA (mesaj sayısı | farklı gemi sayısı):")
    for ad in [b[0] for b in BOLGELER] + [DISI]:
        v = sayac[ad]
        s.append(f"  {ad}: {v['mesaj']} mesaj | {len(v['gemi'])} gemi")

    s.append("")
    for ad in [BOLGELER[0][0], BOLGELER[1][0]]:
        mm = [k for k in sayac[ad]["gemi"]]
        if mm:
            s.append(f"{ad} - görülen gemiler (ad | MMSI | tip | sınıf | hız):")
            for k in mm[:15]:
                g = gemiler[k]
                s.append(f"  {g['ad']} | {k} | tip {g['tip']} | sınıf {g['sinif']} | {g.get('sog')} kn")
            s.append("")

    liman = len(sayac[BOLGELER[0][0]]["gemi"]) + len(sayac[BOLGELER[1][0]]["gemi"])
    diger = sum(len(sayac[b[0]]["gemi"]) for b in BOLGELER[2:])
    s.append("YORUM:")
    if hata or toplam == 0:
        s.append("  AISStream'den hiç veri gelmedi. Önce API anahtarını ve bağlantıyı kontrol edin;")
        s.append("  kapsama hakkında bu testten sonuç çıkarılamaz.")
    elif liman > 0:
        s.append("  AISStream limanı DUYUYOR. Sorun kapsama değil; zamanlama, gemi tipi ya da")
        s.append("  mantık tarafında. (Testi limanda gemi varken yaptıysanız ajan ayarlarına bakılmalı.)")
    elif diger > 0:
        s.append("  AISStream Boğaz/Marmara/diğer kıyıyı duyuyor ama LİMAN ÇEVRESİNDEN HİÇ")
        s.append("  mesaj gelmiyor. Büyük olasılıkla bu kıyıda AISStream alıcısı yok (kapsama boşluğu).")
        s.append("  NOT: Test limanda gemi yokken yapıldıysa kesin sonuç sayılmaz; gemi varken tekrarlayın.")
    return "\n".join(s)


def mail_gonder(konu, govde):
    if not (SENDER_EMAIL and SENDER_PASSWORD and ADMIN_EMAIL):
        print("[MAIL] E-posta ayarları eksik.")
        return False
    for deneme in range(1, 4):
        try:
            msg = MIMEText(govde, "plain", "utf-8")
            msg["From"], msg["To"], msg["Subject"] = SENDER_EMAIL, ADMIN_EMAIL, konu
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as srv:
                srv.login(SENDER_EMAIL, SENDER_PASSWORD)
                srv.sendmail(SENDER_EMAIL, [a.strip() for a in ADMIN_EMAIL.split(",") if a.strip()],
                             msg.as_string())
            print(f"[MAIL] Gönderildi: {konu}")
            return True
        except Exception as e:
            print(f"[MAIL] Deneme {deneme}/3 başarısız: {e}")
            time.sleep(5)
    return False


def main():
    if not API_KEY:
        print("[HATA] AISSTREAM_API_KEY tanımlı değil.")
    sayac, gemiler, toplam, hata = asyncio.run(dinle())
    rapor = rapor_hazirla(sayac, gemiler, toplam, hata)
    print(rapor)
    mail_gonder("[TEŞHİS] AIS kapsama testi", rapor)


if __name__ == "__main__":
    main()
