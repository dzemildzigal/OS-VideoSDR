#include "ring_api.h"

#include <errno.h>

int ring_open(RingContext *ctx, const char *dev_path, int is_tx) {
    if (ctx == 0 || dev_path == 0) {
        errno = EINVAL;
        return -1;
    }

    ctx->fd = -1;
    ctx->is_tx = is_tx;
    ctx->dev_path = dev_path;

    errno = ENOSYS;
    return -1;
}

int ring_pop(RingContext *ctx, RingDescriptor *desc) {
    (void)ctx;
    (void)desc;
    errno = ENOSYS;
    return -1;
}

int ring_push(RingContext *ctx, const RingDescriptor *desc) {
    (void)ctx;
    (void)desc;
    errno = ENOSYS;
    return -1;
}

void ring_close(RingContext *ctx) {
    if (ctx == 0) {
        return;
    }

    ctx->fd = -1;
}
