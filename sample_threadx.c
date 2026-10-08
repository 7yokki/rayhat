/*
 * sample_threadx.c — ThreadX üzerinde çalışan, eğitim amaçlı tren simülasyonu.
 * Linux/GNU host portu için Unix domain datagram socket ile yerel IPC kullanır.
 * Gerçek tren/hat donanımına bağlanmaz, emniyet sertifikalı değildir.
 */
#include "tx_api.h"
#include <errno.h>
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#define SIM_SOCKET_PATH       "/tmp/rayhat_threadx.sock"
#define SIM_MAX_DKPH          1200UL /* 120.0 km/sa, hızlar 0.1 km/sa biriminde */
#define SIM_YELLOW_DKPH        400UL
#define SIM_STATION_DKPH       300UL
#define SIM_STATION_ZONE_CM  300000UL /* sonraki istasyona 3 km kala */
#define SIM_STATION_START_CM 120000UL /* ilk istasyon 1.2 km'de */
#define SIM_STATION_GAP_CM   150000UL
#define SIM_CONTROL_PERIOD       10UL /* varsayılan 100 tick/s ise 100 ms */
#define SIM_QUEUE_DEPTH          16UL
#define SIM_CONTROL_STACK      4096UL
#define SIM_IPC_STACK          4096UL
#define COMMAND_WORDS             3UL
#define STATE_FIELDS             15UL

typedef enum {
    CMD_LIMIT = 1, CMD_STATION_LIMIT, CMD_TARGET, CMD_MODE,
    CMD_EMERGENCY, CMD_SERVICE_BRAKE, CMD_HOLD_BRAKE,
    CMD_SIGNAL, CMD_BLOCK, CMD_STOP_REQUEST, CMD_STOP_CANCEL,
    CMD_DOORS_OPEN, CMD_DOORS_CLOSE, CMD_DEPART, CMD_RESET
} CommandType;
typedef enum { MODE_AUTO = 0, MODE_MANUAL = 1 } TrainMode;
typedef enum { ASPECT_RED = 0, ASPECT_YELLOW = 1, ASPECT_GREEN = 2 } SignalAspect;
typedef enum { BLOCK_CLEAR = 0, BLOCK_OCCUPIED = 1 } BlockState;
typedef enum {
    RESULT_OK = 0, ERR_RANGE = 1, ERR_BRAKE_RESET = 2,
    ERR_NOT_AT_STATION = 3, ERR_TRAIN_MOVING = 4,
    ERR_DOORS_OPEN = 5, ERR_DOORS_CLOSED = 6,
    ERR_SIGNAL_BLOCKED = 7, ERR_ALREADY_STOPPED = 8,
    ERR_STOP_ACTIVE = 9, ERR_NO_STOP_REQUEST = 10,
    ERR_STATION_PASSED = 11, ERR_BRAKE_ACTIVE = 12
} ResultCode;

typedef struct { ULONG id, type, value; } SimCommand;
typedef struct {
    ULONG speed_dkph, limit_dkph, target_dkph, emergency, mode;
    ULONG signal, block, station_limit_dkph, position_cm;
    ULONG stop_requested, doors_open, service_brake, hold_brake;
    ULONG at_station, station_number;
} SimState;

