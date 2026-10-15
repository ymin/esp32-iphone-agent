// HID bridge: an ESP32-S3 that shows up as a mouse + keyboard, over USB or Bluetooth LE, and takes
// line commands over TCP (Wi-Fi). It holds no logic; the server decides.
//
// Protocol (one command per line, the board answers "ok" or "err <why>"):
//   ping | at <x> <y> | stroke <hold ms> <x> <y> [<x> <y> <ms>]... | home | move <dx> <dy>
//   press | release | click | scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//
// Input goes to a phone connected over Bluetooth when there is one, otherwise over USB. The Bluetooth pointer
// is absolute: "at" puts it on a screen spot (0..32767 across each axis), with no calibration. "stroke" is a
// whole drag: press at the first spot, glide in straight lines through the rest (each over its <ms>), rest
// <hold ms>, release. The USB pointer is relative: "home" and "move" steer it ("at" and "stroke" are refused).

#include <WiFi.h>
#include <NetworkClientSecure.h>
#include <esp_task_wdt.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "tusb.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASS, SERVER_HOST, SERVER_PORT; optional SERVER_TLS, BOARD_TOKEN
#ifndef SERVER_TLS
#define SERVER_TLS 0  // 1: the server is reached over TLS, e.g. through Tailscale Funnel from any network
#endif
#ifndef BOARD_TOKEN
#define BOARD_TOKEN ""  // sent in the greeting, so a server reachable from the internet knows it is this board
#endif

const int STEP = 10;      // max pointer delta per report while moving
const int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable
const int BLE_STEP_MS = 15;  // one report per Bluetooth connection event (iOS grants HID devices 15 ms)
const int HOME_REPORTS = 50;
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0  // 1: echo every command over USB serial (needs CDCOnBoot=cdc)
#endif

const unsigned long SILENCE_MS = 15000;  // the server pings every 2 s: this long without a line means the link is dead
const int WATCHDOG_MS = 8000;  // a hung loop (e.g. blocked USB) reboots the board instead of staying offline
const uint8_t BUTTON_LEFT = 1;
const uint8_t SHIFT_BIT = 0x80;  // set in KeyboardLayout_en_US entries that need shift
const uint8_t MOD_CTRL = 0x01, MOD_SHIFT = 0x02, MOD_ALT = 0x04, MOD_GUI = 0x08;

USBHIDMouse mouse;
USBHIDKeyboard keyboard;
NetworkClient plainLink;
NetworkClientSecure tlsLink;
NetworkClient &server = SERVER_TLS ? tlsLink : plainLink;

// Let's Encrypt roots (ISRG Root X1, RSA, and X2, ECDSA): Tailscale's ts.net certificates chain to one of them.
const char LETS_ENCRYPT_ROOTS[] =
  "-----BEGIN CERTIFICATE-----\n"
  "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\n"
  "-----END CERTIFICATE-----\n"
  "-----BEGIN CERTIFICATE-----\n"
  "MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw\n"
  "CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg\n"
  "R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00\n"
  "MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT\n"
  "ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw\n"
  "EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW\n"
  "+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9\n"
  "ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T\n"
  "AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI\n"
  "zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW\n"
  "tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1\n"
  "/q4AaOeMSQ+2b1tbFfLn\n"
  "-----END CERTIFICATE-----\n";
NimBLECharacteristic *bleMouse, *bleKeys;
volatile bool bleConnected = false;  // an encrypted link: iOS ignores HID reports before that
int lostReports = 0;  // reports the Bluetooth stack refused during the current command

