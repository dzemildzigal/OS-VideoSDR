#include "ring_api.h"

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define RING_MAGIC 0x4f535652u
#define RING_VERSION 1u

#define SLOT_EMPTY 0u
#define SLOT_FULL 1u

#define DEFAULT_SLOT_COUNT 2048u
#define DEFAULT_SLOT_PAYLOAD_BYTES 2048u
#define DEFAULT_TIMEOUT_MS 250u
#define POLL_SLEEP_US 50u

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t slot_count;
    uint32_t slot_payload_bytes;
    volatile uint32_t write_index;
    volatile uint32_t read_index;
    uint32_t reserved0;
    uint32_t reserved1;
} RingHeader;

typedef struct {
    volatile uint32_t state;
    uint32_t reserved;
    RingDescriptor desc;
} RingSlot;

static uint64_t monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000ULL) + ((uint64_t)ts.tv_nsec / 1000000ULL);
}

static uint32_t parse_env_u32(const char *name, uint32_t fallback) {
    const char *value = getenv(name);
    char *end = 0;
    unsigned long parsed = 0;

    if (value == 0 || *value == '\0') {
        return fallback;
    }

    parsed = strtoul(value, &end, 10);
    if (end == value || *end != '\0' || parsed == 0 || parsed > 0xFFFFFFFFUL) {
        return fallback;
    }

    return (uint32_t)parsed;
}

static bool path_looks_like_uio(const char *path) {
    const char *base = 0;
    if (path == 0) {
        return false;
    }

    base = strrchr(path, '/');
    if (base != 0) {
        base += 1;
    } else {
        base = path;
    }

    return strncmp(base, "uio", 3) == 0;
}

static int parse_uio_index(const char *path, uint32_t *index_out) {
    const char *base = 0;
    char *end = 0;
    unsigned long value = 0;

    if (path == 0 || index_out == 0) {
        errno = EINVAL;
        return -1;
    }

    base = strrchr(path, '/');
    if (base != 0) {
        base += 1;
    } else {
        base = path;
    }

    if (strncmp(base, "uio", 3) != 0) {
        errno = EINVAL;
        return -1;
    }

    value = strtoul(base + 3, &end, 10);
    if (end == base + 3 || *end != '\0' || value > 0xFFFFFFFFUL) {
        errno = EINVAL;
        return -1;
    }

    *index_out = (uint32_t)value;
    return 0;
}

static int read_uio_map_size(const char *uio_path, uint32_t map_index, size_t *size_out) {
    char sysfs_path[256];
    char content[64];
    FILE *fp = 0;
    uint32_t uio_index = 0;
    char *end = 0;
    unsigned long long size_value = 0;

    if (uio_path == 0 || size_out == 0) {
        errno = EINVAL;
        return -1;
    }

    if (parse_uio_index(uio_path, &uio_index) != 0) {
        return -1;
    }

    snprintf(sysfs_path,
             sizeof(sysfs_path),
             "/sys/class/uio/uio%u/maps/map%u/size",
             (unsigned)uio_index,
             (unsigned)map_index);

    fp = fopen(sysfs_path, "r");
    if (fp == 0) {
        return -1;
    }

    if (fgets(content, sizeof(content), fp) == 0) {
        fclose(fp);
        errno = EIO;
        return -1;
    }
    fclose(fp);

    size_value = strtoull(content, &end, 0);
    if (end == content || size_value == 0ULL || size_value > (unsigned long long)SIZE_MAX) {
        errno = EINVAL;
        return -1;
    }

    *size_out = (size_t)size_value;
    return 0;
}

static bool is_valid_header(const RingHeader *header) {
    if (header == 0) {
        return false;
    }

    if (header->magic != RING_MAGIC || header->version != RING_VERSION) {
        return false;
    }

    if (header->slot_count == 0 || header->slot_payload_bytes == 0) {
        return false;
    }

    return true;
}

