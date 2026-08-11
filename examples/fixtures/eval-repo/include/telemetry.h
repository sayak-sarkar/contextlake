// Synthetic fixture for retrieval-quality scoring. Invented domain, no real code.
#ifndef TELEMETRY_H
#define TELEMETRY_H

#define TELEMETRY_MAX_BATCH 512
#define TELEMETRY_RETRY_LIMIT 3

typedef unsigned long long SampleId;
typedef struct SampleWindow SampleWindow;

enum SampleSeverity { SEVERITY_DEBUG, SEVERITY_NOTICE, SEVERITY_ALERT };

namespace telemetry {

class SampleBuffer {
public:
    SampleBuffer();
    ~SampleBuffer();
    bool append(SampleId id, double reading);
    void drain();
    unsigned pending() const;

private:
    unsigned pending_count_;
    double* readings_;
    SampleId last_id_;
};

class WindowAggregator {
public:
    double mean() const;
    double peak() const;

private:
    unsigned sample_total_;
};

}  // namespace telemetry
#endif
