#define _GNU_SOURCE
/*
 * tx_shim.c - B.3 PYNQ PS sender for the DDR packet ring.
 *
 * The daemon allocates one physically contiguous ring and one separate
 * control page, then programs both physical addresses into frame_writer_0.
 * This process maps those regions, enables the sequencer, and sends complete
 * 1280-byte ring slots with UDP_SEGMENT=1280. The kernel emits one UDP packet
 * per slot. The first 1240 bytes are the authenticated B.1 body; bytes
 * 1240..1279 are deliberate unauthenticated transport padding.
 *
 * Normal path:
 *   read control.produce_idx
 *   invalidate one contiguous batch range
 *   send(sock, ring_slots, count * 1280, 0)
 *   release-store control.consume_idx
 *
 * A batch never contains a partial slot. A ring-wrap batch uses two GSO
 * sends, one for each contiguous range. The old per-buffer nonce-prefix,
 * copy, sendmmsg, and MMIO-ack path is not used.
 *
 * argv compatibility:
 *   tx_shim [dst-ip] [dst-port] [send|nosend|nocopy]
 *   tx_shim --dst-host IP --dst-port PORT [--mode send|nosend|nocopy]
 */

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef UDP_SEGMENT
#define UDP_SEGMENT 103
#endif

#define WRITER_BASE 0x40000000ULL
#define SEQ_BASE    0x40001000ULL

/* DDRRingWriter AXI-Lite register map. */
#define REG_WRITER_CONTROL      0x0004u
#define REG_WRITER_STATUS       0x0008u
#define REG_RING_BASE_LO        0x000Cu
#define REG_RING_BASE_HI        0x0010u
#define REG_CTRL_BASE_LO        0x0014u
#define REG_CTRL_BASE_HI        0x0018u
#define REG_RING_LOG2           0x001Cu
#define REG_SLOT_STRIDE         0x0020u
#define REG_PRODUCE_IDX         0x0024u
#define REG_CONSUME_SHADOW      0x0028u
#define REG_DROP_COUNT          0x002Cu
#define REG_COMPLETE_COUNT_LO   0x0030u
#define REG_COMPLETE_COUNT_HI   0x0034u
#define REG_FAULT_CODE          0x003Cu

#define REG_SEQ_CONTROL         0x0000u

#define RING_LOG2_DEFAULT       11u
#define RING_SLOTS_DEFAULT      (1u << RING_LOG2_DEFAULT)
#define SLOT_STRIDE_DEFAULT     1280u
#define AUTHENTICATED_BYTES     1240u
#define CONTROL_PAGE_BYTES      4096u
#define MAX_GSO_SLOTS           32u
#define SHORT_BATCH_DELAY_NS    2000000ULL

#define __ARM_NR_cacheflush 0x0f0002u

typedef struct {
    const char *dst_ip;
    uint16_t dst_port;
    const char *mode;
} Options;

static inline uint32_t rd32(volatile uint8_t *base, uint32_t off)
{
    return *(volatile uint32_t *)(base + off);
}

static inline void wr32(volatile uint8_t *base, uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(base + off) = val;
}

static inline uint64_t monotonic_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void dcache_invalidate(void *start, size_t len)
{
    /* Keep this operation: removing it caused authenticated PL data to fail. */
    (void)syscall(__ARM_NR_cacheflush, start, (char *)start + len, 0);
}

static volatile uint8_t *map_devmem(uint64_t phys, size_t len)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open(/dev/mem)");
        return MAP_FAILED;
    }

    uint64_t page = phys & ~0xFFFULL;
    size_t span = len + (size_t)(phys & 0xFFFULL);
    void *mapped = mmap(NULL, span, PROT_READ | PROT_WRITE,
                        MAP_SHARED, fd, (off_t)page);
    close(fd);
    if (mapped == MAP_FAILED) {
        perror("mmap(/dev/mem)");
        return MAP_FAILED;
    }
    return (volatile uint8_t *)mapped + (phys & 0xFFFULL);
}

static void usage(const char *prog)
{
    fprintf(stderr,
            "usage: %s [dst-ip] [dst-port] [send|nosend|nocopy]\n"
            "       %s --dst-host IP --dst-port PORT [--mode MODE]\n",
            prog, prog);
}

