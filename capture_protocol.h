#pragma once

#include <stdint.h>

// One definition for both firmwares. RECEIVED acknowledges the command only;
// READY means the Slave sensor can actually capture this session.
enum MessageType {
    MSG_CMD_CAPTURE = 1,
    MSG_ACK_RECEIVED = 2,
    MSG_ACK_UPLOADED = 3,
    MSG_ACK_READY = 4,
    MSG_ACK_UNAVAILABLE = 5,
};

typedef struct __attribute__((packed)) {
    uint8_t msg_type;
    char session_id[32];
} ControlMessage;

_Static_assert(sizeof(ControlMessage) == 33, "ESP-NOW capture message size changed");