// Status on the RGB LED (the red one next to it is the power LED, out of the firmware's reach). Color and pace
// together say what is going on: a blinking color names what is missing, a short burst marks a link coming up,
// solid white means a command is running, and off means all is well.
const int LED_PIN = 48;  // ESP32-S3-DevKitC-1 v1.0 and most clones; v1.1 boards use 38
struct Color { uint8_t r, g, b; };
struct Pattern { Color color; int ms; int times; };  // on and off for `ms` each; `times` flashes (0: until fixed)
const Color OFF = {0, 0, 0};
const Color WORKING = {24, 24, 24};                  // white, solid while a command runs
const Pattern WIFI_DOWN = {{40, 12, 0}, 150, 0};     // orange, fast blink
const Pattern SERVER_DOWN = {{24, 0, 32}, 500, 0};   // purple, medium blink: Wi-Fi is up, the server is not reached
const Pattern BLE_DOWN = {{0, 0, 40}, 1000, 0};      // blue, slow blink
const Pattern WIFI_UP = {{0, 40, 0}, 300, 2};        // green, 2 long flashes
const Pattern BLE_UP = {{0, 30, 30}, 100, 4};        // cyan, 4 quick flashes
Color ledShown = {1, 1, 1};
Pattern burst;
unsigned long burstStart = 0;

void showColor(Color c) {
  if (c.r == ledShown.r && c.g == ledShown.g && c.b == ledShown.b) return;
  rgbLedWrite(LED_PIN, c.r, c.g, c.b);
  ledShown = c;
}

void flash(Pattern p) {
  burst = p;
  burstStart = millis();
}

// On for the first half of each period of the pattern
bool lit(const Pattern &p, unsigned long elapsed) { return elapsed / p.ms % 2 == 0; }

void updateLed() {
  static bool wifiWas = false, bleWas = false;
  bool wifi = WiFi.status() == WL_CONNECTED, ble = bleConnected;
  if (wifi && !wifiWas) flash(WIFI_UP);
  if (ble && !bleWas) flash(BLE_UP);
  wifiWas = wifi;
  bleWas = ble;
  unsigned long now = millis();
  if (burstStart && now - burstStart < 2UL * burst.times * burst.ms) {
    showColor(lit(burst, now - burstStart) ? burst.color : OFF);
    return;
  }
  burstStart = 0;
  const Pattern *problem = !wifi ? &WIFI_DOWN : !server.connected() ? &SERVER_DOWN : !ble ? &BLE_DOWN : nullptr;
  showColor(problem && lit(*problem, now) ? problem->color : OFF);
}
uint8_t buttons = 0;
uint16_t bleX = 0, bleY = 0;  // where the absolute Bluetooth pointer is

// One Bluetooth LE HID device with an absolute mouse (report 1: buttons, x and y as 0..32767 little-endian,
// wheel, pan) and a boot-style keyboard (report 2: modifiers, reserved, 6 keys; plus the LED output report
// hosts expect).
const uint8_t BLE_REPORT_MAP[] = {
  0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x01, 0xA1, 0x00,
  0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x81, 0x03,
  0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
  0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
  0x05, 0x0C, 0x0A, 0x38, 0x02, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
  0xC0, 0xC0,
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x02,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
  0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
  0xC0,
};

class BleLink : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *, NimBLEConnInfo &info) override { NimBLEDevice::startSecurity(info.getConnHandle()); }
  void onAuthenticationComplete(NimBLEConnInfo &info) override {
    bleConnected = info.isEncrypted();
    // iOS often starts at 30 ms; one report per 15 ms event keeps the pointer pace as steady as over USB
    NimBLEDevice::getServer()->updateConnParams(info.getConnHandle(), 12, 12, 0, 400);
#if DEBUG_SERIAL
    Serial.printf("bluetooth %s, interval %.2f ms\n", bleConnected ? "paired" : "pairing failed", info.getConnInterval() * 1.25);
#endif
  }
#if DEBUG_SERIAL
  void onConnParamsUpdate(NimBLEConnInfo &info) override {
    Serial.printf("bluetooth interval %.2f ms\n", info.getConnInterval() * 1.25);
  }
#endif
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &, int reason) override {
    bleConnected = false;
    NimBLEDevice::startAdvertising();  // so the phone reconnects by itself
#if DEBUG_SERIAL
    Serial.printf("bluetooth disconnected (%d)\n", reason);
#endif
  }
};