static int parse_options(int argc, char **argv, Options *out)
{
    int positional = 0;
    out->dst_ip = "192.168.0.37";
    out->dst_port = 5600;
    out->mode = "send";

    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0) {
            usage(argv[0]);
            return 1;
        }
        if (strcmp(arg, "--dst-host") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--dst-host requires a value\n");
                return -1;
            }
            out->dst_ip = argv[i];
            continue;
        }
        if (strcmp(arg, "--dst-port") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--dst-port requires a value\n");
                return -1;
            }
            char *end = NULL;
            long port = strtol(argv[i], &end, 10);
            if (*argv[i] == '\0' || *end != '\0' || port < 1 || port > 65535) {
                fprintf(stderr, "invalid --dst-port: %s\n", argv[i]);
                return -1;
            }
            out->dst_port = (uint16_t)port;
            continue;
        }
        if (strcmp(arg, "--mode") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--mode requires a value\n");
                return -1;
            }
            out->mode = argv[i];
            continue;
        }
        if (arg[0] == '-') {
            fprintf(stderr, "unknown option: %s\n", arg);
            return -1;
        }

        if (positional == 0) {
            out->dst_ip = arg;
        } else if (positional == 1) {
            char *end = NULL;
            long port = strtol(arg, &end, 10);
            if (*arg == '\0' || *end != '\0' || port < 1 || port > 65535) {
                fprintf(stderr, "invalid destination port: %s\n", arg);
                return -1;
            }
            out->dst_port = (uint16_t)port;
        } else if (positional == 2) {
            out->mode = arg;
        } else {
            fprintf(stderr, "too many positional arguments\n");
            return -1;
        }
        positional++;
    }

    if (strcmp(out->mode, "send") != 0 &&
        strcmp(out->mode, "nosend") != 0 &&
        strcmp(out->mode, "nocopy") != 0) {
        fprintf(stderr, "mode must be send, nosend, or nocopy\n");
        return -1;
    }
    return 0;
}

static inline uint32_t ctrl_load_acquire(volatile uint32_t *word)
{
    uint32_t value = *word;
    __sync_synchronize();
    return value;
}

static inline void ctrl_store_release(volatile uint32_t *word, uint32_t value)
{
    __sync_synchronize();
    *word = value;
    __sync_synchronize();
}