static TX_THREAD control_thread, ipc_thread;
static TX_MUTEX state_mutex;
static TX_QUEUE command_queue, result_queue;
static UCHAR control_stack[SIM_CONTROL_STACK], ipc_stack[SIM_IPC_STACK];
static ULONG command_storage[SIM_QUEUE_DEPTH * COMMAND_WORDS];
static ULONG result_storage[SIM_QUEUE_DEPTH];
static SimState state = {0UL, 800UL, 400UL, 0UL, MODE_AUTO,
    ASPECT_GREEN, BLOCK_CLEAR, 300UL,
    0UL, 0UL, 0UL, 0UL, 0UL, 0UL, 1UL};
    static ULONG next_station_cm = SIM_STATION_START_CM;

    static void control_thread_entry(ULONG input);
    static void ipc_thread_entry(ULONG input);

    int main(void)
    {
        tx_kernel_enter();
        return 0;
    }

    void tx_application_define(void *first_unused_memory)
    {
        UINT status;
        (void)first_unused_memory;
        status = tx_mutex_create(&state_mutex, "Simulation state", TX_INHERIT);
        if (status != TX_SUCCESS) { printf("Mutex oluşturulamadı: %u\n", status); return; }
        status = tx_queue_create(&command_queue, "Operator commands", COMMAND_WORDS,
                                 command_storage, sizeof(command_storage));
        if (status != TX_SUCCESS) { printf("Komut kuyruğu oluşturulamadı: %u\n", status); return; }
        status = tx_queue_create(&result_queue, "Command results", 1UL,
                                 result_storage, sizeof(result_storage));
        if (status != TX_SUCCESS) { printf("Sonuç kuyruğu oluşturulamadı: %u\n", status); return; }
        status = tx_thread_create(&control_thread, "Train control", control_thread_entry,
                                  0UL, control_stack, sizeof(control_stack), 5U, 5U,
                                  TX_NO_TIME_SLICE, TX_AUTO_START);
        if (status != TX_SUCCESS) { printf("Kontrol thread'i oluşturulamadı: %u\n", status); return; }
        status = tx_thread_create(&ipc_thread, "Local IPC interface", ipc_thread_entry,
                                  0UL, ipc_stack, sizeof(ipc_stack), 10U, 10U,
                                  TX_NO_TIME_SLICE, TX_AUTO_START);
        if (status != TX_SUCCESS) { printf("IPC thread'i oluşturulamadı: %u\n", status); return; }
        printf("ThreadX simülatörü yerel IPC adresinde hazır: %s\n", SIM_SOCKET_PATH);
    }

    static ULONG base_authority(const SimState *s)
    {
        ULONG cap = s->limit_dkph;
        if (s->block == BLOCK_OCCUPIED || s->signal == ASPECT_RED) return 0UL;
        if (s->signal == ASPECT_YELLOW && cap > SIM_YELLOW_DKPH) cap = SIM_YELLOW_DKPH;
        return cap;
    }

    static ULONG station_cap(const SimState *s, ULONG authority)
    {
        ULONG cap = authority;
        ULONG distance = next_station_cm > s->position_cm ? next_station_cm - s->position_cm : 0UL;
        if (distance <= SIM_STATION_ZONE_CM && cap > s->station_limit_dkph)
            cap = s->station_limit_dkph;
        if (s->stop_requested) {
            /* Yaklaşık 1.0 m/s^2 servis fren eğrisi; dkph^2 yaklaşık 26*cm. */
            double bound = sqrt((double)distance * 26.0);
            ULONG curve = (ULONG)bound;
            if (cap > curve) cap = curve;
        }
        return cap;
    }

    static ULONG apply_command(const SimCommand *cmd)
    {
        ULONG result = RESULT_OK;
        if (tx_mutex_get(&state_mutex, TX_WAIT_FOREVER) != TX_SUCCESS) return ERR_RANGE;
        switch ((CommandType)cmd->type) {
            case CMD_LIMIT:
                if (cmd->value > SIM_MAX_DKPH) result = ERR_RANGE;
                else state.limit_dkph = cmd->value;
                break;
            case CMD_STATION_LIMIT:
                if (cmd->value > SIM_MAX_DKPH) result = ERR_RANGE;
                else state.station_limit_dkph = cmd->value;
                break;
            case CMD_TARGET:
                if (cmd->value > SIM_MAX_DKPH) result = ERR_RANGE;
                else state.target_dkph = cmd->value;
                break;
            case CMD_MODE:
                if (cmd->value > MODE_MANUAL) result = ERR_RANGE;
                else state.mode = cmd->value;
                break;
            case CMD_EMERGENCY:
                if (cmd->value) state.emergency = 1UL;
                else if (state.speed_dkph == 0UL && state.signal != ASPECT_RED &&
                    state.block == BLOCK_CLEAR)
                    state.emergency = 0UL;
                else result = ERR_BRAKE_RESET;
                break;
            case CMD_SERVICE_BRAKE:
                state.service_brake = cmd->value ? 1UL : 0UL;
                break;
            case CMD_HOLD_BRAKE:
                state.hold_brake = cmd->value ? 1UL : 0UL;
                break;
            case CMD_SIGNAL:
                if (cmd->value > ASPECT_GREEN) result = ERR_RANGE;
                else state.signal = cmd->value;
                break;
            case CMD_BLOCK:
                if (cmd->value > BLOCK_OCCUPIED) result = ERR_RANGE;
                else state.block = cmd->value;
                break;
            case CMD_STOP_REQUEST:
                if (state.at_station) result = ERR_ALREADY_STOPPED;
                else if (state.stop_requested) result = ERR_STOP_ACTIVE;
                else if (next_station_cm <= state.position_cm + 100UL) result = ERR_STATION_PASSED;
                else state.stop_requested = 1UL;
                break;
            case CMD_STOP_CANCEL:
                if (!state.stop_requested) result = ERR_NO_STOP_REQUEST;
                else state.stop_requested = 0UL;
                break;
            case CMD_DOORS_OPEN:
                if (!state.at_station) result = ERR_NOT_AT_STATION;
                else if (state.speed_dkph != 0UL) result = ERR_TRAIN_MOVING;
                else state.doors_open = 1UL;
                break;
            case CMD_DOORS_CLOSE:
                if (!state.doors_open) result = ERR_DOORS_CLOSED;
                else state.doors_open = 0UL;
                break;
            case CMD_DEPART:
                if (!state.at_station) result = ERR_NOT_AT_STATION;
                else if (state.speed_dkph != 0UL) result = ERR_TRAIN_MOVING;
                else if (state.doors_open) result = ERR_DOORS_OPEN;
                else if (state.emergency || state.service_brake) result = ERR_BRAKE_ACTIVE;
                else if (state.signal == ASPECT_RED || state.block == BLOCK_OCCUPIED)
                    result = ERR_SIGNAL_BLOCKED;
            else {
                state.at_station = 0UL;
                state.hold_brake = 0UL;
                state.stop_requested = 0UL;
                next_station_cm += SIM_STATION_GAP_CM;
                state.station_number++;
            }
            break;
            case CMD_RESET:
                memset(&state, 0, sizeof(state));
                state.limit_dkph = 800UL;
                state.target_dkph = 400UL;
                state.station_limit_dkph = SIM_STATION_DKPH;
                state.signal = ASPECT_GREEN;
                state.mode = MODE_AUTO;
                state.station_number = 1UL;
                next_station_cm = SIM_STATION_START_CM;
                break;
            default:
                result = ERR_RANGE;
                break;
        }
        tx_mutex_put(&state_mutex);
        return result;
    }

    static void control_thread_entry(ULONG input)
    {
        SimCommand cmd;
        (void)input;
        for (;;) {
            while (tx_queue_receive(&command_queue, &cmd, TX_NO_WAIT) == TX_SUCCESS) {
                ULONG result = apply_command(&cmd);
                (void)tx_queue_send(&result_queue, &result, TX_NO_WAIT);
            }
            if (tx_mutex_get(&state_mutex, TX_WAIT_FOREVER) == TX_SUCCESS) {
                ULONG authority = base_authority(&state);
                ULONG cap = station_cap(&state, authority);
                ULONG target = state.target_dkph < cap ? state.target_dkph : cap;
                ULONG distance = next_station_cm > state.position_cm ?
                next_station_cm - state.position_cm : 0UL;
                if (state.speed_dkph > authority) state.emergency = 1UL;
                if (state.emergency) {
                    state.speed_dkph = state.speed_dkph > 10UL ? state.speed_dkph - 10UL : 0UL;
                } else if (state.hold_brake || state.service_brake || state.speed_dkph > target) {
                    state.speed_dkph = state.speed_dkph > 4UL ? state.speed_dkph - 4UL : 0UL;
                } else if (state.speed_dkph < target) {
                    ULONG step = state.mode == MODE_MANUAL ? 1UL : 2UL;
                    state.speed_dkph = state.speed_dkph + step > target ? target : state.speed_dkph + step;
                }
                if (!state.at_station && state.speed_dkph > 0UL) {
                    ULONG movement = state.speed_dkph * 5UL / 18UL; /* 100 ms, cm */
                    if (movement == 0UL) movement = 1UL;
                    state.position_cm += movement;
                    if (state.stop_requested && distance <= movement + 500UL &&
                        state.speed_dkph <= 40UL) {
                        state.position_cm = next_station_cm;
                    state.speed_dkph = 0UL;
                    state.at_station = 1UL;
                    state.stop_requested = 0UL;
                    state.hold_brake = 1UL;
                        }
                }
                tx_mutex_put(&state_mutex);
            }
            tx_thread_sleep(SIM_CONTROL_PERIOD);
        }
    }

    static int parse_command(char *text, SimCommand *cmd)
    {
        unsigned long value;
        char extra;
        size_t n = strlen(text);
        while (n && (text[n - 1] == '\n' || text[n - 1] == '\r' || text[n - 1] == ' ' || text[n - 1] == '\t'))
            text[--n] = '\0';
        while (*text == ' ' || *text == '\t') ++text;
        if (!strcmp(text, "GET")) return 0;
        #define PARSE_VALUE(prefix, kind) \
        if (sscanf(text, prefix " %lu %c", &value, &extra) == 1) { cmd->type = kind; cmd->value = (ULONG)value; return 1; }
        PARSE_VALUE("LIMIT", CMD_LIMIT)
        PARSE_VALUE("STATION LIMIT", CMD_STATION_LIMIT)
        PARSE_VALUE("TARGET", CMD_TARGET)
        #undef PARSE_VALUE
        if (!strcmp(text, "MODE AUTO")) { cmd->type=CMD_MODE; cmd->value=MODE_AUTO; return 1; }
        if (!strcmp(text, "MODE MANUAL")) { cmd->type=CMD_MODE; cmd->value=MODE_MANUAL; return 1; }
        if (!strcmp(text, "BRAKE EMERGENCY ON")) { cmd->type=CMD_EMERGENCY; cmd->value=1UL; return 1; }
        if (!strcmp(text, "BRAKE EMERGENCY OFF")) { cmd->type=CMD_EMERGENCY; cmd->value=0UL; return 1; }
        if (!strcmp(text, "BRAKE SERVICE ON")) { cmd->type=CMD_SERVICE_BRAKE; cmd->value=1UL; return 1; }
        if (!strcmp(text, "BRAKE SERVICE OFF")) { cmd->type=CMD_SERVICE_BRAKE; cmd->value=0UL; return 1; }
        if (!strcmp(text, "BRAKE HOLD ON")) { cmd->type=CMD_HOLD_BRAKE; cmd->value=1UL; return 1; }
        if (!strcmp(text, "BRAKE HOLD OFF")) { cmd->type=CMD_HOLD_BRAKE; cmd->value=0UL; return 1; }
        if (!strcmp(text, "SIGNAL RED")) { cmd->type=CMD_SIGNAL; cmd->value=ASPECT_RED; return 1; }
        if (!strcmp(text, "SIGNAL YELLOW")) { cmd->type=CMD_SIGNAL; cmd->value=ASPECT_YELLOW; return 1; }
        if (!strcmp(text, "SIGNAL GREEN")) { cmd->type=CMD_SIGNAL; cmd->value=ASPECT_GREEN; return 1; }
        if (!strcmp(text, "BLOCK OCCUPIED")) { cmd->type=CMD_BLOCK; cmd->value=BLOCK_OCCUPIED; return 1; }
        if (!strcmp(text, "BLOCK CLEAR")) { cmd->type=CMD_BLOCK; cmd->value=BLOCK_CLEAR; return 1; }
        if (!strcmp(text, "STATION STOP")) { cmd->type=CMD_STOP_REQUEST; cmd->value=1UL; return 1; }
        if (!strcmp(text, "STATION CANCEL")) { cmd->type=CMD_STOP_CANCEL; cmd->value=0UL; return 1; }
        if (!strcmp(text, "DOORS OPEN")) { cmd->type=CMD_DOORS_OPEN; cmd->value=1UL; return 1; }
        if (!strcmp(text, "DOORS CLOSE")) { cmd->type=CMD_DOORS_CLOSE; cmd->value=0UL; return 1; }
        if (!strcmp(text, "DEPART")) { cmd->type=CMD_DEPART; cmd->value=0UL; return 1; }
        if (!strcmp(text, "RESET")) { cmd->type=CMD_RESET; cmd->value=0UL; return 1; }
        return -1;
    }

    static void state_response(int fd, const struct sockaddr_un *peer, socklen_t peer_len,
                               ULONG result, int is_command)
    {
        SimState s;
        char response[256];
        if (tx_mutex_get(&state_mutex, TX_WAIT_FOREVER) != TX_SUCCESS) return;
        s = state;
        tx_mutex_put(&state_mutex);
        (void)snprintf(response, sizeof(response), "%s,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu",
                       is_command ? (result == RESULT_OK ? "ACK" : "ERR") : "STATE",
                       (unsigned long)result,
                       (unsigned long)s.speed_dkph, (unsigned long)s.limit_dkph,
                       (unsigned long)s.target_dkph, (unsigned long)s.emergency,
                       (unsigned long)s.mode, (unsigned long)s.signal, (unsigned long)s.block,
                       (unsigned long)s.station_limit_dkph, (unsigned long)s.position_cm,
                       (unsigned long)s.stop_requested, (unsigned long)s.doors_open,
                       (unsigned long)s.service_brake, (unsigned long)s.hold_brake,
                       (unsigned long)s.at_station, (unsigned long)s.station_number);
        (void)sendto(fd, response, strlen(response), 0,
                     (const struct sockaddr *)peer, peer_len);
    }

    static void ipc_thread_entry(ULONG input)
    {
        int fd;
        struct sockaddr_un local_addr;
        struct timeval timeout;
        (void)input;
        fd = socket(AF_UNIX, SOCK_DGRAM, 0);
        if (fd < 0) { printf("IPC socket açılamadı: %s\n", strerror(errno)); return; }
        timeout.tv_sec = 0; timeout.tv_usec = 100000;
        (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        memset(&local_addr, 0, sizeof(local_addr));
        local_addr.sun_family = AF_UNIX;
        (void)snprintf(local_addr.sun_path, sizeof(local_addr.sun_path), "%s", SIM_SOCKET_PATH);
        if (bind(fd, (const struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
            printf("IPC bind başarısız (%s): %s. Eski socket dosyasını kontrol edin.\n",
                   SIM_SOCKET_PATH, strerror(errno));
            close(fd); return;
        }
        for (;;) {
            char buffer[128];
            struct sockaddr_un peer;
            socklen_t peer_len = sizeof(peer);
            ssize_t n = recvfrom(fd, buffer, sizeof(buffer) - 1, 0,
                                 (struct sockaddr *)&peer, &peer_len);
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
                printf("IPC recvfrom hatası: %s\n", strerror(errno)); tx_thread_sleep(10UL); continue;
            }
            buffer[n] = '\0';
            if (peer.sun_path[0] == '\0') continue;
            {
                SimCommand cmd = {0UL, 0UL, 0UL};
                int parsed = parse_command(buffer, &cmd);
                if (parsed < 0) {
                    state_response(fd, &peer, peer_len, ERR_RANGE, 1);
                    continue;
                }
                if (parsed == 0) {
                    state_response(fd, &peer, peer_len, RESULT_OK, 0);
                    continue;
                }
                if (tx_queue_send(&command_queue, &cmd, TX_NO_WAIT) != TX_SUCCESS) {
                    state_response(fd, &peer, peer_len, ERR_RANGE, 1);
                    continue;
                }
                {
                    ULONG result = ERR_RANGE;
                    if (tx_queue_receive(&result_queue, &result, 100UL) != TX_SUCCESS)
                        result = ERR_RANGE;
                    state_response(fd, &peer, peer_len, result, 1);
                }
            }
        }
    }