void setupBle() {
  NimBLEDevice::init("HID Bridge");
  NimBLEDevice::setSecurityAuth(true, false, true);  // "Just Works" pairing, bonded so the phone reconnects
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  NimBLEServer *ble = NimBLEDevice::createServer();
  ble->setCallbacks(new BleLink());
  NimBLEHIDDevice *hid = new NimBLEHIDDevice(ble);
  hid->setManufacturer("Espressif");
  hid->setPnp(0x02, 0x303A, 0x4002, 0x0100);
  hid->setHidInfo(0x00, 0x01);
  hid->setReportMap((uint8_t *)BLE_REPORT_MAP, sizeof(BLE_REPORT_MAP));
  bleMouse = hid->getInputReport(1);
  bleKeys = hid->getInputReport(2);
  hid->getOutputReport(2);
  hid->setBatteryLevel(100);
  ble->start();
  NimBLEAdvertising *advertising = NimBLEDevice::getAdvertising();
  advertising->setName("HID Bridge");
  advertising->setAppearance(0x03C1);  // keyboard: iOS Settings does not list a "generic HID" appearance
  advertising->addServiceUUID(hid->getHidService()->getUUID());
  advertising->enableScanResponse(true);
  advertising->start();
}

// A refused report is lost movement, so retry while the stack is short of buffers, and count what still fails.
// After one report failed the link is stuck; the rest of the command fails fast so the watchdog never fires.
void bleSend(NimBLECharacteristic *report, const uint8_t *data, size_t length) {
  for (int i = 0; i < (lostReports ? 1 : 20); i++) {
    if (report->notify(data, length)) return;
    delay(5);
  }
  lostReports++;
}

void sendBleMouse(int8_t wheel = 0, int8_t pan = 0) {
  uint8_t report[] = {buttons, (uint8_t)bleX, (uint8_t)(bleX >> 8), (uint8_t)bleY, (uint8_t)(bleY >> 8),
                      (uint8_t)wheel, (uint8_t)pan};
  bleSend(bleMouse, report, sizeof(report));
}

void sendMouse(int8_t x, int8_t y, int8_t wheel = 0, int8_t pan = 0) {
  if (bleConnected) {
    sendBleMouse(wheel, pan);  // the absolute pointer stays put; only the wheel moves
  } else {
    mouse.move(x, y, wheel, pan);
  }
}

void setButtons(uint8_t b) {
  buttons = b;
  if (bleConnected) {
    sendMouse(0, 0);
  } else if (b) {
    mouse.press(MOUSE_LEFT);
  } else {
    mouse.release(MOUSE_LEFT);
  }
}

void sendKeys(uint8_t modifiers, const uint8_t *keys, int count) {
  KeyReport report = {modifiers, 0, {0}};
  if (count) memcpy(report.keys, keys, min(count, 6));
  if (bleConnected) {
    bleSend(bleKeys, (uint8_t *)&report, sizeof(report));
  } else {
    keyboard.sendReport(&report);
  }
}

// A gesture cut off mid-way must not leave the button held (long press). Keys never stay down: every
// command releases them before it answers.
void releaseButton() {
  if (!buttons) return;
  buttons = 0;
  if (bleConnected) sendMouse(0, 0);
  if (tud_mounted() && !tud_suspended()) mouse.release(MOUSE_LEFT);
}

void tapKey(uint8_t modifiers, const uint8_t *keys, int count) {
  int pace = bleConnected ? BLE_STEP_MS : STEP_MS;
  sendKeys(modifiers, keys, count);
  delay(pace);
  sendKeys(0, nullptr, 0);
  delay(pace);
}

// A straight line at one speed: iOS accelerates by pointer speed, so a move that went diagonal and then along
// one axis (slower) would cover a different distance than the diagonal moves calibration measures.
void movePaced(long dx, long dy) {
  int pace = STEP_MS, step = STEP;
  long reports = lround(ceil(hypot(dx, dy) / step)), sentX = 0, sentY = 0;
  for (long i = 1; i <= reports; i++) {
    long x = lround((double)dx * i / reports), y = lround((double)dy * i / reports);
    sendMouse(x - sentX, y - sentY);
    sentX = x;
    sentY = y;
    delay(pace);
  }
}

