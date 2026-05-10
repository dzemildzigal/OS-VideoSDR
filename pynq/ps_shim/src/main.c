#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef enum {
    MODE_TX,
    MODE_RX,
} RunMode;

typedef struct {
    RunMode mode;
    const char *bind_ip;
    const char *target_ip;
    uint16_t port;
    uint32_t frames;
    uint32_t fps;
    uint32_t frame_bytes;
    uint32_t segment_bytes;
    uint32_t inter_packet_gap_us;
    uint32_t send_buffer_bytes;
    uint32_t recv_buffer_bytes;
    uint32_t max_runtime_s;
    uint32_t socket_timeout_ms;
    uint32_t print_interval_ms;
} Options;

static uint64_t monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000ULL) + ((uint64_t)ts.tv_nsec / 1000000ULL);
}

static uint64_t monotonic_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000000ULL) + ((uint64_t)ts.tv_nsec / 1000ULL);
}

static uint32_t parse_u32(const char *value, const char *name) {
    char *end = 0;
    unsigned long parsed = strtoul(value, &end, 10);
    if (end == value || *end != '\0' || parsed > 0xFFFFFFFFUL) {
        fprintf(stderr, "invalid %s: %s\n", name, value);
        exit(2);
    }
    return (uint32_t)parsed;
}

static void print_usage(const char *prog) {
    printf("Usage: %s --mode tx|rx [options]\n", prog);
    printf("Options:\n");
    printf("  --mode tx|rx\n");
    printf("  --bind-ip <ip>                (default 0.0.0.0)\n");
    printf("  --target-ip <ip>              (tx only, default 127.0.0.1)\n");
    printf("  --port <port>                 (default 5000)\n");
    printf("  --frames <n>                  (tx target frame count, default 0=until runtime)\n");
    printf("  --fps <n>                     (tx frame rate, default 1)\n");
    printf("  --frame-bytes <n>             (default 120000)\n");
    printf("  --segment-bytes <n>           (default 1200)\n");
    printf("  --inter-packet-gap-us <n>     (default 0)\n");
    printf("  --send-buffer-bytes <n>       (default 8388608)\n");
    printf("  --recv-buffer-bytes <n>       (default 8388608)\n");
    printf("  --max-runtime-s <n>           (default 30)\n");
    printf("  --socket-timeout-ms <n>       (default 250)\n");
    printf("  --print-interval-ms <n>       (default 1000)\n");
}

