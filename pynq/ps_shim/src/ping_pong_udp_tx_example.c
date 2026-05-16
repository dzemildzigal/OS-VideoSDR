#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/*
 * Minimal PS C consumer example for phase-1 ping-pong handoff.
 *
 * This is a template, not wired into CMake by default.
 * It assumes one UIO device with:
 *   map0: control registers
 *   map1: frame data aperture (buffer0 + buffer1)
 */

#define REG_VERSION         0x0000u
#define REG_CONTROL         0x0004u
#define REG_STATUS          0x0008u
#define REG_FRAME_BYTES_CFG 0x0010u
#define REG_WRITE_INDEX     0x0014u
#define REG_READY_MASK      0x0018u
#define REG_CONSUMED_MASK   0x001Cu
#define REG_FRAME_ID_BUF0   0x0020u
#define REG_FRAME_ID_BUF1   0x0024u
#define REG_VALID_BYTES_BUF0 0x0028u
#define REG_VALID_BYTES_BUF1 0x002Cu
#define REG_DROP_COUNT      0x0030u
#define REG_IRQ_ENABLE      0x0034u
#define REG_IRQ_STATUS      0x0038u

#define CTRL_ENABLE         (1u << 0)
#define CTRL_SOFT_RESET     (1u << 1)

#define READY_BUF0          (1u << 0)
#define READY_BUF1          (1u << 1)

#define IRQ_FRAME_READY     (1u << 0)

#define FRAME_BYTES_1080P_RGB888 6220800u
#define BUFFER_STRIDE_BYTES (16u * 1024u * 1024u)

static uint64_t monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000ULL) + ((uint64_t)ts.tv_nsec / 1000000ULL);
}

