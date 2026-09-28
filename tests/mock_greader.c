/* Mock native GReader.dll untuk menguji cykeo-bridge adapter di Linux.
 *
 * Hanya untuk TEST — bukan SDK Cykeo asli, dan TIDAK pernah menyentuh hardware.
 * Mengimplementasikan subset API yang dipakai bridge, dengan sengaja memakai
 * layout LogBaseEpcInfo yang "salah" dari tebakan lama, supaya test membuktikan
 * parser defensif kita: EPC tetap terekstrak dari frame opaque.
 *
 * Build: gcc -shared -fPIC -o libGReader.so mock_greader.c
 */
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* ---- Layout frame: sengaja dibuat TIDAK struct-asli (uji parser defensif) ---- */
typedef struct {
    char   reserved_head[8];   /* padding agar EPC tidak di offset 0 */
    char   Epc[256];
    char   Tid[64];
    char   Userdata[128];
    char   Reserved[64];
    int32_t AntId;
    int32_t Rssi;
    int32_t Result;
    int32_t Pc;
} MockLogBaseEpcInfo;

typedef struct {
    int      isPrint;
    void   (*cbEpc)(const char*, MockLogBaseEpcInfo*);
    void   (*cbOver)(const char*, MockLogBaseEpcInfo*);
    int      inventoryStarted;
    int      closed;
} MockGClient;

static MockGClient g_client;
static int g_registered_log = 0;
static int g_registered_over = 0;
static int g_stop_count = 0;
static int g_inventory_count = 0;

/* Tag yang akan di-feed lewat mock_feed() */
static char g_pending_epc[256] = {0};

/* Dipanggil test untuk menyuntik EPC berikutnya */
void mock_feed(const char *epc) {
    if (epc) {
        strncpy(g_pending_epc, epc, sizeof(g_pending_epc) - 1);
        g_pending_epc[sizeof(g_pending_epc) - 1] = '\0';
    } else {
        g_pending_epc[0] = '\0';
    }
}

int  mock_stop_count(void)     { return g_stop_count; }
int  mock_inventory_count(void){ return g_inventory_count; }
int  mock_cb_log(void)         { return g_registered_log; }
int  mock_cb_over(void)        { return g_registered_over; }
int  mock_is_print(void)       { return g_client.isPrint; }
void mock_set_print(int v)     { g_client.isPrint = v; }
int  mock_was_closed(void)     { return g_client.closed; }

/* Reset total state mock — dipanggil tiap test supaya tidak saling contamasi. */
void mock_reset(void) {
    memset(&g_client, 0, sizeof(g_client));
    g_stop_count = 0;
    g_inventory_count = 0;
    g_registered_log = 0;
    g_registered_over = 0;
    g_pending_epc[0] = '\0';
}

/* ---- API yang dipakai bridge (nama & arity sesuai guide resmi) ---- */

void* CreateRS232(const char *initParam, const char *readerName,
                  int timeout, int type) {
    (void)initParam; (void)readerName; (void)timeout; (void)type;
    memset(&g_client, 0, sizeof(g_client));
    g_client.closed = 0;
    /* Reset counter global supaya setiap koneksi benar-benar terukur dari nol. */
    g_stop_count = 0;
    g_inventory_count = 0;
    g_registered_log = 0;
    g_registered_over = 0;
    return &g_client;
}

int Close(void *client) {
    if (client) {
        ((MockGClient *)client)->closed = 1;
    }
    return 1;
}

int RegCallBack(void *client, int event, void *fn) {
    (void)client;
    if (event == 0x01) {           /* E_TAG_EPC_LOG */
        g_client.cbEpc = (void (*)(const char*, MockLogBaseEpcInfo*))fn;
        g_registered_log = 1;
    } else if (event == 0x02) {    /* E_TAG_EPC_OVER */
        g_client.cbOver = (void (*)(const char*, MockLogBaseEpcInfo*))fn;
        g_registered_over = 1;
    }
    return 1;
}

int SendSynMsg(void *client, int msg, void *param) {
    (void)param;
    if (msg == 0x10) {             /* EMESS_BASE_STOP */
        g_stop_count++;
    } else if (msg == 0x50) {      /* EMESS_BASE_INVENTORY_EPC */
        g_inventory_count++;
        g_client.inventoryStarted = 1;
    }
    (void)client;
    return 1;
}

/* Simulasikan reader memancarkan satu tag ke callback (dipanggil test). */
int mock_emit_tag(const char *epc, int ant, int rssi) {
    if (!g_client.cbEpc || !epc || !*epc) return 0;
    MockLogBaseEpcInfo info;
    memset(&info, 0, sizeof(info));
    strncpy(info.Epc, epc, sizeof(info.Epc) - 1);
    strncpy(info.Tid, "E2801160C0000000000000AB1", sizeof(info.Tid) - 1);
    info.AntId = ant;
    info.Rssi = rssi;
    info.Result = 0;
    info.Pc = 0xC000;
    g_client.cbEpc("COM12:115200", &info);
    return 1;
}

int mock_emit_over(void) {
    if (!g_client.cbOver) return 0;
    MockLogBaseEpcInfo info;
    memset(&info, 0, sizeof(info));
    g_client.cbOver("COM12:115200", &info);
    return 1;
}
