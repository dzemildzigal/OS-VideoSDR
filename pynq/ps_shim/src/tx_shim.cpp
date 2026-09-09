/*
 * tx_shim.cpp - B.3 PYNQ PS sender for the DDR packet ring, native-XRT
 * buffer edition.
 *
 * tx_shim now owns ALL ring-related physical memory itself. It opens the
 * device that tx_daemon.py already programmed (no xclbin reload), then
 * allocates the ring and control-page buffers as native XRT buffer objects
 * (xrt::bo) and writes their physical addresses into frame_writer_0's
 * registers. tx_daemon.py no longer allocates or touches ring memory at
 * all: it only loads the overlay, selects the design clock, configures the
 * AES sequencer, and pulses HDMI HPD.
 *
 * Why this replaces the previous /dev/mem-based tx_shim.c:
 *   1. The old ring mapping used /dev/mem with O_SYNC, which on this ARM
 *      CPU makes the mapping non-cacheable, strongly-ordered device
 *      memory. Every batch read was a slow, one-word-at-a-time bus access
 *      with no burst/prefetch - the real reason the shim capped out near
 *      43,000-46,000 packets/s.
 *   2. Removing O_SYNC to get a fast mapping created a SECOND, independent
 *      mapping of the SAME physical pages that pynq/XRT already mapped
 *      with a different attribute in the daemon process. The ARM
 *      architecture calls two different memory attributes for the same
 *      physical page a "mismatched memory attribute", and its behavior is
 *      officially undefined - almost certainly the real cause of the
 *      register-readback and ring-stuck symptoms seen during testing.
 * A single xrt::bo, allocated once, mapped once, with the driver's own
 * bo.sync() call standing in for the previous home-grown ARM cacheflush
 * syscall, removes both problems at once.
 *
 * The tiny frame_writer_0 / aes_seq_0 AXI-Lite register spaces remain raw
 * /dev/mem MMIO. That is the correct, intended use of /dev/mem: small,
 * low-frequency register access that must stay non-cacheable and strongly
 * ordered. Only the bulk ring/control DATA moved to XRT-managed memory.
 *
 * argv compatibility (unchanged from the previous tx_shim):
 *   tx_shim [dst-ip] [dst-port] [send|nosend|nocopy]
 *   tx_shim --dst-host IP --dst-port PORT [--mode send|nosend|nocopy]
 */

#include <arpa/inet.h>
#include <cerrno>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <xrt/xrt_bo.h>
#include <xrt/xrt_device.h>

#ifndef UDP_SEGMENT
#define UDP_SEGMENT 103
#endif

#define WRITER_BASE 0x40000000ULL
#define SEQ_BASE    0x40001000ULL

/* DDRRingWriter AXI-Lite register map (unchanged from the previous shim). */
#define REG_WRITER_CONTROL      0x0004u
#define REG_WRITER_STATUS       0x0008u
#define REG_RING_BASE_LO        0x000Cu
#define REG_RING_BASE_HI        0x0010u
#define REG_CTRL_BASE_LO        0x0014u
#define REG_CTRL_BASE_HI        0x0018u
#define REG_RING_LOG2           0x001Cu
#define REG_SLOT_STRIDE         0x0020u
#define REG_DROP_COUNT          0x002Cu
#define REG_COMPLETE_COUNT_LO   0x0030u

#define REG_SEQ_CONTROL         0x0000u

#define RING_LOG2_DEFAULT       11u
#define RING_SLOTS_DEFAULT      (1u << RING_LOG2_DEFAULT)
#define SLOT_STRIDE_DEFAULT     1280u
#define AUTHENTICATED_BYTES     1240u
#define CONTROL_PAGE_BYTES      4096u
#define MAX_GSO_SLOTS           32u
#define SHORT_BATCH_DELAY_NS    2000000ULL
#define RING_BYTES              ((size_t)RING_SLOTS_DEFAULT * SLOT_STRIDE_DEFAULT)
#define XRT_MEMORY_GROUP        0u

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

/* Tiny, low-frequency AXI-Lite register space only. Correctly non-cacheable
 * and strongly ordered, as MMIO register access must be. */
