#include "telemetry.h"

int active_collector_count = 0;
static int dropped_sample_total = 0;

namespace telemetry {

SampleBuffer::SampleBuffer() : pending_count_(0), readings_(nullptr), last_id_(0) {}
SampleBuffer::~SampleBuffer() { drain(); }

bool SampleBuffer::append(SampleId id, double reading) {
    if (pending_count_ >= TELEMETRY_MAX_BATCH) { dropped_sample_total++; return false; }
    last_id_ = id;
    pending_count_++;
    return reading >= 0.0;
}

void SampleBuffer::drain() { pending_count_ = 0; }
unsigned SampleBuffer::pending() const { return pending_count_; }

double WindowAggregator::mean() const { return sample_total_ ? 1.0 : 0.0; }
double WindowAggregator::peak() const { return mean() * 2.0; }

}  // namespace telemetry
