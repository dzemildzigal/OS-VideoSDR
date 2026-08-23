/*
 * tx_shim.c - C sender that replicates tx_daemon.py's send loop 1:1.
 *
 * Expected setup: tx_daemon.py runs with --configure-only. It loads the
 * overlay, programs the sequencer (key/nonce/session), configures the
 * frame_writer buffers, and then waits. This program reads the buffer
 * physical addresses back from the writer registers, mmaps them via
 * /dev/mem, and drains ready frames exactly like the Python loop:
 *
 *   mask = READY_MASK & 3
 *   both_ready = (mask & 3) == 3
 *   buf0 -> prefix = nonce_now - 2   (when both ready)
 *   buf1 -> prefix = nonce_now - 1   (always)
 *   datagram = 8-byte big-endian nonce prefix + buffer[0..nbytes)
 *
 * Wire format per packet: [8B nonce prefix][1216B CT][16B tag] = 1240B.
 */

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define WRITER_BASE 0x40000000u
#define SEQ_BASE    0x40001000u

#define REG_READY_MASK       0x0018u
#define REG_CONSUMED_MASK    0x001Cu
#define REG_VALID_BYTES_BUF0 0x0028u
#define REG_VALID_BYTES_BUF1 0x002Cu
#define REG_DROP_COUNT       0x0030u
#define REG_IRQ_STATUS       0x0038u
#define REG_FRAME_ID_BUF0    0x0020u
#define REG_FRAME_ID_BUF1    0x0024u
#define REG_BUF0_ADDR_LO     0x0044u
#define REG_BUF0_ADDR_HI     0x0048u
#define REG_BUF1_ADDR_LO     0x004Cu
#define REG_BUF1_ADDR_HI     0x0050u

#define REG_NONCE_HI 0x0040u
#define REG_NONCE_LO 0x0044u

#define REG_NONCE_SEED_HI 0x0014u
#define REG_NONCE_SEED_LO 0x0018u

#define MAX_FRAME_BYTES 4096u

#define __ARM_NR_cacheflush 0x0f0002u

static inline uint32_t rd32(volatile uint8_t *base, uint32_t off)
{
    return *(volatile uint32_t *)(base + off);
}

static inline void wr32(volatile uint8_t *base, uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(base + off) = val;
}

static void dcache_invalidate(void *start, size_t len)
{
    syscall(__ARM_NR_cacheflush, start, (char *)start + len, 0);
}

static volatile uint8_t *map_devmem(uint32_t phys, size_t len)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open(/dev/mem)");
        return MAP_FAILED;
    }
    off_t page = (off_t)(phys & ~0xFFFu);
    size_t span = len + (size_t)(phys & 0xFFFu);
    volatile uint8_t *p = (volatile uint8_t *)mmap(0, span,
                                                  PROT_READ | PROT_WRITE,
                                                  MAP_SHARED, fd, page);
    close(fd);
    if (p == MAP_FAILED) {
        perror("mmap(/dev/mem)");
        return MAP_FAILED;
    }
    return p + (phys & 0xFFFu);
}