static Options parse_args(int argc, char **argv) {
    Options opt;
    bool mode_set = false;
    opt.mode = MODE_TX;
    opt.bind_ip = "0.0.0.0";
    opt.target_ip = "127.0.0.1";
    opt.port = 5000;
    opt.frames = 0;
    opt.fps = 1;
    opt.frame_bytes = 120000;
    opt.segment_bytes = 1200;
    opt.inter_packet_gap_us = 0;
    opt.send_buffer_bytes = 8U * 1024U * 1024U;
    opt.recv_buffer_bytes = 8U * 1024U * 1024U;
    opt.max_runtime_s = 30;
    opt.socket_timeout_ms = 250;
    opt.print_interval_ms = 1000;

    for (int i = 1; i < argc; ++i) {
        const char *a = argv[i];

        if (strcmp(a, "--help") == 0) {
            print_usage(argv[0]);
            exit(0);
        }

        if (strcmp(a, "--mode") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--mode requires value\n");
                exit(2);
            }
            if (strcmp(argv[i], "tx") == 0) {
                opt.mode = MODE_TX;
            } else if (strcmp(argv[i], "rx") == 0) {
                opt.mode = MODE_RX;
            } else {
                fprintf(stderr, "unsupported mode: %s\n", argv[i]);
                exit(2);
            }
            mode_set = true;
            continue;
        }

        if (strcmp(a, "--bind-ip") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--bind-ip requires value\n");
                exit(2);
            }
            opt.bind_ip = argv[i];
            continue;
        }

        if (strcmp(a, "--target-ip") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--target-ip requires value\n");
                exit(2);
            }
            opt.target_ip = argv[i];
            continue;
        }

        if (strcmp(a, "--port") == 0) {
            uint32_t port = 0;
            if (++i >= argc) {
                fprintf(stderr, "--port requires value\n");
                exit(2);
            }
            port = parse_u32(argv[i], "port");
            if (port == 0 || port > 65535U) {
                fprintf(stderr, "port must be 1..65535\n");
                exit(2);
            }
            opt.port = (uint16_t)port;
            continue;
        }

        if (strcmp(a, "--frames") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--frames requires value\n");
                exit(2);
            }
            opt.frames = parse_u32(argv[i], "frames");
            continue;
        }

        if (strcmp(a, "--fps") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--fps requires value\n");
                exit(2);
            }
            opt.fps = parse_u32(argv[i], "fps");
            continue;
        }

        if (strcmp(a, "--frame-bytes") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--frame-bytes requires value\n");
                exit(2);
            }
            opt.frame_bytes = parse_u32(argv[i], "frame-bytes");
            continue;
        }

        if (strcmp(a, "--segment-bytes") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--segment-bytes requires value\n");
                exit(2);
            }
            opt.segment_bytes = parse_u32(argv[i], "segment-bytes");
            continue;
        }

        if (strcmp(a, "--inter-packet-gap-us") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--inter-packet-gap-us requires value\n");
                exit(2);
            }
            opt.inter_packet_gap_us = parse_u32(argv[i], "inter-packet-gap-us");
            continue;
        }

        if (strcmp(a, "--send-buffer-bytes") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--send-buffer-bytes requires value\n");
                exit(2);
            }
            opt.send_buffer_bytes = parse_u32(argv[i], "send-buffer-bytes");
            continue;
        }

        if (strcmp(a, "--recv-buffer-bytes") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--recv-buffer-bytes requires value\n");
                exit(2);
            }
            opt.recv_buffer_bytes = parse_u32(argv[i], "recv-buffer-bytes");
            continue;
        }

        if (strcmp(a, "--max-runtime-s") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--max-runtime-s requires value\n");
                exit(2);
            }
            opt.max_runtime_s = parse_u32(argv[i], "max-runtime-s");
            continue;
        }

        if (strcmp(a, "--socket-timeout-ms") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--socket-timeout-ms requires value\n");
                exit(2);
            }
            opt.socket_timeout_ms = parse_u32(argv[i], "socket-timeout-ms");
            continue;
        }

        if (strcmp(a, "--print-interval-ms") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--print-interval-ms requires value\n");
                exit(2);
            }
            opt.print_interval_ms = parse_u32(argv[i], "print-interval-ms");
            continue;
        }

        fprintf(stderr, "unknown argument: %s\n", a);
        exit(2);
    }

    if (opt.fps == 0) {
        fprintf(stderr, "fps must be > 0\n");
        exit(2);
    }
    if (opt.segment_bytes == 0) {
        fprintf(stderr, "segment-bytes must be > 0\n");
        exit(2);
    }
    if (opt.frame_bytes == 0) {
        fprintf(stderr, "frame-bytes must be > 0\n");
        exit(2);
    }
    if (!mode_set) {
        fprintf(stderr, "--mode tx|rx is required\n");
        exit(2);
    }

    return opt;
}

static int make_socket(const Options *opt) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }

    if (opt->mode == MODE_TX) {
        int snd = (int)opt->send_buffer_bytes;
        if (setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &snd, sizeof(snd)) != 0) {
            perror("setsockopt(SO_SNDBUF)");
        }
    } else {
        int rcv = (int)opt->recv_buffer_bytes;
        if (setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof(rcv)) != 0) {
            perror("setsockopt(SO_RCVBUF)");
        }

        struct timeval tv;
        tv.tv_sec = (time_t)(opt->socket_timeout_ms / 1000U);
        tv.tv_usec = (suseconds_t)((opt->socket_timeout_ms % 1000U) * 1000U);
        if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) != 0) {
            perror("setsockopt(SO_RCVTIMEO)");
        }
    }

    return fd;
}

