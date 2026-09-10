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
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/file.h>
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
// Read-only: 1 when the writer reaches DDR through the PS ACP, so its writes
// snoop the CPU caches and no invalidate is needed before reading slots.
#define REG_COHERENT            0x004Cu
// PS-pushed consume index. The writer takes its full-ring drop decision from
// this register instead of reading the control page over its AXI master port,
// because that read returns the writer's own produce word on this hardware
// (the HP0 read path mis-answers a 4-byte read at +4 inside the 8-byte region
// the publish write touches). Push it after every sent batch.
#define REG_PS_CONSUME          0x0048u

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

    // Batch the cache maintenance. One sync per 40,960-byte batch costs
    // ~119 us and 195 ms per second of CPU. Instead, sync a window that
    // covers the next SYNC_AHEAD_BYTES and remember how much of it is still
    // covered. Invalidation only drops CPU cache lines (it never touches
    // DDR), the PS reads each slot exactly once after publication, and with
    // drops working the writer cannot lap the consumer, so published slot
    // data is stable. The window is therefore safe and cuts the sync call
    // count by roughly (1 + SYNC_AHEAD_BYTES / batch_bytes).
#define SYNC_AHEAD_BYTES (7u * MAX_GSO_SLOTS * SLOT_STRIDE_DEFAULT)

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

static int make_tx_socket(const Options &opt, uint32_t slot_stride)
{
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        return -1;
    }
    int sndbuf = 4 * 1024 * 1024;
    if (setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf)) < 0)
        perror("setsockopt(SO_SNDBUF)");

    int segment_size = (int)slot_stride;
    if (setsockopt(sock, IPPROTO_UDP, UDP_SEGMENT,
                   &segment_size, sizeof(segment_size)) < 0) {
        perror("setsockopt(UDP_SEGMENT=1280)");
        close(sock);
        return -1;
    }

    struct sockaddr_in dst;
    memset(&dst, 0, sizeof(dst));
    dst.sin_family = AF_INET;
    dst.sin_port = htons(opt.dst_port);
    if (inet_pton(AF_INET, opt.dst_ip, &dst.sin_addr) != 1) {
        fprintf(stderr, "bad destination IP: %s\n", opt.dst_ip);
        close(sock);
        return -1;
    }
    if (connect(sock, (const struct sockaddr *)&dst, sizeof(dst)) < 0) {
        perror("connect");
        close(sock);
        return -1;
    }
    return sock;
}

// ---------------------------------------------------------------------------
// Two-core sender.
//
// One core cannot pass ~56k packets/s: the kernel TX path costs ~14 us per
// 1280-byte packet and the cache invalidate adds ~2.6 us. The board has two
// Cortex-A9 cores and only one was sending.
//
// Each worker claims one batch at a time, in order, under a small lock. It then
// invalidates that batch's range (in its OWN L1 and the shared L2) and sends it
// from its OWN socket. Each core therefore only ever reads data it has just
// invalidated, which keeps the design correct without a coherent port.
//
// The writer's full-ring frontier is the first batch that is not yet complete,
// so a slot is only reused after BOTH workers are past it.
// ---------------------------------------------------------------------------
typedef struct {
    pthread_mutex_t  lock;
    int              sock[2];
    uint8_t         *ring;
    uint32_t        *ctrl;
    volatile uint8_t *fw;
    uint32_t         ring_slots;
    uint32_t         ring_mask;
    uint32_t         slot_stride;
    int              do_send;
    int              do_cache;
    xrt::bo         *ring_bo;
    uint64_t         claim_batch;    // next batch index to claim
    uint64_t         release_batch;  // first batch not yet complete
    uint64_t         done_flags;     // bitmap: batch index % 64 -> complete
    uint64_t         stat_pkts;
    uint64_t         stat_batches;
    uint64_t         stat_sync_ns;
    uint64_t         stat_send_ns;
    uint64_t         stat_spins;
} SendShared;

typedef struct {
    SendShared *sh;
    int         idx;
} WorkerArg;