// The absolute Bluetooth pointer, in a straight line over `ms` (at once if 0): one report per connection event.
void glideTo(long x, long y, long ms) {
  long fromX = bleX, fromY = bleY, reports = max(1L, ms / BLE_STEP_MS);
  x = constrain(x, 0, 32767);
  y = constrain(y, 0, 32767);
  for (long i = 1; i <= reports; i++) {
    bleX = fromX + (x - fromX) * i / reports;
    bleY = fromY + (y - fromY) * i / reports;
    sendBleMouse();
    delay(BLE_STEP_MS);
  }
}

// One command for the whole drag, so network delays never fall between press and release (a long press).
String stroke(const String &arg) {
  if (!bleConnected) return "err absolute pointer needs Bluetooth";
  long v[3 + 3 * 32];
  int n = 0;
  const char *p = arg.c_str();
  for (char *end; n < (int)(sizeof(v) / sizeof(v[0])); p = end) {
    v[n] = strtol(p, &end, 10);
    if (end == p) break;
    n++;
  }
  if (n < 6 || (n - 3) % 3) return "err stroke needs <hold ms> <x> <y> and at least one <x> <y> <ms>";
  glideTo(v[1], v[2], 0);
  delay(30);  // iOS must see the pointer arrive before the press, or it draws a line from where it was
  setButtons(BUTTON_LEFT);
  delay(40);  // and the press settle before moving, or the start of the stroke is lost
  for (int i = 3; i < n; i += 3) {
    glideTo(v[i], v[i + 1], v[i + 2]);
    esp_task_wdt_reset();
  }
  delay(v[0]);
  setButtons(0);
  return "ok";
}

// One key name or character as a HID usage, plus the modifier it needs; 0 when unknown.
uint8_t keyUsage(const String &k, uint8_t &modifiers) {
  if (k == "enter") return 0x28;
  if (k == "esc") return 0x29;
  if (k == "backspace") return 0x2A;
  if (k == "tab") return 0x2B;
  if (k == "space") return 0x2C;
  if (k == "right") return 0x4F;
  if (k == "left") return 0x50;
  if (k == "down") return 0x51;
  if (k == "up") return 0x52;
  if (k.length() != 1 || (uint8_t)k[0] >= 128) return 0;
  uint8_t code = KeyboardLayout_en_US[(uint8_t)k[0]];
  if (code & SHIFT_BIT) modifiers |= MOD_SHIFT;
  return code & ~SHIFT_BIT;
}

bool pressCombo(String combo) {
  uint8_t modifiers = 0, keys[6];
  int count = 0;
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    String k = combo.substring(0, i);
    if (k == "cmd") modifiers |= MOD_GUI;
    else if (k == "ctrl") modifiers |= MOD_CTRL;
    else if (k == "alt") modifiers |= MOD_ALT;
    else if (k == "shift") modifiers |= MOD_SHIFT;
    else {
      uint8_t usage = keyUsage(k, modifiers);
      if (!usage || count == 6) return false;
      keys[count++] = usage;
    }
  }
  // Like fingers on a keyboard: modifiers down first, then the keys, then let go in reverse. iOS ignores shortcuts
  // whose modifier arrives in the same report as the key.
  int pace = bleConnected ? BLE_STEP_MS : STEP_MS;
  if (modifiers && count) {
    sendKeys(modifiers, nullptr, 0);
    delay(pace);
  }
  sendKeys(modifiers, keys, count);
  delay(30);
  if (modifiers && count) {
    sendKeys(modifiers, nullptr, 0);
    delay(pace);
  }
  sendKeys(0, nullptr, 0);
  return true;
}

void typeText(const String &text) {
  for (unsigned i = 0; i < text.length(); i++) {
    uint8_t modifiers = 0, usage = keyUsage(String(text[i]), modifiers);
    if (usage) tapKey(modifiers, &usage, 1);
  }
}

