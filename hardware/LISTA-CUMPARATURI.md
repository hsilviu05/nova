# Listă de cumpărături — NOVA (prototip V1)

Tot ce trebuie ca să construiești un NOVA funcțional, cu variante ieftine și
scumpe pentru fiecare piesă.

> ### ⚠️ Despre prețuri — citește asta întâi
>
> **Prețurile de mai jos sunt estimări, nu cotații.** Nu am putut verifica
> prețurile live: mediul în care lucrez are acces la internet filtrat, iar
> `optimusdigital.ro`, `waveshare.com` și `emag.ro` sunt blocate de proxy.
>
> Singurul preț pe care l-am putut confirma dintr-o sursă directă este cel de
> pe site-ul oficial Waveshare: **27,99 – 32,99 USD** pentru placa principală
> ([waveshare.com](https://www.waveshare.com/esp32-s3-touch-amoled-2.06.htm)).
> Acesta e important, vezi mai jos.
>
> **Verifică fiecare preț înainte să comanzi.** Restul cifrelor sunt
> estimări din experiență pentru magazinele româneşti de electronică, la
> septembrie 2026, și pot fi greșite cu ±40%.

---

## 1. Placa principală — unde o cumperi contează enorm

Aceeași placă, trei prețuri foarte diferite:

| Sursă | Preț estimat | Livrare | Observații |
|---|---|---|---|
| **waveshare.com direct** | **~29 USD ≈ 135 RON** + transport ~15–25 USD | 2–4 săptămâni | **Confirmat pe site-ul oficial.** Total ~215–250 RON. Posibile taxe vamale. |
| eMAG (revânzător) | ~350 RON | 1–3 zile | Prețul văzut anterior pentru varianta 1.64". Verifică pentru 2.06". |
| AliExpress (magazin oficial Waveshare) | ~140–180 RON | 2–5 săptămâni | Caută magazinul oficial, nu clone. |

> **Recomandarea mea:** comandă **direct de la Waveshare** dacă nu te
> grăbești. Diferența față de eMAG e de ~100–150 RON, adică aproape jumătate
> din restul componentelor la un loc.
>
> **Ia varianta „Without Battery".** V1 merge pe cablu. Bateria vine după ce
> mecanica e stabilă.

**Ce include placa** (nu mai cumperi separat): ecran AMOLED 410×502, touch,
**două microfoane digitale**, difuzor, codec audio ES8311, IMU pe 6 axe, RTC,
și circuit de încărcare LiPo.

---

## 2. Componente obligatorii

Comandă-le din România — sunt ieftine, iar transportul din China nu merită
pentru ele.

| # | Componentă | Buc. | Preț estimat | De ce e necesară |
|---|---|---|---|---|
| 1 | **Modul PCA9685** (16 canale PWM, I²C) | 1 | ~25–35 RON | **Obligatoriu.** Placa Waveshare nu are pini PWM liberi pentru servomotoare. Vezi ADR 008. |
| 2 | **Servomotor SG90** | 3 | ~15 RON/buc = ~45 RON | Două pentru cap (stânga-dreapta + sus-jos). Al treilea e rezervă — la SG90 se rup dinții din plastic. |
| 3 | **Senzor VL53L0X** (ToF, GY-530) | 1 | ~25–40 RON | Detecția prezenței. Ăsta generează evenimentele `person_detected` din care se calculează toate statisticile. |
| 4 | **Cablu USB-C cu date** | 1 | ~20–30 RON | Nu unul doar de încărcare. Toată lumea pierde o jumătate de oră pe asta o dată. |

**Subtotal obligatoriu: ~115–150 RON**

### Magazine româneşti recomandate

- [Optimus Digital](https://www.optimusdigital.ro) — cel mai mare stoc, livrare rapidă
- [Ardushop](https://ardushop.ro) — alternativă bună, uneori mai ieftin
- [Sigma Nortec](https://sigmanortec.ro) — are toate cele trei module

Compară între ele: la modulele mici diferențele sunt de 30–50%.

---

## 3. Alimentare — nu sări peste asta

Servomotoarele **nu** au voie să tragă curent din regulatorul plăcii. Un SG90
trage ~700 mA la blocaj; dacă alimentezi din placă, ESP32-ul se resetează în
mijlocul mișcării. E cea mai frecventă cauză de eșec la proiecte de genul
ăsta.

| # | Componentă | Buc. | Preț estimat | Note |
|---|---|---|---|---|
| 5 | **Sursă 5V / 2A** | 1 | ~25–40 RON | Un încărcător de telefon vechi de 5V 2A merge perfect — **0 RON** dacă ai unul prin casă. |
| 6 | **Modul USB → clemă cu șurub** | 1 | ~8–12 RON | Ca să scoți 5V și GND din încărcător fără să tai cablul. |
| 7 | **Condensator electrolitic 470–1000 µF / 10V** | 1 | ~2 RON | Lângă servomotoare. Absoarbe vârful de curent la pornire. |

**Subtotal alimentare: ~10–55 RON** (10 RON dacă refolosești un încărcător)

---

## 4. Montaj și prototipare

| # | Componentă | Buc. | Preț estimat | Note |
|---|---|---|---|---|
| 8 | **Breadboard 400 puncte** | 1 | ~8–12 RON | Testează totul înainte să lipești. |
| 9 | **Set fire Dupont** (M-T, T-T) | 1 | ~12–18 RON | Pentru PCA9685 și senzorul ToF pe breadboard. |
| 10 | **Fir siliconic 30 AWG** | 1 set | ~20–30 RON | Placa are **pad-uri de lipit**, nu conector. Ai nevoie de fir subțire și flexibil. |
| 11 | **Șuruburi M2 + piulițe** | 1 set | ~15–25 RON | Pentru asamblarea carcasei printate. |

**Subtotal montaj: ~55–85 RON**

---

## 5. Unelte — doar dacă nu le ai deja

| Componentă | Preț estimat | Obligatoriu? |
|---|---|---|
| **Letcon (stație de lipit)** | ~80–200 RON | **Da.** Poziţiile 1, 3 și 10 cer lipire, indiferent ce placă alegi. |
| Fludor + fluxor | ~25–40 RON | Da, dacă iei letcon |
| **Inserturi filetate M2** (heat-set) | ~30–40 RON | Nu. Vezi mai jos. |
| Multimetru | ~50–100 RON | Nu, dar îți economisește ore de depanare |

> **Economie:** inserturile filetate se montează cu letconul și țin mult mai
> bine în PLA. Dar pentru un **prototip**, șuruburile autofiletante direct în
> plastic sunt suficiente. Amână inserturile pentru versiunea finală.

---

## 6. Filament pentru carcasă

Ai imprimantă Bambu Lab, deci ai deja consumabile. Dacă mai trebuie:

| Material | Preț estimat | Recomandare |
|---|---|---|
| **PLA** (1 kg) | ~80–110 RON | **Da, începe cu PLA.** Ușor de printat, rigid, ieftin. |
| PETG (1 kg) | ~100–130 RON | Doar dacă vrei rezistență la căldură. Mai greu de reglat. |

O carcasă completă consumă ~150–250 g. Un kilogram ajunge pentru 4–6
iterații, ceea ce e realist — prima carcasă nu iese niciodată bine.

---

## Total

| Scenariu | Total estimat |
|---|---|
| **Minim** (placă de la Waveshare, ai încărcător și letcon) | **~400–450 RON** |
| **Realist** (placă de la Waveshare, cumperi tot restul) | **~500–600 RON** |
| **Rapid** (placă de pe eMAG, livrare în 3 zile) | **~650–750 RON** |
| **+ letcon**, dacă nu ai | **+80–200 RON** |

---

## Cum reduci costul, concret

1. **Comandă placa direct de la Waveshare** — economie ~100–150 RON. E cea
   mai mare economie de pe listă, de departe.
2. **Refolosește un încărcător de 5V** — economie ~30 RON.
3. **Amână inserturile filetate** — economie ~35 RON. Șuruburi
   autofiletante în plastic sunt OK pentru prototip.
4. **Comandă modulele mici de pe AliExpress** dacă ai răbdare — PCA9685,
   SG90 și VL53L0X costă împreună ~50–60 RON acolo, față de ~115–150 RON în
   România. Economie ~60–90 RON, dar aștepți 3–5 săptămâni.
5. **Nu cumpăra baterie acum.** V1 merge pe cablu, iar bateria complică
   alimentarea servomotoarelor.

### Ce NU recomand să economisești

- **Nu sări peste PCA9685.** Nu e opțional — placa nu are PWM liber.
- **Nu alimenta servomotoarele din placă.** Economisești 35 RON și pierzi
  săptămâni depanând reseturi aleatorii.
- **Nu lua servomotoare fără marcă de pe eMAG.** Diferența față de SG90
  normale e de 5 RON și jumătate din ele vibrează continuu.

### O alternativă mai ieftină pe care am evaluat-o și am respins-o

Placă ESP32-S3 simplă (~40 RON) + ecran rotund GC9A01 (~45 RON) + microfon
I²S INMP441 (~30 RON) + amplificator MAX98357A (~30 RON) + difuzor (~15 RON)
= **~160 RON**, față de ~215–250 RON pentru placa Waveshare.

Economisești ~60 RON și primești: cinci module de conectat în loc de unul,
un ecran mai mic și mai slab, fără touch, fără IMU, fără RTC, fără codec
audio, și un teanc de fire care trebuie să încapă în carcasă.

**Nu merită.** Diferența de preț e mai mică decât un rând de filament.

---

## Ordinea în care cumperi

Nu comanda tot deodată. Placa are cel mai lung timp de livrare, iar restul
depinde de ce revizie de placă primești.

1. **Acum:** placa Waveshare (2–4 săptămâni de la Waveshare).
2. **Acum:** PCA9685, servomotoare, senzor ToF, breadboard, fire Dupont —
   din România, ajung în câteva zile. Poți începe testele cu ele conectate
   la orice ESP32 pe care îl ai deja.
3. **După ce ajunge placa:** fir siliconic, șuruburi. Măsori placa reală
   înainte să comanzi mecanica.

---

## Înainte să lipești ceva

Placa asta are **revizii diferite** cu pinout diferit: `LCD_CS` și `IMU_INT1`
își schimbă locul între GPIO9 și GPIO46. **Identifică revizia plăcii primite**
și verific-o în [wiki-ul Waveshare](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-2.06)
înainte de orice lipire.

Detaliile electrice complete — magistrala I²C, adresele modulelor, pinii
rezervați — sunt în [BOM.md](BOM.md).
