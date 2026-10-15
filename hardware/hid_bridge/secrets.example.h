// Copy to secrets.h (git-ignored) and fill in.
#define WIFI_SSID "your-wifi"
#define WIFI_PASS "your-password"
#define SERVER_HOST "192.168.1.10"  // machine running `python -m hidagent`
#define SERVER_PORT 7300
// To reach the server from any network (e.g. a phone's hotspot) through Tailscale Funnel instead:
// #define SERVER_HOST "your-mac.your-tailnet.ts.net"
// #define SERVER_PORT 443
// #define SERVER_TLS 1
// #define BOARD_TOKEN "same as BOARD_TOKEN in server/.env"
