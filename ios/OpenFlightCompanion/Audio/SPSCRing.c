#include "SPSCRing.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

struct OFLCaptureRing {
    float *samples;
    OFLStampRecord *stamps;
    uint32_t sample_capacity;
    uint32_t stamp_capacity;
    // Producer-owned counters, published with release stores.
    _Atomic uint64_t sample_write;
    _Atomic uint64_t stamp_write;
    // Consumer-owned counters.
    _Atomic uint64_t sample_read;
    _Atomic uint64_t stamp_read;
    _Atomic uint64_t dropped_frames;
    _Atomic uint64_t lost_frames;
    // Touched by the producer only.
    uint64_t produced;
};

OFLCaptureRing *ofl_ring_create(uint32_t sample_capacity, uint32_t stamp_capacity) {
    if (sample_capacity == 0 || stamp_capacity == 0) return NULL;
    OFLCaptureRing *ring = calloc(1, sizeof(OFLCaptureRing));
    if (!ring) return NULL;
    ring->samples = calloc(sample_capacity, sizeof(float));
    ring->stamps = calloc(stamp_capacity, sizeof(OFLStampRecord));
    if (!ring->samples || !ring->stamps) {
        free(ring->samples);
        free(ring->stamps);
        free(ring);
        return NULL;
    }
    ring->sample_capacity = sample_capacity;
    ring->stamp_capacity = stamp_capacity;
    atomic_init(&ring->sample_write, 0);
    atomic_init(&ring->stamp_write, 0);
    atomic_init(&ring->sample_read, 0);
    atomic_init(&ring->stamp_read, 0);
    atomic_init(&ring->dropped_frames, 0);
    atomic_init(&ring->lost_frames, 0);
    ring->produced = 0;
    return ring;
}

void ofl_ring_destroy(OFLCaptureRing *ring) {
    if (!ring) return;
    free(ring->samples);
    free(ring->stamps);
    free(ring);
}

void ofl_ring_write(OFLCaptureRing *ring, const float *data, uint32_t frames, uint32_t stride,
                    double sample_time, uint64_t host_time, uint32_t flags) {
    if (frames == 0) return;
    if (stride == 0) stride = 1;
    const uint64_t produced_start = ring->produced;
    ring->produced += frames;

    const uint64_t stamp_write = atomic_load_explicit(&ring->stamp_write, memory_order_relaxed);
    const uint64_t stamp_read = atomic_load_explicit(&ring->stamp_read, memory_order_acquire);
    if (stamp_write - stamp_read >= ring->stamp_capacity) {
        atomic_fetch_add_explicit(&ring->lost_frames, frames, memory_order_relaxed);
        return;
    }

    OFLStampRecord record;
    record.sample_time = sample_time;
    record.host_time = host_time;
    record.produced_start = produced_start;
    record.frames = frames;
    record.flags = flags & (OFL_STAMP_SAMPLE_TIME_VALID | OFL_STAMP_HOST_TIME_VALID);

    const uint64_t sample_write = atomic_load_explicit(&ring->sample_write, memory_order_relaxed);
    const uint64_t sample_read = atomic_load_explicit(&ring->sample_read, memory_order_acquire);
    if (frames <= ring->sample_capacity && sample_write - sample_read + frames <= ring->sample_capacity) {
        const uint32_t capacity = ring->sample_capacity;
        uint32_t position = (uint32_t)(sample_write % capacity);
        if (data == NULL) {
            const uint32_t first = frames < capacity - position ? frames : capacity - position;
            memset(ring->samples + position, 0, first * sizeof(float));
            if (first < frames) memset(ring->samples, 0, (frames - first) * sizeof(float));
        } else if (stride == 1) {
            const uint32_t first = frames < capacity - position ? frames : capacity - position;
            memcpy(ring->samples + position, data, first * sizeof(float));
            if (first < frames) memcpy(ring->samples, data + first, (frames - first) * sizeof(float));
        } else {
            for (uint32_t i = 0; i < frames; i++) {
                ring->samples[position] = data[(uint64_t)i * stride];
                position = position + 1 == capacity ? 0 : position + 1;
            }
        }
        record.ring_start = sample_write;
        atomic_store_explicit(&ring->sample_write, sample_write + frames, memory_order_release);
    } else {
        record.ring_start = sample_write;
        record.flags |= OFL_STAMP_AUDIO_DROPPED;
        atomic_fetch_add_explicit(&ring->dropped_frames, frames, memory_order_relaxed);
    }

    ring->stamps[stamp_write % ring->stamp_capacity] = record;
    atomic_store_explicit(&ring->stamp_write, stamp_write + 1, memory_order_release);
}

bool ofl_ring_peek_stamp(OFLCaptureRing *ring, OFLStampRecord *out) {
    const uint64_t stamp_read = atomic_load_explicit(&ring->stamp_read, memory_order_relaxed);
    const uint64_t stamp_write = atomic_load_explicit(&ring->stamp_write, memory_order_acquire);
    if (stamp_read == stamp_write) return false;
    *out = ring->stamps[stamp_read % ring->stamp_capacity];
    return true;
}

void ofl_ring_copy(const OFLCaptureRing *ring, uint64_t start, uint32_t count, float *out) {
    const uint32_t capacity = ring->sample_capacity;
    const uint32_t position = (uint32_t)(start % capacity);
    const uint32_t first = count < capacity - position ? count : capacity - position;
    memcpy(out, ring->samples + position, first * sizeof(float));
    if (first < count) memcpy(out + first, ring->samples, (count - first) * sizeof(float));
}

void ofl_ring_pop_stamp(OFLCaptureRing *ring) {
    const uint64_t stamp_read = atomic_load_explicit(&ring->stamp_read, memory_order_relaxed);
    const uint64_t stamp_write = atomic_load_explicit(&ring->stamp_write, memory_order_acquire);
    if (stamp_read == stamp_write) return;
    const OFLStampRecord record = ring->stamps[stamp_read % ring->stamp_capacity];
    if ((record.flags & OFL_STAMP_AUDIO_DROPPED) == 0) {
        atomic_store_explicit(&ring->sample_read, record.ring_start + record.frames, memory_order_release);
    }
    atomic_store_explicit(&ring->stamp_read, stamp_read + 1, memory_order_release);
}

uint64_t ofl_ring_dropped_frames(const OFLCaptureRing *ring) {
    return atomic_load_explicit(&((OFLCaptureRing *)ring)->dropped_frames, memory_order_relaxed);
}

uint64_t ofl_ring_lost_frames(const OFLCaptureRing *ring) {
    return atomic_load_explicit(&((OFLCaptureRing *)ring)->lost_frames, memory_order_relaxed);
}

uint32_t ofl_ring_sample_capacity(const OFLCaptureRing *ring) {
    return ring->sample_capacity;
}
