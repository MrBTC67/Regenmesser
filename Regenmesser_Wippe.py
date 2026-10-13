import machine
import time
import network
import urequests
import ujson
import ntptime
import esp32
import micropython

# --- REPL-/Thonny-Zugriff ---
# Ctrl+C (ASCII 3) muss auch bei automatisch gestarteten Programmen
# zuverlässig zur MicroPython-Eingabeaufforderung zurückführen.
micropython.kbd_intr(3)
STARTUP_INTERRUPT_SECONDS = 10

def startup_interrupt_window():
    """Kurzes Fenster zum Abbrechen vor dem automatischen Programmstart."""
    print("\nProgrammstart in {} Sekunden - Ctrl+C zum Unterbrechen...".format(
        STARTUP_INTERRUPT_SECONDS))
    deadline = time.ticks_add(time.ticks_ms(), STARTUP_INTERRUPT_SECONDS * 1000)

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        # Der normale MicroPython-Tastaturinterrupt übernimmt Ctrl+C.
        # Die kurze Pause gibt USB/Thonny Zeit, die serielle Verbindung zu öffnen.
        time.sleep_ms(100)

    print("Programmstart.")

startup_interrupt_window()

# --- Hardware & Energie-Konfiguration ---
machine.freq(80000000)

# Konfiguration - PRODUKTIV
FIREBASE_URL = "https://wetterdaten-98f0f-default-rtdb.europe-west1.firebasedatabase.app/Regenmesser"
SSID         = "Starlink1"
PASSWORD     = "astroman"
REGEN_PIN    = 14    
LITER_PRO_WIPP = 0.26 
START_RAIN_L_M2 = 1.0
DRY_SEND_INTERVAL = 7200  # 2 Stunden bei Trockenheit
MONATE = ["Januar", "Februar", "Maerz", "April", "Mai", "Juni",
          "Juli", "August", "September", "Oktober", "November", "Dezember"]

# --- Statistik-Speicher ---
def save_stats(s):
    try:
        with open('stats.json', 'w') as f:
            ujson.dump(s, f)
    except:
        pass

def load_stats():
    defaults = {"tag": 0.0, "monat": START_RAIN_L_M2, "jahr": 0.0, "last_d": 0, "last_m": 0, "last_y": 0}
    try:
        with open('stats.json', 'r') as f:
            data = ujson.load(f)
            if isinstance(data, dict):
                for key, value in defaults.items():
                    if key not in data:
                        data[key] = value
                return data
    except:
        pass
    return defaults

# --- Netzwerk ---
def connect_wifi():
    machine.freq(160000000) 
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Verbinde mit WLAN...")
        wlan.connect(SSID, PASSWORD)
        for _ in range(15):
            if wlan.isconnected(): break
            time.sleep(1)
    if wlan.isconnected():
        try:
            ntptime.settime()
        except:
            pass
        return True
    return False

def arm_rain_wakeup():
    """Wake-up für die Wippe vor jedem Light-Sleep einrichten."""
    try:
        # GPIO-Wakeup ist für Light-Sleep vorgesehen, sofern die Firmware
        # diese Funktion unterstützt.
        esp32.wake_on_gpio((regen_pin,), esp32.WAKEUP_ALL_LOW)
    except AttributeError:
        # Fallback für ältere MicroPython-Versionen ohne wake_on_gpio().
        esp32.wake_on_ext0(regen_pin, esp32.WAKEUP_ALL_LOW)

def get_now():
    t = time.time()
    tm = time.localtime(t)
    is_dst = 1 if (3 < tm[1] < 10 or (tm[1] == 3 and tm[2] >= 25) or (tm[1] == 10 and tm[2] < 25)) else 0
    return time.localtime(t + (3600 * (1 + is_dst)))

def format_ts(lt):
    return "{:02d}.{:02d}.{:04d} {:02d}:{:02d}:{:02d}".format(lt[2], lt[1], lt[0], lt[3], lt[4], lt[5])

def parse_float(value):
    try:
        return float(value)
    except:
        return 0.0

def parse_timestamp(ts):
    try:
        parts = ts.replace('.', ' ').replace(':', ' ').split()
        return (int(parts[2]), int(parts[1]), int(parts[0]), int(parts[3]), int(parts[4]), int(parts[5]))
    except:
        return None

def needs_restore(s):
    return s.get("last_d", 0) == 0 and s.get("last_m", 0) == 0 and s.get("last_y", 0) == 0

def restore_stats_from_firebase(current_lt):
    url = "{}/{}/{}.json".format(FIREBASE_URL, current_lt[0], MONATE[current_lt[1] - 1])
    try:
        r = urequests.get(url, timeout=10)
        data = r.json()
        r.close()
    except Exception as e:
        print("Restore-Fehler:", e)
        return False

    if not isinstance(data, dict) or not data:
        return False

    best = None
    for item in data.values():
        if not isinstance(item, dict):
            continue

        zeitstempel = item.get("zeitstempel")
        parsed_ts = parse_timestamp(zeitstempel) if zeitstempel else None
        if parsed_ts is None:
            continue

        kandidat = {
            "tag": parse_float(item.get("wippe_tag")),
            "monat": parse_float(item.get("wippe_monat")),
            "jahr": parse_float(item.get("wippe_jahr")),
            "ts": parsed_ts
        }

        if best is None:
            best = kandidat
            continue

        if kandidat["monat"] > best["monat"]:
            best = kandidat
        elif kandidat["monat"] == best["monat"] and kandidat["jahr"] > best["jahr"]:
            best = kandidat
        elif kandidat["monat"] == best["monat"] and kandidat["jahr"] == best["jahr"] and kandidat["ts"] > best["ts"]:
            best = kandidat

    if best is None:
        return False

    stats["monat"] = best["monat"]
    stats["jahr"] = best["jahr"]
    if best["ts"][0] == current_lt[0] and best["ts"][1] == current_lt[1] and best["ts"][2] == current_lt[2]:
        stats["tag"] = best["tag"]
    else:
        stats["tag"] = 0.0

    stats["last_d"] = current_lt[2]
    stats["last_m"] = current_lt[1]
    stats["last_y"] = current_lt[0]
    save_stats(stats)
    print("Stats aus Firebase wiederhergestellt.")
    return True