static size_t ring_map_size(uint32_t slot_count, uint32_t slot_payload_bytes) {
    size_t slots_size = 0;
    size_t payload_size = 0;
    size_t map_size = 0;

    if (slot_count == 0 || slot_payload_bytes == 0) {
        return 0;
    }

    if (slot_count > (SIZE_MAX / sizeof(RingSlot))) {
        return 0;
    }
    slots_size = (size_t)slot_count * sizeof(RingSlot);

    if (slot_count > (SIZE_MAX / (size_t)slot_payload_bytes)) {
        return 0;
    }
    payload_size = (size_t)slot_count * (size_t)slot_payload_bytes;

    if (sizeof(RingHeader) > (SIZE_MAX - slots_size)) {
        return 0;
    }
    map_size = sizeof(RingHeader) + slots_size;

    if (payload_size > (SIZE_MAX - map_size)) {
        return 0;
    }
    map_size += payload_size;

    return map_size;
}

static RingSlot *ring_slots(const RingContext *ctx) {
    return (RingSlot *)ctx->slot_base;
}

int ring_open(RingContext *ctx, const char *dev_path, int is_tx) {
    uint32_t slot_count = parse_env_u32("OSV_RING_SLOT_COUNT", DEFAULT_SLOT_COUNT);
    uint32_t slot_payload_bytes = parse_env_u32("OSV_RING_SLOT_PAYLOAD_BYTES", DEFAULT_SLOT_PAYLOAD_BYTES);
    uint32_t uio_map_index = parse_env_u32("OSV_RING_UIO_MAP_INDEX", 0);
    uint32_t uio_ring_offset = parse_env_u32("OSV_RING_UIO_RING_OFFSET", 0);
    bool uio_allow_reset = parse_env_u32("OSV_RING_UIO_ALLOW_RESET", 0) > 0;
    bool debug_enabled = parse_env_u32("OSV_RING_DEBUG", 0) > 0;
    struct stat st;
    RingHeader disk_header;
    RingHeader *header = 0;
    size_t map_len = 0;
    size_t mmap_offset = 0;
    size_t ring_offset = 0;
    size_t available_len = 0;
    size_t needed_len = 0;
    bool have_disk_header = false;
    bool should_reset_ring = false;
    bool is_char_device = false;
    void *map_base = 0;
    int open_flags = O_RDWR;
    int fd = -1;

    if (ctx == 0 || dev_path == 0) {
        errno = EINVAL;
        return -1;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->fd = -1;

    if (slot_count < 2) {
        slot_count = 2;
    }
    if (slot_payload_bytes < 256) {
        slot_payload_bytes = 256;
    }

    if (!path_looks_like_uio(dev_path)) {
        open_flags |= O_CREAT;
    }

    fd = open(dev_path, open_flags, 0666);
    if (fd < 0) {
        return -1;
    }

    if (fstat(fd, &st) != 0) {
        close(fd);
        return -1;
    }

    is_char_device = S_ISCHR(st.st_mode);

    memset(&disk_header, 0, sizeof(disk_header));
    if (!is_char_device && (size_t)st.st_size >= sizeof(disk_header)) {
        ssize_t n = pread(fd, &disk_header, sizeof(disk_header), 0);
        if (n == (ssize_t)sizeof(disk_header) && is_valid_header(&disk_header)) {
            slot_count = disk_header.slot_count;
            slot_payload_bytes = disk_header.slot_payload_bytes;
            have_disk_header = true;
        }
    }

    if (is_char_device) {
        if (read_uio_map_size(dev_path, uio_map_index, &map_len) != 0) {
            perror("read_uio_map_size");
            close(fd);
            return -1;
        }
        mmap_offset = (size_t)getpagesize() * (size_t)uio_map_index;
        ring_offset = (size_t)uio_ring_offset;
    } else {
        map_len = ring_map_size(slot_count, slot_payload_bytes);
        if (map_len == 0) {
            close(fd);
            errno = EOVERFLOW;
            return -1;
        }
    }

    if (!is_char_device) {
        if ((size_t)st.st_size < map_len) {
            if (ftruncate(fd, (off_t)map_len) != 0) {
                close(fd);
                return -1;
            }
        }
    }

    map_base = mmap(0, map_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)mmap_offset);
    if (map_base == MAP_FAILED) {
        close(fd);
        return -1;
    }

    if (ring_offset > map_len || (map_len - ring_offset) < sizeof(RingHeader)) {
        fprintf(stderr,
                "ring_open: map too small for header (map_len=%zu ring_offset=%zu)\n",
                map_len,
                ring_offset);
        munmap(map_base, map_len);
        close(fd);
        errno = ENOSPC;
        return -1;
    }

    available_len = map_len - ring_offset;
    header = (RingHeader *)((uint8_t *)map_base + ring_offset);

    needed_len = ring_map_size(slot_count, slot_payload_bytes);
    if (needed_len == 0 || needed_len > available_len) {
        size_t per_slot = sizeof(RingSlot) + (size_t)slot_payload_bytes;
        size_t max_slots = 0;

        if (available_len > sizeof(RingHeader) && per_slot > 0) {
            max_slots = (available_len - sizeof(RingHeader)) / per_slot;
        }
        if (max_slots < 2) {
            fprintf(stderr,
                    "ring_open: map too small (available=%zu needed=%zu slot_payload=%u)\n",
                    available_len,
                    needed_len,
                    (unsigned)slot_payload_bytes);
            munmap(map_base, map_len);
            close(fd);
            errno = ENOSPC;
            return -1;
        }

        slot_count = (uint32_t)max_slots;
        needed_len = ring_map_size(slot_count, slot_payload_bytes);
    }

    should_reset_ring = !is_valid_header(header) || header->slot_count != slot_count ||
                        header->slot_payload_bytes != slot_payload_bytes;

    if (!is_char_device && !have_disk_header) {
        should_reset_ring = true;
    }

    if (should_reset_ring && is_char_device && !uio_allow_reset) {
        fprintf(stderr,
                "ring_open: refusing to initialize char-device mapping %s because ring header is invalid "
                "or layout mismatched; set OSV_RING_UIO_ALLOW_RESET=1 only for dedicated ring memory\n",
                dev_path);
        munmap(map_base, map_len);
        close(fd);
        errno = ENODEV;
        return -1;
    }

    if (should_reset_ring) {
        memset((void *)header, 0, needed_len);
        header->magic = RING_MAGIC;
        header->version = RING_VERSION;
        header->slot_count = slot_count;
        header->slot_payload_bytes = slot_payload_bytes;
        header->write_index = 0;
        header->read_index = 0;
    }

    ctx->fd = fd;
    ctx->is_tx = is_tx;
    ctx->dev_path = dev_path;
    ctx->map_base = map_base;
    ctx->map_len = map_len;
    ctx->slot_count = header->slot_count;
    ctx->slot_payload_bytes = header->slot_payload_bytes;
    ctx->timeout_ms = parse_env_u32("OSV_RING_TIMEOUT_MS", DEFAULT_TIMEOUT_MS);
    ctx->slot_base = (void *)((uint8_t *)header + sizeof(RingHeader));
    ctx->payload_base =
        (uint8_t *)ctx->slot_base + ((size_t)ctx->slot_count * sizeof(RingSlot));
    ctx->write_index = &header->write_index;
    ctx->read_index = &header->read_index;

    if (debug_enabled) {
        fprintf(stderr,
                "ring_open: dev=%s char=%d map_len=%zu map_index=%u ring_offset=%zu "
                "slot_count=%u slot_payload=%u needed_len=%zu reset=%d uio_allow_reset=%d\n",
                dev_path,
                is_char_device ? 1 : 0,
                map_len,
                (unsigned)uio_map_index,
                ring_offset,
                (unsigned)ctx->slot_count,
                (unsigned)ctx->slot_payload_bytes,
                needed_len,
                should_reset_ring ? 1 : 0,
                uio_allow_reset ? 1 : 0);
    }

    return 0;
}

