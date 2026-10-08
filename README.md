# RayHat — ThreadX tren ve sinyalizasyon simülasyonu

Eğitim ve yazılım prototipleme amaçlı bir kapalı çevrim demosu. Beş vagonlu tek tren, sinyal/blok, hız profili, istasyon durağı, kapılar ve fren kontrollerini gösterir. Tk sahnesi yaklaşık 30+ FPS hedefler; üst güzergâh şeridinde tren, önceki durak/başlangıç ve sonraki dört durak sembolik olarak gösterilir. Hızlanma/yavaşlama göstergesi filtrelenmiş ivme ve histerezis kullanır. **Gerçek Marmaray, tren, saha ekipmanı veya demiryolu sinyalizasyon ağına bağlanmaz; emniyet sertifikalı değildir ve gerçek işletmede kullanılmamalıdır.**

## Paket

- `sample_threadx.c` — Eclipse ThreadX thread, mutex, komut ve sonuç kuyrukları kullanan C simülatörü.

- `train_gui.py` — Tkinter komuta merkezi; dahili Python demo ya da ThreadX Unix IPC backend’i.

- `Makefile` — Eclipse ThreadX Linux/GNU host portuna karşı derleme.

## Eclipse ThreadX kullanım akışı

Eclipse’in resmî kılavuzundaki temel akış:

1. Resmî [Eclipse ThreadX deposunu](https://github.com/eclipse-threadx/threadx) alın ve hedef işlemci/toolchain için uygun portu seçin.

1. API kullanan kaynaklara `tx_api.h` ekleyin ve uygulamayı ThreadX çalışma zamanı ile bağlayın.

1. `main()` içinden `tx_kernel_enter()` çağırın; bu çağrı kernel'e geçiş yapar ve normalde geri dönmez.

1. İlk thread, queue, mutex gibi nesneleri `tx_application_define()` içinde oluşturun.

1. Thread’leri `tx_thread_create`, periyodik beklemeyi `tx_thread_sleep`, paylaşılan durum korumasını `tx_mutex_*`, thread’ler arası mesajlaşmayı `tx_queue_*` ile yönetin. ThreadX'te küçük öncelik numarası daha yüksek önceliktir.

Bu C örneğinde IPC thread’i operatör komutlarını doğrular, ThreadX kuyruğuna iletir ve kontrol thread’inden uygulandı/reddedildi sonucunu bekler. Kontrol thread’i tren dinamiğini 100 ms civarında günceller. Zamanlama `SIM_CONTROL_PERIOD=10` tick'tir ve varsayılan 100 tick/saniye ayarına göre belirlenmiştir; portunuzun timer frekansına göre ayarlayın.

## Gerçek yerel IPC: AF_UNIX

GUI ile C simülatörü **TCP/UDP ağ soketi değil**, Linux Unix domain datagram soketi (`AF_UNIX`, `SOCK_DGRAM`) üzerinden haberleşir. Sunucu yolu:

```
/tmp/rayhat_threadx.sock
```

Bu bağlantı yalnızca aynı hostta çalışan süreçler içindir. GUI, ThreadX kaynağı seçildiğinde dahili Python modelini duraklatır. Bağlantı yoksa hız göstergesi boş/çevrimdışı görünür; Python simülasyonuna sessiz fallback yapılmaz. Komutlar yanıt/onay alınmadan “uygulandı” sayılmaz; C tarafındaki kural reddi GUI günlüğünde gerekçesiyle görünür.

GUI çizim döngüsü 16 ms aralıkla çalışır, IPC durum örneklemesi 50 ms aralıkla yapılır ve aradaki karelerde tren konumu kısa süreli hız kestirimiyle yumuşatılır. C simülatörünün fizik adımı 100 ms’dir.

C programı çöktükten sonra socket dosyası kalır ve yeniden başlatma `Address already in use` ile başarısız olursa, **başka bir simülatör çalışmadığını doğruladıktan sonra** stale dosyayı silin:

```bash
rm /tmp/rayhat_threadx.sock
```

## GUI’yi çalıştırma

```bash
python3 train_gui.py
```

Ubuntu’da Tkinter eksikse `python3-tk` sistem paketi gerekir. İlk seçim dahili demo modudur. ThreadX backend’i için önce aşağıdaki C uygulaması başlatılır, sonra arayüzden **ThreadX yerel IPC** seçilir.

## C uygulamasını derleme — Linux/GNU host portu

Resmî ThreadX deposunda Linux/GNU portu `ports/linux/gnu` altındadır. Proje dizininin üst klasörüne klonlayın veya `THREADX_DIR` ile konumunu belirtin:

```bash
git clone https://github.com/eclipse-threadx/threadx ../threadx
make THREADX_DIR=../threadx
./build/train_threadx
```

Derleme GCC ve `make` kullanır; Makefile upstream Linux/GNU portunun 64-bit (`ARCH64=1` ) `tx.a` kütüphanesini oluşturur. Linux portu POSIX pthread ve simüle edilmiş timer kullanır. Upstream yönergesine göre gerçek-zamanlı scheduling bazı sistemlerde ayrıcalık isteyebilir. Yetki hatasında önce yerel scheduling/`rtprio` ayarlarını inceleyin; uygulamayı gelişigüzel yönetici olarak çalıştırmayın.

### ThreadX ağ yığını notu

`AF_UNIX` desteği bu örnekte **Linux/GNU host portuna** aittir. ThreadX’in kendisi gömülü MCU’da Linux Unix socket sağlamaz. MCU veya ürün ağ iletişimi için hedefe uygun Eclipse ThreadX bileşeni (ör. [NetX Duo](https://github.com/eclipse-threadx/netxduo)) ve sürücülerle ayrı entegrasyon gerekir. Bu örneği olduğu gibi gerçek araca/MCU’ya taşımayın.

## Kontroller

- **Hedef hız:** 0–120 km/sa, Uygula ile onaylanır.

- **Hat limiti:** 0–120 km/sa.

- **İstasyon limiti:** istasyon bölgesinde geçerli hız sınırı.

- **Otomatik/manuel sürüş**, simülasyon sinyali (kırmızı/sarı/yeşil) ve blok dolu/boş.

- **İstasyonda dur:** bir sonraki durağa yaklaşma eğrisiyle servis freni uygular; durunca tutma freni devreye girer.

- **Duruşu iptal et**, kapıları aç/kapat, sonraki istasyona kalkış.

- **Manuel servis/durak freni**, **tutma freni**, **acil fren** ve reset.

- Uygulanması mümkün olmayan komut (ör. hareket halindeyken kapı açma/kalkış ya da durmamış trende acil fren reseti) reddedilir ve olay günlüğüne neden yazılır.

- **Simülasyonu sıfırla**.

Duruş profili demo amaçlı yaklaşık fren eğrisi kullanır. Gerçek ATP/CBTC, interlocking, fail-safe state, frenleme eğrileri, odometri, haberleşme güvenliği, zamanlama veya sertifikasyonun yerine geçmez.

## Yerel IPC protokolü v2

Komutlar `AF_UNIX/SOCK_DGRAM` yoluyla gönderilir. Yanıt biçimi:

```
STATE,<result>,<15 status alanı>
ACK,<result>,<15 status alanı>
ERR,<result>,<15 status alanı>
```

Sıralı durum alanları: `speed_dkph, limit_dkph, target_dkph, emergency, mode, signal, block, station_limit_dkph, position_cm, stop_requested, doors_open, service_brake, hold_brake, at_station, station_number`. `dkph`, 0.1 km/sa birimidir; ör. `800 = 80.0 km/sa`. Mesafe santimetredir. `GET` durum sorgular.

Komut örnekleri:

```
GET
TARGET 450
LIMIT 800
STATION LIMIT 300
MODE AUTO                 # veya MODE MANUAL
SIGNAL RED                # YELLOW / GREEN
BLOCK OCCUPIED            # veya BLOCK CLEAR
STATION STOP              # veya STATION CANCEL
DOORS OPEN                # veya DOORS CLOSE
DEPART
BRAKE SERVICE ON          # veya OFF
BRAKE HOLD ON             # veya OFF
BRAKE EMERGENCY ON        # veya OFF
RESET
```

## Kaynaklar

- [Eclipse ThreadX resmî deposu](https://github.com/eclipse-threadx/threadx)

- [Installation and Use, Chapter 2](https://github.com/eclipse-threadx/rtos-docs/blob/main/rtos-docs/threadx/chapter2.md)

- [ThreadX API / Application Components, Chapter 3](https://github.com/eclipse-threadx/rtos-docs/blob/main/rtos-docs/threadx/chapter3.md)

- [Linux/GNU port yönergesi](https://github.com/eclipse-threadx/threadx/blob/master/ports/linux/gnu/readme_threadx.txt)

- [NetX Duo deposu](https://github.com/eclipse-threadx/netxduo)