static int send_gso(int sock, const void *data, size_t bytes, int do_send)
{
    if (!do_send)
        return 0;

    ssize_t sent = send(sock, data, bytes, 0);
    if (sent < 0) {
        if (errno != EINTR && errno != EAGAIN && errno != ENOBUFS)
            perror("send(UDP GSO)");
        return -1;
    }
    if ((size_t)sent != bytes) {
        fprintf(stderr, "send(UDP GSO) short write: %zd of %zu bytes\n",
                sent, bytes);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);

    Options opt;
    int parse_rc = parse_options(argc, argv, &opt);
    if (parse_rc != 0)
        return parse_rc > 0 ? 0 : 2;

    int do_send = strcmp(opt.mode, "send") == 0;
    int do_cache = strcmp(opt.mode, "nocopy") != 0;
    if (!do_send || !do_cache)
        printf("tx_shim: MEASURE MODE '%s' - network/cache operation disabled as selected\n",
               opt.mode);

    volatile uint8_t *fw = map_devmem(WRITER_BASE, 0x1000);
    volatile uint8_t *seq = map_devmem(SEQ_BASE, 0x1000);
    if (fw == MAP_FAILED || seq == MAP_FAILED)
        return 1;

    uint64_t ring_base = ((uint64_t)rd32(fw, REG_RING_BASE_HI) << 32) |
                         rd32(fw, REG_RING_BASE_LO);
    uint64_t ctrl_base = ((uint64_t)rd32(fw, REG_CTRL_BASE_HI) << 32) |
                         rd32(fw, REG_CTRL_BASE_LO);
    uint32_t ring_log2 = rd32(fw, REG_RING_LOG2);
    uint32_t slot_stride = rd32(fw, REG_SLOT_STRIDE);
    uint32_t ring_slots = (ring_log2 < 31) ? (1u << ring_log2) : 0;

    if (ring_log2 != RING_LOG2_DEFAULT || ring_slots != RING_SLOTS_DEFAULT ||
        slot_stride != SLOT_STRIDE_DEFAULT || ring_base == 0 || ctrl_base == 0) {
        fprintf(stderr,
                "tx_shim: invalid ring config base=0x%" PRIX64
                " ctrl=0x%" PRIX64 " log2=%u stride=%u\n",
                ring_base, ctrl_base, ring_log2, slot_stride);
        return 1;
    }

    size_t ring_bytes = (size_t)ring_slots * (size_t)slot_stride;
    if ((ring_base & 127ULL) != 0 || (ctrl_base & 0xFFFULL) != 0 ||
        (ctrl_base >= ring_base && ctrl_base < ring_base + ring_bytes)) {
        fprintf(stderr,
                "tx_shim: unsafe ring geometry: ring alignment=%" PRIu64
                " ctrl alignment=%" PRIu64 " overlap=%d\n",
                ring_base & 127ULL, ctrl_base & 0xFFFULL,
                ctrl_base >= ring_base && ctrl_base < ring_base + ring_bytes);
        return 1;
    }

    volatile uint8_t *ring = map_devmem(ring_base, ring_bytes);
    volatile uint8_t *ctrl_map = map_devmem(ctrl_base, CONTROL_PAGE_BYTES);
    if (ring == MAP_FAILED || ctrl_map == MAP_FAILED)
        return 1;
    volatile uint32_t *ctrl = (volatile uint32_t *)ctrl_map;
    uint32_t ring_mask = ring_slots - 1u;

    uint32_t consume = ctrl_load_acquire(&ctrl[1]) & ring_mask;
    uint32_t produce = ctrl_load_acquire(&ctrl[0]) & ring_mask;
    uint32_t writer_status = rd32(fw, REG_WRITER_STATUS);
    printf("tx_shim: ring @ 0x%" PRIX64 " (%u slots x %u = %zu bytes)\n",
           ring_base, ring_slots, slot_stride, ring_bytes);
    printf("tx_shim: ctrl @ 0x%" PRIX64 " produce=%u consume=%u status=0x%08X\n",
           ctrl_base, produce, consume, writer_status);
    printf("tx_shim: authenticated body=%u bytes, transport slot=%u bytes\n",
           AUTHENTICATED_BYTES, slot_stride);

    int sock = -1;
    if (do_send) {
        sock = socket(AF_INET, SOCK_DGRAM, 0);
        if (sock < 0) {
            perror("socket");
            return 1;
        }
        int sndbuf = 4 * 1024 * 1024;
        if (setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf)) < 0)
            perror("setsockopt(SO_SNDBUF)");

        int segment_size = (int)slot_stride;
        if (setsockopt(sock, IPPROTO_UDP, UDP_SEGMENT,
                       &segment_size, sizeof(segment_size)) < 0) {
            perror("setsockopt(UDP_SEGMENT=1280)");
            close(sock);
            return 1;
        }

        struct sockaddr_in dst;
        memset(&dst, 0, sizeof(dst));
        dst.sin_family = AF_INET;
        dst.sin_port = htons(opt.dst_port);
        if (inet_pton(AF_INET, opt.dst_ip, &dst.sin_addr) != 1) {
            fprintf(stderr, "bad destination IP: %s\n", opt.dst_ip);
            close(sock);
            return 1;
        }
        if (connect(sock, (const struct sockaddr *)&dst, sizeof(dst)) < 0) {
            perror("connect");
            close(sock);
            return 1;
        }
        printf("tx_shim: UDP GSO segment=%u batch<=%u destination=%s:%u\n",
               slot_stride, MAX_GSO_SLOTS, opt.dst_ip, opt.dst_port);
    }

    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(1, &cpuset);
    if (sched_setaffinity(0, sizeof(cpuset), &cpuset) != 0)
        perror("sched_setaffinity CPU1 (continuing)");
    if (setpriority(PRIO_PROCESS, 0, -20) != 0)
        perror("setpriority -20 (continuing)");

    /* The configure-only daemon leaves the sequencer disabled until this point. */
    wr32(seq, REG_SEQ_CONTROL, 1u);
    printf("tx_shim: sequencer enabled; draining ring\n");

    uint64_t stat_start = monotonic_ns();
    uint64_t stat_pkts = 0;
    uint64_t stat_batches = 0;
    uint64_t stat_syscalls = 0;
    uint64_t stat_short_batches = 0;
    uint64_t stat_slot_bytes = 0;
    uint64_t stat_cache_ns = 0;
    uint64_t stat_spins = 0;
    uint32_t drops_last = rd32(fw, REG_DROP_COUNT);
    uint64_t partial_since = 0;

    for (;;) {
        produce = ctrl_load_acquire(&ctrl[0]) & ring_mask;
        uint32_t available = (produce - consume) & ring_mask;
        if (available == 0) {
            stat_spins++;
            continue;
        }

        uint64_t now = monotonic_ns();
        if (available < MAX_GSO_SLOTS) {
            if (partial_since == 0)
                partial_since = now;
            if (now - partial_since < SHORT_BATCH_DELAY_NS)
                continue;
        } else {
            partial_since = 0;
        }

        uint32_t batch = available > MAX_GSO_SLOTS ? MAX_GSO_SLOTS : available;
        uint32_t batch_start = consume;
        uint32_t first = ring_slots - batch_start;
        if (first > batch)
            first = batch;
        uint32_t second = batch - first;
        uint32_t parts[2] = {first, second};
        uint32_t sent_slots = 0;
        int failed = 0;

        for (unsigned part = 0; part < 2 && parts[part] != 0; part++) {
            uint32_t part_slots = parts[part];
            /* Use the original batch start plus the amount already sent.
             * Do not add sent_slots to the already-advanced consume index:
             * that skips the second half of a ring-wrap batch. */
            uint32_t part_index = (batch_start + sent_slots) & ring_mask;
            volatile uint8_t *slot_ptr = ring +
                ((size_t)part_index * slot_stride);
            size_t part_bytes = (size_t)part_slots * slot_stride;

            if (do_cache) {
                uint64_t c0 = monotonic_ns();
                dcache_invalidate((void *)slot_ptr, part_bytes);
                stat_cache_ns += monotonic_ns() - c0;
            }

            stat_syscalls += do_send ? 1u : 0u;
            if (send_gso(sock, (const void *)slot_ptr, part_bytes, do_send) != 0) {
                failed = 1;
                break;
            }

            sent_slots += part_slots;
            consume = (batch_start + sent_slots) & ring_mask;
            ctrl_store_release(&ctrl[1], consume);
            stat_pkts += part_slots;
            stat_slot_bytes += (uint64_t)part_slots * slot_stride;
        }

        if (sent_slots == 0)
            continue;

        stat_batches++;
        if (!failed && sent_slots < MAX_GSO_SLOTS)
            stat_short_batches++;
        if (failed) {
            /* A successful first half of a wrap is already published. The
             * next iteration retries only the unsent slots. */
            partial_since = 0;
            continue;
        }
        partial_since = 0;

        now = monotonic_ns();
        if (now - stat_start >= 1000000000ULL) {
            uint32_t drops_now = rd32(fw, REG_DROP_COUNT);
            uint32_t complete_now = rd32(fw, REG_COMPLETE_COUNT_LO);
            double seconds = (double)(now - stat_start) / 1e9;
            double pkts_s = (double)stat_pkts / seconds;
            double batches_s = (double)stat_batches / seconds;
            double syscalls_s = (double)stat_syscalls / seconds;
            double cache_us = (double)stat_cache_ns / 1000.0;
            printf("tx_shim: pkts/s=%.1f batches/s=%.1f syscalls/s=%.1f "
                   "cache-invalidate-us=%.1f slot-bytes/s=%.0f "
                   "drops=%u drops_delta=%u short-batches=%" PRIu64
                   " produce=%u consume=%u complete=%u spins=%" PRIu64 "\n",
                   pkts_s, batches_s, syscalls_s, cache_us,
                   (double)stat_slot_bytes / seconds,
                   drops_now, drops_now - drops_last, stat_short_batches,
                   produce, consume, complete_now, stat_spins);
            stat_start = now;
            stat_pkts = 0;
            stat_batches = 0;
            stat_syscalls = 0;
            stat_short_batches = 0;
            stat_slot_bytes = 0;
            stat_cache_ns = 0;
            stat_spins = 0;
            drops_last = drops_now;
        }
    }

    if (sock >= 0)
        close(sock);
    return 0;
}