static uint64_t monotonic_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    const char *dst_ip = "192.168.0.37";
    uint16_t dst_port = 5600;
    if (argc > 1) dst_ip = argv[1];
    if (argc > 2) dst_port = (uint16_t)atoi(argv[2]);

    volatile uint8_t *fw = map_devmem(WRITER_BASE, 0x1000);
    volatile uint8_t *seq = map_devmem(SEQ_BASE, 0x1000);
    if (fw == MAP_FAILED || seq == MAP_FAILED)
        return 1;

    uint32_t b0_lo = rd32(fw, REG_BUF0_ADDR_LO);
    uint32_t b0_hi = rd32(fw, REG_BUF0_ADDR_HI);
    uint32_t b1_lo = rd32(fw, REG_BUF1_ADDR_LO);
    uint32_t b1_hi = rd32(fw, REG_BUF1_ADDR_HI);
    uint64_t phys0 = ((uint64_t)b0_hi << 32) | b0_lo;
    uint64_t phys1 = ((uint64_t)b1_hi << 32) | b1_lo;
    printf("tx_shim: buf0 @ 0x%" PRIX64 "  buf1 @ 0x%" PRIX64 "\n", phys0, phys1);

    volatile uint8_t *buf[2];
    buf[0] = map_devmem((uint32_t)phys0, MAX_FRAME_BYTES);
    buf[1] = map_devmem((uint32_t)phys1, MAX_FRAME_BYTES);
    if (buf[0] == MAP_FAILED || buf[1] == MAP_FAILED)
        return 1;

    uint64_t nonce_seed =
        ((uint64_t)rd32(seq, REG_NONCE_SEED_HI) << 32) |
        (uint64_t)rd32(seq, REG_NONCE_SEED_LO);
    printf("tx_shim: nonce seed = %" PRIu64 "\n", nonce_seed);

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        return 1;
    }
    int sndbuf = 4 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    struct sockaddr_in dst;
    memset(&dst, 0, sizeof(dst));
    dst.sin_family = AF_INET;
    dst.sin_port = htons(dst_port);
    if (inet_pton(AF_INET, dst_ip, &dst.sin_addr) != 1) {
        fprintf(stderr, "bad dst ip %s\n", dst_ip);
        return 1;
    }

    printf("tx_shim: sending to %s:%u (1:1 daemon loop)\n", dst_ip, dst_port);
    fflush(stdout);

    /* Enable the sequencer. The configure-only daemon left it disabled so
     * the nonce counter could not run ahead of the writer while nobody was
     * draining. The FSM processes the pending key load first, then the
     * session path - safe to enable here, right before the drain loop. */
    printf("tx_shim: enabling sequencer\n");
    fflush(stdout);
    wr32(seq, 0x00u, 1u);

    uint8_t stage[8 + MAX_FRAME_BYTES];
    uint64_t frames = 0, bytes = 0;
    uint64_t debug_prefixes = 0;
    uint32_t drops_last = rd32(fw, REG_DROP_COUNT);
    uint64_t started = monotonic_ms(), last_print = started;

    for (;;) {
        uint32_t mask = rd32(fw, REG_READY_MASK) & 0x3u;
        if (mask == 0) {
            usleep(10);
        } else {
            for (int idx = 0; idx < 2; idx++) {
                if (!(mask & (1u << idx)))
                    continue;

                uint32_t frame_id = rd32(fw, idx == 0 ? REG_FRAME_ID_BUF0
                                                      : REG_FRAME_ID_BUF1);
                uint32_t nbytes = rd32(fw, idx == 0 ? REG_VALID_BYTES_BUF0
                                                    : REG_VALID_BYTES_BUF1);
                uint64_t nonce_pkt = nonce_seed + (uint64_t)frame_id;

                if (debug_prefixes < 12) {
                    printf("tx_shim: buf%d frame_id=%" PRIu32
                           " nonce=%" PRIu64 " bytes=%" PRIu32 "\n",
                           idx, frame_id, nonce_pkt, nbytes);
                    debug_prefixes++;
                }

                for (int b = 0; b < 8; b++)
                    stage[b] = (uint8_t)(nonce_pkt >> (56 - 8 * b));
                if (nbytes == 0) {
                    wr32(fw, REG_CONSUMED_MASK, 1u << idx);
                    continue;
                }
                if (nbytes > MAX_FRAME_BYTES)
                    nbytes = MAX_FRAME_BYTES;

                dcache_invalidate((void *)buf[idx], nbytes);
                memcpy(stage + 8, (const void *)buf[idx], nbytes);

                ssize_t sent = sendto(sock, stage, 8 + nbytes, 0,
                                      (const struct sockaddr *)&dst,
                                      sizeof(dst));
                if (sent < 0) {
                    /* ECONNREFUSED fires when the PC has no listener and its
                     * ICMP port-unreachable arrives; NEVER exit - keep
                     * draining so the pipeline stays 1:1. */
                    perror("sendto");
                } else {
                    wr32(fw, REG_IRQ_STATUS, 1u);            /* clear_irq (RW1C) */
                    wr32(fw, REG_CONSUMED_MASK, 1u << idx);  /* mark consumed   */
                    frames++;
                    bytes += (uint64_t)sent;
                }
            }
        }

        uint64_t now = monotonic_ms();
        if (now - last_print >= 1000ULL) {
            uint32_t drops_now = rd32(fw, REG_DROP_COUNT);
            printf("tx_shim: frames=%" PRIu64 " bytes=%" PRIu64
                   " drops_delta=%u\n",
                   frames, bytes, drops_now - drops_last);
            drops_last = drops_now;
            last_print = now;
        }
    }

    return 0;
}
