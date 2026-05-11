#include "ring_api.h"

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
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
    struct stat st;
    RingHeader disk_header;
    size_t map_len = 0;
    bool have_disk_header = false;
    void *map_base = 0;
    RingHeader *header = 0;
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

    fd = open(dev_path, O_RDWR | O_CREAT, 0666);
    if (fd < 0) {
        return -1;
    }

    if (fstat(fd, &st) != 0) {
        close(fd);
        return -1;
    }

    memset(&disk_header, 0, sizeof(disk_header));
    if ((size_t)st.st_size >= sizeof(disk_header)) {
        ssize_t n = pread(fd, &disk_header, sizeof(disk_header), 0);
        if (n == (ssize_t)sizeof(disk_header) && is_valid_header(&disk_header)) {
            slot_count = disk_header.slot_count;
            slot_payload_bytes = disk_header.slot_payload_bytes;
            have_disk_header = true;
        }
    }

    map_len = ring_map_size(slot_count, slot_payload_bytes);
    if (map_len == 0) {
        close(fd);
        errno = EOVERFLOW;
        return -1;
    }

    if ((size_t)st.st_size < map_len) {
        if (ftruncate(fd, (off_t)map_len) != 0) {
            close(fd);
            return -1;
        }
    }

    map_base = mmap(0, map_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map_base == MAP_FAILED) {
        close(fd);
        return -1;
    }

    header = (RingHeader *)map_base;
    if (!have_disk_header || !is_valid_header(header) || header->slot_count != slot_count ||
        header->slot_payload_bytes != slot_payload_bytes) {
        memset(map_base, 0, map_len);
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
    ctx->slot_base = (void *)((uint8_t *)map_base + sizeof(RingHeader));
    ctx->payload_base =
        (uint8_t *)ctx->slot_base + ((size_t)ctx->slot_count * sizeof(RingSlot));
    ctx->write_index = &header->write_index;
    ctx->read_index = &header->read_index;

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