int ring_set_timeout_ms(RingContext *ctx, uint32_t timeout_ms) {
    if (ctx == 0) {
        errno = EINVAL;
        return -1;
    }

    ctx->timeout_ms = timeout_ms;
    return 0;
}

uint32_t ring_slot_payload_bytes(const RingContext *ctx) {
    if (ctx == 0) {
        return 0;
    }

    return ctx->slot_payload_bytes;
}

int ring_push(RingContext *ctx, const RingDescriptor *desc) {
    const uint8_t *src = 0;
    uint64_t started_ms = 0;

    if (ctx == 0 || desc == 0 || ctx->slot_base == 0 || ctx->payload_base == 0 ||
        ctx->write_index == 0 || ctx->read_index == 0 || ctx->slot_count == 0 ||
        ctx->slot_payload_bytes == 0) {
        errno = EINVAL;
        return -1;
    }

    if (desc->payload_len > ctx->slot_payload_bytes) {
        errno = EMSGSIZE;
        return -1;
    }

    if (desc->payload_len > 0 && desc->buffer_addr == 0) {
        errno = EINVAL;
        return -1;
    }

    src = (const uint8_t *)(uintptr_t)desc->buffer_addr;
    started_ms = monotonic_ms();

    while (1) {
        uint32_t write_index = *ctx->write_index;
        uint32_t slot_index = write_index % ctx->slot_count;
        RingSlot *slot = &ring_slots(ctx)[slot_index];

        if (slot->state == SLOT_EMPTY) {
            RingDescriptor local_desc = *desc;
            uint8_t *dst =
                ctx->payload_base + ((size_t)slot_index * ctx->slot_payload_bytes);

            if (local_desc.payload_len > 0) {
                memcpy(dst, src, local_desc.payload_len);
            }
            local_desc.buffer_addr = (uint64_t)(uintptr_t)dst;

            __sync_synchronize();
            slot->desc = local_desc;
            __sync_synchronize();
            slot->state = SLOT_FULL;
            __sync_synchronize();
            *ctx->write_index = (write_index + 1U) % ctx->slot_count;
            return 0;
        }

        if (ctx->timeout_ms > 0 && (monotonic_ms() - started_ms) >= ctx->timeout_ms) {
            errno = EAGAIN;
            return -1;
        }

        usleep(POLL_SLEEP_US);
    }
}