static void *tx_worker(void *arg)
{
    WorkerArg *wa = (WorkerArg *)arg;
    SendShared *sh = wa->sh;
    const int idx = wa->idx;

    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(idx, &cpuset);
    if (pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset) != 0)
        perror("pthread_setaffinity (continuing)");
    // Default scheduling priority on purpose: with two senders both cores are
    // busy, and a negative nice value starves sshd and systemd (the board became
    // unreachable once).

    for (;;) {
        uint64_t batch_index;
        uint32_t batch_start;

        // Claim the next batch in order, only if the writer has published it.
        pthread_mutex_lock(&sh->lock);
        uint32_t produce = ctrl_load_acquire(&sh->ctrl[0]) & sh->ring_mask;
        batch_index = sh->claim_batch;
        batch_start = (uint32_t)((batch_index * MAX_GSO_SLOTS) & sh->ring_mask);
        uint32_t available = (produce - batch_start) & sh->ring_mask;
        if (available < MAX_GSO_SLOTS) {
            sh->stat_spins++;
            pthread_mutex_unlock(&sh->lock);
            continue;
        }
        sh->claim_batch++;
        pthread_mutex_unlock(&sh->lock);

        // Owned slot range is stable: the writer cannot pass the frontier.
        uint64_t sync_ns = 0;
        uint64_t send_ns = 0;
        for (;;) {
            uint32_t remaining = MAX_GSO_SLOTS;
            uint32_t cur = batch_start;
            int failed = 0;
            sync_ns = 0;
            send_ns = 0;

            while (remaining != 0) {
                uint32_t part_slots = sh->ring_slots - cur;
                if (part_slots > remaining)
                    part_slots = remaining;
                size_t byte_offset = (size_t)cur * sh->slot_stride;
                size_t part_bytes = (size_t)part_slots * sh->slot_stride;
                uint8_t *slot_ptr = sh->ring + byte_offset;

                if (sh->do_cache) {
                    uint64_t c0 = monotonic_ns();
                    sh->ring_bo->sync(XCL_BO_SYNC_BO_FROM_DEVICE,
                                      part_bytes, byte_offset);
                    sync_ns += monotonic_ns() - c0;
                }

                uint64_t s0 = monotonic_ns();
                int rc = send_gso(sh->sock[idx], (const void *)slot_ptr,
                                  part_bytes, sh->do_send);
                send_ns += monotonic_ns() - s0;
                if (rc != 0) {
                    failed = 1;
                    break;
                }

                remaining -= part_slots;
                cur = (cur + part_slots) & sh->ring_mask;
            }

            if (!failed)
                break;
            // Transient send failure (for example no receiver yet): hold this
            // batch and retry. The slot data cannot change while we own it.
            usleep(1000);
        }

        // Complete the batch and advance the reuse frontier.
        pthread_mutex_lock(&sh->lock);
        sh->done_flags |= (1ull << (batch_index % 64));
        while (sh->done_flags & (1ull << (sh->release_batch % 64))) {
            sh->done_flags &= ~(1ull << (sh->release_batch % 64));
            sh->release_batch++;
        }
        uint32_t frontier = (uint32_t)((sh->release_batch * MAX_GSO_SLOTS) & sh->ring_mask);
        ctrl_store_release(&sh->ctrl[1], frontier);
        wr32(sh->fw, REG_PS_CONSUME, frontier);
        sh->stat_pkts += MAX_GSO_SLOTS;
        sh->stat_batches++;
        sh->stat_sync_ns += sync_ns;
        sh->stat_send_ns += send_ns;
        pthread_mutex_unlock(&sh->lock);
    }
    return NULL;
}