// A locked or sleeping phone suspends USB; sending HID reports then can stall. Ask it to wake up instead,
// and report "asleep" right away if it does not, so the board never hangs.
bool usbReady() {
  if (tud_suspended()) {
    tud_remote_wakeup();
    for (int i = 0; i < 50 && tud_suspended(); i++) delay(10);
  }
  return tud_mounted() && !tud_suspended();
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  if (!bleConnected && !usbReady()) return "err phone not connected (pair Bluetooth, or plug in USB and unlock the phone)";
  if (cmd == "at") {
    if (!bleConnected) return "err absolute pointer needs Bluetooth";
    glideTo(a, b, 0);
    return "ok";
  }
  if (cmd == "stroke") return stroke(arg);
  if (cmd == "home") {
    if (bleConnected) { glideTo(0, 0, 0); return "ok"; }
    for (int i = 0; i < HOME_REPORTS; i++) {  // slam the pointer into the top-left corner
      sendMouse(-127, -127);
      delay(2);
    }
    return "ok";
  }
  if (cmd == "move") {
    if (bleConnected) glideTo(bleX + a, bleY + b, 0);  // e.g. the server's keep-awake nudge
    else movePaced(a, b);
    return "ok";
  }
  if (cmd == "press") { setButtons(BUTTON_LEFT); return "ok"; }
  if (cmd == "release") { setButtons(0); return "ok"; }
  if (cmd == "click") { setButtons(BUTTON_LEFT); delay(60); setButtons(0); return "ok"; }
  if (cmd == "scroll") { sendMouse(0, 0, a, b); return "ok"; }
  if (cmd == "type") { typeText(arg); return "ok"; }
  if (cmd == "key") return pressCombo(arg) ? "ok" : "err unknown key";
  return "err unknown command";
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

void setup() {
#if DEBUG_SERIAL
  Serial.begin(115200);
#endif
  mouse.begin();
  keyboard.begin();
  USB.productName("HID Bridge");
  USB.begin();
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
  setupBle();
  tlsLink.setCACert(LETS_ENCRYPT_ROOTS);
  tlsLink.setHandshakeTimeout(4);
  WiFi.setHostname("hid-bridge");
  // Wi-Fi keeps modem sleep (no setSleep(false)): it shares the radio with Bluetooth, which requires it.
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  esp_task_wdt_reset();
  updateLed();
#if DEBUG_SERIAL
  static unsigned long statusShown = 0;
  if (WiFi.status() != WL_CONNECTED && millis() - statusShown > 5000) {
    statusShown = millis();
    Serial.printf("wifi %s: status %d\n", WIFI_SSID, WiFi.status());  // 1: no such network, 4: wrong password
  }
#endif
  if (WiFi.status() != WL_CONNECTED) { releaseButton(); delay(50); return; }  // auto-reconnects
#if DEBUG_SERIAL
  static IPAddress shown;
  if (WiFi.localIP() != shown) Serial.printf("wifi %s as %s\n", WIFI_SSID, (shown = WiFi.localIP()).toString().c_str());
#endif
  // A link the server dropped can still look connected (its close lost on the way, e.g. over a phone's hotspot)
  static unsigned long heard = 0;
  if (server.connected() && millis() - heard > SILENCE_MS) server.stop();
  if (!server.connected()) {
    releaseButton();
    // retry fast to show up at once; over the internet DNS, TCP and the TLS handshake (4 s at most) need longer
    if (!server.connect(SERVER_HOST, SERVER_PORT, SERVER_TLS ? 2500 : 1000)) { delay(200); return; }
    server.setNoDelay(true);
    // why the board last restarted, uptime, and the token
    server.printf("hello %s %lu%s%s\n", resetReason(), millis(), *BOARD_TOKEN ? " " : "", BOARD_TOKEN);
    heard = millis();
#if DEBUG_SERIAL
    Serial.printf("connected to %s:%d as %s after %lu ms\n", SERVER_HOST, SERVER_PORT,
                  WiFi.localIP().toString().c_str(), millis());
#endif
  }
  if (server.available()) {
    String line = server.readStringUntil('\n');
    line.trim();
    if (!line.length()) return;
    unsigned long started = millis();
    lostReports = 0;
    if (line != "ping") showColor(WORKING);
    String reply = run(line);
    updateLed();
    if (lostReports && reply == "ok") reply = "err bluetooth dropped " + String(lostReports) + " reports";
    server.print(reply + "\n");
    heard = millis();
#if DEBUG_SERIAL
    Serial.printf("%s -> %s (%lu ms)\n", line.c_str(), reply.c_str(), millis() - started);
#endif
  }
}