def maybe_restore_on_send(current_lt):
    # Restore immer versuchen, solange restore_pending aktiv ist.
    # Falls seit Boot schon Wippen erfasst wurden, diese Delta-Werte nach dem Restore addieren.
    local_tag_delta = max(0.0, stats.get("tag", 0.0))
    local_monat_delta = max(0.0, stats.get("monat", START_RAIN_L_M2) - START_RAIN_L_M2)
    local_jahr_delta = max(0.0, stats.get("jahr", 0.0))

    if restore_stats_from_firebase(current_lt):
        stats["tag"] += local_tag_delta
        stats["monat"] += local_monat_delta
        stats["jahr"] += local_jahr_delta
        save_stats(stats)
        return True
    return False

# --- Vorbereitung ---
stats = load_stats()
regen_pin = machine.Pin(REGEN_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
arm_rain_wakeup()

print("\n--- START: Regenmesser (Produktiv-Modus) ---")

# Initialer Sync
restore_pending = needs_restore(stats)
if restore_pending:
    aktuelles_lt = get_now()
    # Marker sofort setzen, damit ein Start ohne WLAN die Werte nicht auf 0 zurücksetzt.
    stats["last_d"] = aktuelles_lt[2]
    stats["last_m"] = aktuelles_lt[1]
    stats["last_y"] = aktuelles_lt[0]
    save_stats(stats)

if connect_wifi() and restore_pending:
    if maybe_restore_on_send(get_now()):
        restore_pending = False
network.WLAN(network.STA_IF).active(False)
machine.freq(80000000)

# Erster Versand nach jedem Neustart ist zwingend. Erst nach einem
# erfolgreichen Versand beginnt das reguläre 2-Stunden-Intervall.
first_send_pending = True
last_send_time = 0
regen_aktiv = False
pin_wake_gruende = []
if hasattr(machine, "PIN_WAKE"):
    pin_wake_gruende.append(machine.PIN_WAKE)
if hasattr(machine, "EXT0_WAKE"):
    pin_wake_gruende.append(machine.EXT0_WAKE)

while True:
    lt = get_now()
    now = time.time()
    wake_reason = machine.wake_reason()
    
    # 1. Reset-Logik (Tages-, Monats- und Jahreswechsel)
    reset_notwendig = False

    # Tageswechsel
    if lt[2] != stats["last_d"]:
        stats["tag"] = 0.0
        stats["last_d"] = lt[2]
        reset_notwendig = True

    # Monatswechsel
    if lt[1] != stats["last_m"]:
        stats["monat"] = START_RAIN_L_M2
        stats["last_m"] = lt[1]
        reset_notwendig = True

    # Jahreswechsel
    if lt[0] != stats["last_y"]:
        stats["jahr"] = 0.0
        stats["last_y"] = lt[0]
        reset_notwendig = True

    if reset_notwendig:
        save_stats(stats)

    # 2. Wippe verarbeiten
    # Der tatsächliche Pin-Pegel ist zuverlässiger als die Wakeup-Konstante.
    # Je nach ESP32-Firmware kann Light-Sleep einen anderen wake_reason liefern.
    if regen_pin.value() == 0:
        stats["tag"] += LITER_PRO_WIPP
        stats["monat"] += LITER_PRO_WIPP
        stats["jahr"] += LITER_PRO_WIPP
        regen_aktiv = True
        save_stats(stats)
        print(f"[{format_ts(lt)}] Wippe! Stand: {stats['tag']:.2f} L (Wake: {wake_reason})")
        while regen_pin.value() == 0:
            time.sleep_ms(10)

    # 3. Sende-Logik
    intervall = 60 if regen_aktiv else DRY_SEND_INTERVAL
    
    if first_send_pending or (now - last_send_time) >= intervall:
        print(f"Sende an Firebase (Intervall: {intervall}s)...")
        if connect_wifi():
            if restore_pending:
                if maybe_restore_on_send(lt):
                    restore_pending = False

            url = f"{FIREBASE_URL}/{lt[0]}/{MONATE[lt[1]-1]}.json"
            
            payload = {
                "wippe_tag": round(stats["tag"], 2),
                "wippe_monat": round(stats["monat"], 2),
                "wippe_jahr": round(stats["jahr"], 2),
                "zeitstempel": format_ts(lt),
                "unix_ms": time.time() * 1000
            }
            
            try:
                r = urequests.post(url, data=ujson.dumps(payload), timeout=10)
                r.close()
                last_send_time = time.time()
                first_send_pending = False
                regen_aktiv = False 
                save_stats(stats)
                print("Gesendet.")
            except Exception as e:
                print("Fehler:", e)
            
            network.WLAN(network.STA_IF).active(False)
        machine.freq(80000000)

    # 4. Dynamischer Schlaf
    aktuelle_zeit = time.time()
    verstrichen = aktuelle_zeit - last_send_time
    restzeit = intervall - verstrichen
    
    schlaf_ms = max(1000, int(restzeit * 1000))
    
    print(f"Schlaf für {schlaf_ms/1000:.1f}s...")
    time.sleep_ms(100)
    arm_rain_wakeup()
    machine.lightsleep(schlaf_ms)
