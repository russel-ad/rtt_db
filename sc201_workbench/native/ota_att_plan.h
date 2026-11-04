#pragma once
#include "mlink/wire.h"
#include "mlink/xfer.h"
#include <cstdint>

// Size bounds only. Encoding, encryption, transfer/ACK state and Flash
// acknowledgement remain entirely in the frozen libmlink implementation.
inline uint32_t rttOtaFrameBound(uint16_t chunk)
{
    const uint32_t raw = MLINK_HDR_LEN + MLINK_ENC_OVERHEAD +
        MLINK_XFER_BLOCK_OVERHEAD + uint32_t(chunk) + MLINK_CRC_LEN;
    return MLINK_FRAME_DELIMS + raw + MLINK_COBS_OVERHEAD(raw);
}

inline uint16_t rttOtaChunk(uint16_t plaintextRoom, uint16_t attPayload, bool enabled)
{
    const uint16_t original = uint16_t(mlink_xfer_block_max(plaintextRoom) & ~uint16_t(3));
    if (!enabled || !original || !attPayload || rttOtaFrameBound(original) <= attPayload) return original;
    // Retain the original plan for small MTUs. Saving a data submission must
    // also compensate for more block acknowledgements and stored reports.
    const uint32_t originalParts = (rttOtaFrameBound(original) + attPayload - 1u) / attPayload;
    for (uint16_t candidate = original; candidate >= 4; candidate -= 4) {
        if (rttOtaFrameBound(candidate) > attPayload) continue;
        return uint32_t(candidate) * (originalParts + 2u) > uint32_t(original) * 3u ? candidate : original;
    }
    return original;
}