int ring_pop(RingContext *ctx, RingDescriptor *desc) {
    uint64_t started_ms = 0;

    if (ctx == 0 || desc == 0 || ctx->slot_base == 0 || ctx->payload_base == 0 ||
        ctx->write_index == 0 || ctx->read_index == 0 || ctx->slot_count == 0 ||
        ctx->slot_payload_bytes == 0) {
        errno = EINVAL;
        return -1;
    }

    started_ms = monotonic_ms();

    while (1) {
        uint32_t read_index = *ctx->read_index;
        uint32_t slot_index = read_index % ctx->slot_count;
        RingSlot *slot = &ring_slots(ctx)[slot_index];

        if (slot->state == SLOT_FULL) {
            RingDescriptor local_desc = slot->desc;
            uint8_t *src =
                ctx->payload_base + ((size_t)slot_index * ctx->slot_payload_bytes);

            if (local_desc.payload_len > ctx->slot_payload_bytes) {
                errno = EIO;
                return -1;
            }

            local_desc.buffer_addr = (uint64_t)(uintptr_t)src;

            __sync_synchronize();
            slot->state = SLOT_EMPTY;
            __sync_synchronize();
            *ctx->read_index = (read_index + 1U) % ctx->slot_count;
            *desc = local_desc;
            return 0;
        }

        if (ctx->timeout_ms > 0 && (monotonic_ms() - started_ms) >= ctx->timeout_ms) {
            errno = EAGAIN;
            return -1;
        }

        usleep(POLL_SLEEP_US);
    }
}

void ring_close(RingContext *ctx) {
    if (ctx == 0) {
        return;
    }

    if (ctx->map_base != 0 && ctx->map_len > 0) {
        munmap(ctx->map_base, ctx->map_len);
    }

    if (ctx->fd >= 0) {
        close(ctx->fd);
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->fd = -1;
}