static int bind_socket(int fd, const char *ip, uint16_t port) {
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &addr.sin_addr) != 1) {
        fprintf(stderr, "invalid bind ip: %s\n", ip);
        return -1;
    }

    if (bind(fd, (const struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("bind");
        return -1;
    }
    return 0;
}

static int run_tx(const Options *opt) {
    int fd = make_socket(opt);
    if (fd < 0) {
        return 1;
    }

    if (strcmp(opt->bind_ip, "0.0.0.0") != 0) {
        if (bind_socket(fd, opt->bind_ip, 0) != 0) {
            close(fd);
            return 1;
        }
    }

    struct sockaddr_in target;
    memset(&target, 0, sizeof(target));
    target.sin_family = AF_INET;
    target.sin_port = htons(opt->port);
    if (inet_pton(AF_INET, opt->target_ip, &target.sin_addr) != 1) {
        fprintf(stderr, "invalid target ip: %s\n", opt->target_ip);
        close(fd);
        return 1;
    }

    uint8_t *segment = (uint8_t *)malloc(opt->segment_bytes);
    if (segment == 0) {
        fprintf(stderr, "malloc failed for segment buffer\n");
        close(fd);
        return 1;
    }
    memset(segment, 0xA5, opt->segment_bytes);

    const uint32_t segment_count =
        (opt->frame_bytes + opt->segment_bytes - 1U) / opt->segment_bytes;

    uint64_t started_ms = monotonic_ms();
    uint64_t started_us = monotonic_us();
    uint64_t last_print_ms = started_ms;
    uint64_t next_frame_us = started_us;
    uint64_t packets_tx = 0;
    uint64_t bytes_tx = 0;
    uint32_t frames_tx = 0;
    uint64_t frame_period_us = 1000000ULL / (uint64_t)opt->fps;

    if (frame_period_us == 0) {
        frame_period_us = 1;
    }

    printf("ps_shim tx start: target=%s:%u fps=%u frame_bytes=%u segment_bytes=%u segments_per_frame=%u\n",
           opt->target_ip,
           (unsigned)opt->port,
           (unsigned)opt->fps,
           (unsigned)opt->frame_bytes,
           (unsigned)opt->segment_bytes,
           (unsigned)segment_count);

    while (1) {
        uint64_t now_ms = monotonic_ms();
        if (opt->max_runtime_s > 0 && (now_ms - started_ms) >= ((uint64_t)opt->max_runtime_s * 1000ULL)) {
            break;
        }
        if (opt->frames > 0 && frames_tx >= opt->frames) {
            break;
        }

        uint32_t remaining = opt->frame_bytes;
        for (uint32_t seg = 0; seg < segment_count; ++seg) {
            uint32_t payload = remaining > opt->segment_bytes ? opt->segment_bytes : remaining;

            if (payload >= 16U) {
                memcpy(segment + 0, &frames_tx, sizeof(frames_tx));
                memcpy(segment + 4, &seg, sizeof(seg));
                memcpy(segment + 8, &segment_count, sizeof(segment_count));
                memcpy(segment + 12, &payload, sizeof(payload));
            }

            ssize_t sent = sendto(
                fd,
                segment,
                payload,
                0,
                (const struct sockaddr *)&target,
                sizeof(target));
            if (sent < 0) {
                perror("sendto");
                free(segment);
                close(fd);
                return 1;
            }

            packets_tx += 1ULL;
            bytes_tx += (uint64_t)sent;
            remaining -= payload;

            if (opt->inter_packet_gap_us > 0) {
                usleep(opt->inter_packet_gap_us);
            }
        }

        frames_tx += 1U;

        now_ms = monotonic_ms();
        if ((now_ms - last_print_ms) >= opt->print_interval_ms) {
            double elapsed_s = (double)(now_ms - started_ms) / 1000.0;
            double mbps = elapsed_s > 0.0 ? ((double)bytes_tx * 8.0) / (elapsed_s * 1000000.0) : 0.0;
            printf("ps_shim tx stats: frames=%u packets=%" PRIu64 " throughput_mbps=%.2f\n",
                   (unsigned)frames_tx,
                   packets_tx,
                   mbps);
            last_print_ms = now_ms;
        }

        uint64_t now_us;
        next_frame_us += frame_period_us;
        now_us = monotonic_us();
        if (next_frame_us > now_us) {
            usleep((useconds_t)(next_frame_us - now_us));
        }
    }

    uint64_t done_ms = monotonic_ms();
    double elapsed_s = (double)(done_ms - started_ms) / 1000.0;
    double mbps = elapsed_s > 0.0 ? ((double)bytes_tx * 8.0) / (elapsed_s * 1000000.0) : 0.0;
    printf("ps_shim tx done: frames=%u packets=%" PRIu64 " throughput_mbps=%.2f elapsed_s=%.1f\n",
           (unsigned)frames_tx,
           packets_tx,
           mbps,
           elapsed_s);

    free(segment);
    close(fd);
    return 0;
}

static int run_rx(const Options *opt) {
    int fd = make_socket(opt);
    if (fd < 0) {
        return 1;
    }

    if (bind_socket(fd, opt->bind_ip, opt->port) != 0) {
        close(fd);
        return 1;
    }

    uint32_t max_datagram = opt->segment_bytes + 256U;
    uint8_t *buffer = (uint8_t *)malloc(max_datagram);
    if (buffer == 0) {
        fprintf(stderr, "malloc failed for rx buffer\n");
        close(fd);
        return 1;
    }

    const uint64_t segments_per_frame =
        (opt->frame_bytes + opt->segment_bytes - 1U) / opt->segment_bytes;

    uint64_t started_ms = monotonic_ms();
    uint64_t last_print_ms = started_ms;
    uint64_t packets_rx = 0;
    uint64_t bytes_rx = 0;

    printf("ps_shim rx start: listen=%s:%u frame_bytes=%u segment_bytes=%u approx_segments_per_frame=%" PRIu64 "\n",
           opt->bind_ip,
           (unsigned)opt->port,
           (unsigned)opt->frame_bytes,
           (unsigned)opt->segment_bytes,
           segments_per_frame);

    while (1) {
        uint64_t now_ms = monotonic_ms();
        if (opt->max_runtime_s > 0 && (now_ms - started_ms) >= ((uint64_t)opt->max_runtime_s * 1000ULL)) {
            break;
        }

        ssize_t n = recvfrom(fd, buffer, max_datagram, 0, 0, 0);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                now_ms = monotonic_ms();
                if ((now_ms - last_print_ms) >= opt->print_interval_ms) {
                    double elapsed_s = (double)(now_ms - started_ms) / 1000.0;
                    double mbps = elapsed_s > 0.0 ? ((double)bytes_rx * 8.0) / (elapsed_s * 1000000.0) : 0.0;
                    uint64_t approx_frames = segments_per_frame > 0 ? packets_rx / segments_per_frame : 0;
                    printf("ps_shim rx stats: approx_frames=%" PRIu64 " packets=%" PRIu64 " throughput_mbps=%.2f\n",
                           approx_frames,
                           packets_rx,
                           mbps);
                    last_print_ms = now_ms;
                }
                continue;
            }

            perror("recvfrom");
            free(buffer);
            close(fd);
            return 1;
        }

        packets_rx += 1ULL;
        bytes_rx += (uint64_t)n;

        now_ms = monotonic_ms();
        if ((now_ms - last_print_ms) >= opt->print_interval_ms) {
            double elapsed_s = (double)(now_ms - started_ms) / 1000.0;
            double mbps = elapsed_s > 0.0 ? ((double)bytes_rx * 8.0) / (elapsed_s * 1000000.0) : 0.0;
            uint64_t approx_frames = segments_per_frame > 0 ? packets_rx / segments_per_frame : 0;
            printf("ps_shim rx stats: approx_frames=%" PRIu64 " packets=%" PRIu64 " throughput_mbps=%.2f\n",
                   approx_frames,
                   packets_rx,
                   mbps);
            last_print_ms = now_ms;
        }
    }

    uint64_t done_ms = monotonic_ms();
    double elapsed_s = (double)(done_ms - started_ms) / 1000.0;
    double mbps = elapsed_s > 0.0 ? ((double)bytes_rx * 8.0) / (elapsed_s * 1000000.0) : 0.0;
    uint64_t approx_frames = segments_per_frame > 0 ? packets_rx / segments_per_frame : 0;
    printf("ps_shim rx done: approx_frames=%" PRIu64 " packets=%" PRIu64 " throughput_mbps=%.2f elapsed_s=%.1f\n",
           approx_frames,
           packets_rx,
           mbps,
           elapsed_s);

    free(buffer);
    close(fd);
    return 0;
}

int main(int argc, char **argv) {
    Options opt = parse_args(argc, argv);

    if (opt.mode == MODE_TX) {
        return run_tx(&opt);
    }

    return run_rx(&opt);
}