static uint32_t reg_read32(volatile uint8_t *base, uint32_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void reg_write32(volatile uint8_t *base, uint32_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

static int parse_uio_index(const char *path, uint32_t *out) {
    const char *base = strrchr(path, '/');
    char *end = 0;
    unsigned long v = 0;

    if (path == 0 || out == 0) {
        return -1;
    }

    base = (base != 0) ? (base + 1) : path;
    if (strncmp(base, "uio", 3) != 0) {
        return -1;
    }

    v = strtoul(base + 3, &end, 10);
    if (end == base + 3 || *end != '\0' || v > 0xFFFFFFFFUL) {
        return -1;
    }

    *out = (uint32_t)v;
    return 0;
}

static int read_uio_map_size(const char *uio_path, uint32_t map_index, size_t *size_out) {
    char p[256];
    char line[64];
    FILE *fp = 0;
    uint32_t idx = 0;
    char *end = 0;
    unsigned long long sz = 0;

    if (parse_uio_index(uio_path, &idx) != 0 || size_out == 0) {
        return -1;
    }

    snprintf(p, sizeof(p), "/sys/class/uio/uio%u/maps/map%u/size", (unsigned)idx, (unsigned)map_index);
    fp = fopen(p, "r");
    if (fp == 0) {
        return -1;
    }
    if (fgets(line, sizeof(line), fp) == 0) {
        fclose(fp);
        return -1;
    }
    fclose(fp);

    sz = strtoull(line, &end, 0);
    if (end == line || sz == 0ULL || sz > (unsigned long long)SIZE_MAX) {
        return -1;
    }

    *size_out = (size_t)sz;
    return 0;
}

static int send_frame_udp(int sock_fd,
                          const struct sockaddr_in *dst,
                          const uint8_t *frame,
                          uint32_t frame_bytes,
                          uint32_t segment_bytes) {
    uint32_t offset = 0;

    while (offset < frame_bytes) {
        uint32_t n = frame_bytes - offset;
        if (n > segment_bytes) {
            n = segment_bytes;
        }

        ssize_t sent = sendto(sock_fd,
                              frame + offset,
                              n,
                              0,
                              (const struct sockaddr *)dst,
                              sizeof(*dst));
        if (sent < 0 || (uint32_t)sent != n) {
            return -1;
        }

        offset += n;
    }

    return 0;
}

int main(int argc, char **argv) {
    const char *uio_dev = "/dev/uio2";
    const char *target_ip = "127.0.0.1";
    uint16_t target_port = 5000;
    uint32_t frame_bytes = FRAME_BYTES_1080P_RGB888;
    uint32_t segment_bytes = 1200;

    int fd = -1;
    int sock = -1;
    size_t map0_len = 0;
    size_t map1_len = 0;
    volatile uint8_t *regs = 0;
    uint8_t *data = 0;

    struct sockaddr_in dst;
    memset(&dst, 0, sizeof(dst));

    if (argc > 1) {
        uio_dev = argv[1];
    }

    if (read_uio_map_size(uio_dev, 0, &map0_len) != 0 || read_uio_map_size(uio_dev, 1, &map1_len) != 0) {
        fprintf(stderr, "failed to read uio map sizes for %s\n", uio_dev);
        return 1;
    }

    if (map1_len < (size_t)BUFFER_STRIDE_BYTES + (size_t)frame_bytes) {
        fprintf(stderr, "map1 too small for two-frame ping-pong layout\n");
        return 1;
    }

    fd = open(uio_dev, O_RDWR);
    if (fd < 0) {
        perror("open(uio)");
        return 1;
    }

    regs = (volatile uint8_t *)mmap(0, map0_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (regs == MAP_FAILED) {
        perror("mmap(map0)");
        close(fd);
        return 1;
    }

    data = (uint8_t *)mmap(0,
                           map1_len,
                           PROT_READ | PROT_WRITE,
                           MAP_SHARED,
                           fd,
                           (off_t)getpagesize()); /* map1 offset */
    if (data == MAP_FAILED) {
        perror("mmap(map1)");
        munmap((void *)regs, map0_len);
        close(fd);
        return 1;
    }

    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        munmap(data, map1_len);
        munmap((void *)regs, map0_len);
        close(fd);
        return 1;
    }

    dst.sin_family = AF_INET;
    dst.sin_port = htons(target_port);
    if (inet_pton(AF_INET, target_ip, &dst.sin_addr) != 1) {
        fprintf(stderr, "invalid target ip\n");
        close(sock);
        munmap(data, map1_len);
        munmap((void *)regs, map0_len);
        close(fd);
        return 1;
    }

    /* Enable pipeline and set expected frame size. */
    reg_write32(regs, REG_FRAME_BYTES_CFG, frame_bytes);
    reg_write32(regs, REG_IRQ_ENABLE, 0u); /* polling mode in this template */
    reg_write32(regs, REG_CONTROL, CTRL_ENABLE);

    printf("ping-pong tx start: uio=%s target=%s:%u frame_bytes=%u segment_bytes=%u\n",
           uio_dev,
           target_ip,
           (unsigned)target_port,
           (unsigned)frame_bytes,
           (unsigned)segment_bytes);

    uint64_t started = monotonic_ms();
    uint64_t last_print = started;
    uint64_t frames = 0;
    uint64_t bytes = 0;

    while (1) {
        uint32_t ready = reg_read32(regs, REG_READY_MASK);

        if (ready & READY_BUF0) {
            uint32_t valid = reg_read32(regs, REG_VALID_BYTES_BUF0);
            if (valid > frame_bytes) {
                valid = frame_bytes;
            }
            if (send_frame_udp(sock, &dst, data + 0u, valid, segment_bytes) != 0) {
                perror("send_frame_udp(buf0)");
                break;
            }
            reg_write32(regs, REG_CONSUMED_MASK, READY_BUF0); /* RW1C */
            frames += 1;
            bytes += valid;
        }

        if (ready & READY_BUF1) {
            uint32_t valid = reg_read32(regs, REG_VALID_BYTES_BUF1);
            if (valid > frame_bytes) {
                valid = frame_bytes;
            }
            if (send_frame_udp(sock, &dst, data + BUFFER_STRIDE_BYTES, valid, segment_bytes) != 0) {
                perror("send_frame_udp(buf1)");
                break;
            }
            reg_write32(regs, REG_CONSUMED_MASK, READY_BUF1); /* RW1C */
            frames += 1;
            bytes += valid;
        }

        uint64_t now = monotonic_ms();
        if (now - last_print >= 1000ULL) {
            double elapsed_s = (double)(now - started) / 1000.0;
            double mbps = (elapsed_s > 0.0) ? ((double)bytes * 8.0) / (elapsed_s * 1000000.0) : 0.0;
            uint32_t drops = reg_read32(regs, REG_DROP_COUNT);
            printf("ping-pong tx stats: frames=%" PRIu64 " throughput_mbps=%.2f drop_count=%u\n",
                   frames,
                   mbps,
                   (unsigned)drops);
            last_print = now;
        }
    }

    close(sock);
    munmap(data, map1_len);
    munmap((void *)regs, map0_len);
    close(fd);
    return 0;
}
