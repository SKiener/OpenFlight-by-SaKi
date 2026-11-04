// Single-producer / single-consumer capture ring (design §9.2).
//
// The audio IO thread is the only producer: `ofl_ring_write` copies one input buffer into a
// preallocated sample ring and publishes a stamp record for it. It never allocates, locks or
// blocks. The processing queue is the only consumer. C11 atomics are used because Swift's
// `Atomic` needs iOS 18 and the app targets iOS 17.
#ifndef OPENFLIGHT_SPSC_RING_H
#define OPENFLIGHT_SPSC_RING_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/// The input buffer's `AudioTimeStamp.mSampleTime` was valid.
#define OFL_STAMP_SAMPLE_TIME_VALID 1u
/// `host_time` is the buffer's `mHostTime`; otherwise the producer read `mach_absolute_time()`.
#define OFL_STAMP_HOST_TIME_VALID 2u
/// The sample ring was full: the stamp is published, the frames are not.
#define OFL_STAMP_AUDIO_DROPPED 4u

/// One input buffer as the IO thread saw it.
typedef struct OFLStampRecord {
    /// `AudioTimeStamp.mSampleTime` (hardware sample clock); meaningful only with
    /// OFL_STAMP_SAMPLE_TIME_VALID.
    double sample_time;
    /// Host time in mach ticks (`mach_absolute_time()` units, not nanoseconds).
    uint64_t host_time;
    /// Frames the producer had received before this buffer, counting dropped ones.
    uint64_t produced_start;
    /// Position of the buffer's first frame in the sample ring (absolute, not wrapped).
    uint64_t ring_start;
    uint32_t frames;
    uint32_t flags;
} OFLStampRecord;

typedef struct OFLCaptureRing OFLCaptureRing;

/// Allocates a ring (zeroed, so the pages are resident before the IO thread touches them).
/// NULL when a capacity is zero or the allocation fails.
OFLCaptureRing *_Nullable ofl_ring_create(uint32_t sample_capacity, uint32_t stamp_capacity);
void ofl_ring_destroy(OFLCaptureRing *_Nullable ring);

/// Producer (IO thread). Copies `frames` samples, taking every `stride`-th float of `data`
/// (channel 0 of interleaved input; 1 for non-interleaved). A NULL `data` is written as silence.
void ofl_ring_write(OFLCaptureRing *_Nonnull ring, const float *_Nullable data, uint32_t frames, uint32_t stride,
                    double sample_time, uint64_t host_time, uint32_t flags);

/// Consumer: the oldest unread stamp. False when none is waiting.
bool ofl_ring_peek_stamp(OFLCaptureRing *_Nonnull ring, OFLStampRecord *_Nonnull out);
/// Consumer: copies `count` samples starting at ring position `start` (which must lie in the
/// oldest unread stamp's frames).
void ofl_ring_copy(const OFLCaptureRing *_Nonnull ring, uint64_t start, uint32_t count, float *_Nonnull out);
/// Consumer: finishes the oldest stamp and frees its frames for the producer.
void ofl_ring_pop_stamp(OFLCaptureRing *_Nonnull ring);

/// Frames the producer dropped because the sample ring was full.
uint64_t ofl_ring_dropped_frames(const OFLCaptureRing *_Nonnull ring);
/// Frames the producer dropped because the stamp ring was full (no record was published).
uint64_t ofl_ring_lost_frames(const OFLCaptureRing *_Nonnull ring);
uint32_t ofl_ring_sample_capacity(const OFLCaptureRing *_Nonnull ring);

#ifdef __cplusplus
}
#endif

#endif