static volatile uint8_t *map_devmem(uint64_t phys, size_t len)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open(/dev/mem)");
        return (volatile uint8_t *)MAP_FAILED;
    }

    uint64_t page = phys & ~0xFFFULL;
    size_t span = len + (size_t)(phys & 0xFFFULL);
    void *mapped = mmap(NULL, span, PROT_READ | PROT_WRITE,
                        MAP_SHARED, fd, (off_t)page);
    close(fd);
    if (mapped == MAP_FAILED) {
        perror("mmap(/dev/mem)");
        return (volatile uint8_t *)MAP_FAILED;
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

    /* Disable the writer before (re)configuring it. Matches the daemon's
     * former soft_reset(); the daemon no longer touches this register. */
    wr32(fw, REG_WRITER_CONTROL, 0);

    uint32_t ring_log2 = rd32(fw, REG_RING_LOG2);
    uint32_t slot_stride = rd32(fw, REG_SLOT_STRIDE);
    uint32_t ring_slots = (ring_log2 < 31) ? (1u << ring_log2) : 0;
    if (ring_log2 != RING_LOG2_DEFAULT || ring_slots != RING_SLOTS_DEFAULT ||
        slot_stride != SLOT_STRIDE_DEFAULT) {
        fprintf(stderr,
                "tx_shim: unexpected ring geometry log2=%u slots=%u stride=%u\n",
                ring_log2, ring_slots, slot_stride);
        return 1;
    }

    xrt::device device;
    xrt::bo ring_bo;
    xrt::bo ctrl_bo;
    try {
        /* Attaches to the device tx_daemon.py already programmed. This does
         * not call load_xclbin and does not reprogram anything. */
        device = xrt::device(0);
        ring_bo = xrt::bo(device, RING_BYTES, xrt::bo::flags::cacheable,
                          XRT_MEMORY_GROUP);
        ctrl_bo = xrt::bo(device, CONTROL_PAGE_BYTES, xrt::bo::flags::normal,
                          XRT_MEMORY_GROUP);
    } catch (const std::exception &e) {
        fprintf(stderr, "tx_shim: XRT device/buffer setup failed: %s\n", e.what());
        return 1;
    }

    uint8_t *ring = ring_bo.map<uint8_t *>();
    uint32_t *ctrl = ctrl_bo.map<uint32_t *>();
    if (ring == NULL || ctrl == NULL) {
        fprintf(stderr, "tx_shim: XRT buffer map() returned NULL\n");
        return 1;
    }
    memset(ctrl, 0, CONTROL_PAGE_BYTES);
    ctrl_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE);

    uint64_t ring_base = ring_bo.address();
    uint64_t ctrl_base = ctrl_bo.address();
    if (ring_base == 0 || ctrl_base == 0 || (ring_base & 127ULL) != 0 ||
        (ctrl_base & 0xFFFULL) != 0 ||
        (ctrl_base >= ring_base && ctrl_base < ring_base + RING_BYTES) ||
        (ring_base >= ctrl_base && ring_base < ctrl_base + CONTROL_PAGE_BYTES)) {
        fprintf(stderr,
                "tx_shim: unsafe ring geometry: ring=0x%" PRIX64
                " ctrl=0x%" PRIX64 "\n", ring_base, ctrl_base);
        return 1;
    }

    wr32(fw, REG_RING_BASE_LO, (uint32_t)(ring_base & 0xFFFFFFFFu));
    wr32(fw, REG_RING_BASE_HI, (uint32_t)((ring_base >> 32) & 0xFFFFFFFFu));
    wr32(fw, REG_CTRL_BASE_LO, (uint32_t)(ctrl_base & 0xFFFFFFFFu));
    wr32(fw, REG_CTRL_BASE_HI, (uint32_t)((ctrl_base >> 32) & 0xFFFFFFFFu));
    wr32(fw, REG_WRITER_CONTROL, 1u);

    uint32_t ring_mask = ring_slots - 1u;
    uint32_t consume = ctrl_load_acquire(&ctrl[1]) & ring_mask;
    uint32_t produce = ctrl_load_acquire(&ctrl[0]) & ring_mask;
    uint32_t writer_status = rd32(fw, REG_WRITER_STATUS);
    printf("tx_shim: ring @ 0x%" PRIX64 " (%u slots x %u = %zu bytes) [xrt::bo cacheable]\n",
           ring_base, ring_slots, slot_stride, RING_BYTES);
    printf("tx_shim: ctrl @ 0x%" PRIX64 " produce=%u consume=%u status=0x%08X [xrt::bo normal]\n",
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

    /* The configure-only daemon leaves the sequencer disabled until this
     * point, exactly as before: the writer must be enabled and ready to
     * drain before the AES pipeline can start producing packets. */
    wr32(seq, REG_SEQ_CONTROL, 1u);
    printf("tx_shim: sequencer enabled; draining ring\n");

    uint64_t stat_start = monotonic_ns();
    uint64_t stat_pkts = 0;
    uint64_t stat_batches = 0;
    uint64_t stat_syscalls = 0;
    uint64_t stat_short_batches = 0;
    uint64_t stat_slot_bytes = 0;
    uint64_t stat_sync_ns = 0;
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
            size_t byte_offset = (size_t)part_index * slot_stride;
            uint8_t *slot_ptr = ring + byte_offset;
            size_t part_bytes = (size_t)part_slots * slot_stride;

            if (do_cache) {
                uint64_t c0 = monotonic_ns();
                ring_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE, part_bytes, byte_offset);
                stat_sync_ns += monotonic_ns() - c0;
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
            double sync_us = (double)stat_sync_ns / 1000.0;
            printf("tx_shim: pkts/s=%.1f batches/s=%.1f syscalls/s=%.1f "
                   "bo-sync-us=%.1f slot-bytes/s=%.0f "
                   "drops=%u drops_delta=%u short-batches=%" PRIu64
                   " produce=%u consume=%u complete=%u spins=%" PRIu64 "\n",
                   pkts_s, batches_s, syscalls_s, sync_us,
                   (double)stat_slot_bytes / seconds,
                   drops_now, drops_now - drops_last, stat_short_batches,
                   produce, consume, complete_now, stat_spins);
            stat_start = now;
            stat_pkts = 0;
            stat_batches = 0;
            stat_syscalls = 0;
            stat_short_batches = 0;
            stat_slot_bytes = 0;
            stat_sync_ns = 0;
            stat_spins = 0;
            drops_last = drops_now;
        }
    }

    if (sock >= 0)
        close(sock);
    return 0;
}
