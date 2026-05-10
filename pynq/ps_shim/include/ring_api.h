#ifndef OS_VIDEOSDR_RING_API_H
#define OS_VIDEOSDR_RING_API_H

#include <stdint.h>

typedef struct {
    uint64_t buffer_addr;
    uint32_t payload_len;
    uint32_t session_id;
    uint16_t stream_id;
    uint32_t frame_id;
    uint16_t segment_id;
    uint16_t segment_count;
    uint64_t nonce_counter;
    uint8_t key_id;
    uint8_t flags;
    uint64_t timestamp_ns;
} RingDescriptor;

typedef struct {
    int fd;
    int is_tx;
    const char *dev_path;
} RingContext;

int ring_open(RingContext *ctx, const char *dev_path, int is_tx);
int ring_pop(RingContext *ctx, RingDescriptor *desc);
int ring_push(RingContext *ctx, const RingDescriptor *desc);
void ring_close(RingContext *ctx);

#endif