// Only one sender may own the ring. Two instances fight over the same slots
// and over the network queues (the board became unreachable that way).
static int single_instance_guard(void)
{
    int fd = open("/var/lock/osv_tx_shim.lock", O_CREAT | O_RDWR, 0644);
    if (fd < 0)
        return 0;
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
        fprintf(stderr, "tx_shim: another sender instance already owns the ring; exiting\n");
        close(fd);
        return -1;
    }
    return fd;
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
    int guard_fd = single_instance_guard();
    if (guard_fd < 0)
        return 1;
    int force_sync = -1;   // -1 = follow the hardware flag, 1 = force, 0 = off
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--sync") == 0)
            force_sync = 1;
        else if (strcmp(argv[i], "--no-sync") == 0)
            force_sync = 0;
    }
    if (!do_send || !do_cache)
        printf("tx_shim: MEASURE MODE '%s' - network/cache operation disabled as selected\n",
               opt.mode);

    volatile uint8_t *fw = map_devmem(WRITER_BASE, 0x1000);
    volatile uint8_t *seq = map_devmem(SEQ_BASE, 0x1000);
    if (fw == MAP_FAILED || seq == MAP_FAILED)
        return 1;

    /* The coherent (ACP) build tells us that the PL's writes snoop the CPU
     * caches, so the per-batch invalidate can be skipped. An explicit
     * --sync/--no-sync overrides the flag. */
    if (force_sync == 0) {
        do_cache = 0;
        printf("tx_shim: cache maintenance disabled by --no-sync\n");
    } else if (force_sync == 1) {
        do_cache = 1;
        printf("tx_shim: cache maintenance forced on by --sync\n");
    } else if (rd32(fw, REG_COHERENT) & 1u) {
        do_cache = 0;
        printf("tx_shim: writer reports an ACP-coherent path; no cache maintenance needed\n");
    }

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
    // The writer keeps its PS-consume register across control enable/disable,
    // so a previous session's value can leave it in a permanent drop state
    // (produce+1 == stale consume). Initialize it to 0 before enabling.
    wr32(fw, REG_PS_CONSUME, 0u);
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

    int socks[2] = {-1, -1};
    if (do_send) {
        for (int i = 0; i < 2; i++) {
            socks[i] = make_tx_socket(opt, slot_stride);
            if (socks[i] < 0)
                return 1;
        }
        printf("tx_shim: UDP GSO segment=%u batch<=%u destination=%s:%u sockets=2 (one per core)\n",
               slot_stride, MAX_GSO_SLOTS, opt.dst_ip, opt.dst_port);
    }

    /* The worker threads pin themselves: worker 0 to CPU0, worker 1 to CPU1. */

    /* The configure-only daemon leaves the sequencer disabled until this
     * point, exactly as before: the writer must be enabled and ready to
     * drain before the AES pipeline can start producing packets. */
    wr32(seq, REG_SEQ_CONTROL, 1u);
    printf("tx_shim: sequencer enabled; draining ring\n");

    /* Hand the ring to two sender cores. Each worker claims batches in order,
     * invalidates its own range, and sends from its own socket. */
    SendShared sh;
    memset(&sh, 0, sizeof(sh));
    pthread_mutex_init(&sh.lock, NULL);
    sh.sock[0] = socks[0];
    sh.sock[1] = socks[1];
    sh.ring = ring;
    sh.ctrl = ctrl;
    sh.fw = fw;
    sh.ring_slots = ring_slots;
    sh.ring_mask = ring_mask;
    sh.slot_stride = slot_stride;
    sh.do_send = do_send;
    sh.do_cache = do_cache;
    sh.ring_bo = &ring_bo;

    WorkerArg wa[2];
    pthread_t tid[2];
    for (int i = 0; i < 2; i++) {
        wa[i].sh = &sh;
        wa[i].idx = i;
        if (pthread_create(&tid[i], NULL, tx_worker, &wa[i]) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    uint32_t drops_last = rd32(fw, REG_DROP_COUNT);
    uint64_t stat_start = monotonic_ns();
    for (;;) {
        sleep(1);
        uint64_t now = monotonic_ns();
        uint32_t drops_now = rd32(fw, REG_DROP_COUNT);
        uint32_t complete_now = rd32(fw, REG_COMPLETE_COUNT_LO);
        pthread_mutex_lock(&sh.lock);
        uint64_t pkts = sh.stat_pkts;          sh.stat_pkts = 0;
        uint64_t batches = sh.stat_batches;    sh.stat_batches = 0;
        uint64_t sync_ns = sh.stat_sync_ns;    sh.stat_sync_ns = 0;
        uint64_t send_ns = sh.stat_send_ns;    sh.stat_send_ns = 0;
        uint64_t spins = sh.stat_spins;        sh.stat_spins = 0;
        uint32_t frontier = (uint32_t)((sh.release_batch * MAX_GSO_SLOTS) & ring_mask);
        uint32_t produce_now = ctrl_load_acquire(&ctrl[0]) & ring_mask;
        pthread_mutex_unlock(&sh.lock);

        double seconds = (double)(now - stat_start) / 1e9;
        printf("tx_shim: pkts/s=%.1f batches/s=%.1f bo-sync-us=%.1f send-us=%.1f "
               "slot-bytes/s=%.0f drops=%u drops_delta=%u threads=2 "
               "produce=%u frontier=%u complete=%u spins=%" PRIu64 "\n",
               (double)pkts / seconds,
               (double)batches / seconds,
               (double)sync_ns / 1000.0,
               (double)send_ns / 1000.0,
               (double)pkts * slot_stride / seconds,
               drops_now, drops_now - drops_last,
               produce_now, frontier, complete_now, spins);
        stat_start = now;
        drops_last = drops_now;
    }

    return 0;
}
