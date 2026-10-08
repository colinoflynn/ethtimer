/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* ethtimer -- a general-purpose Ethernet request/response timing instrument.
 *
 * The wire protocol between host and device, and the instrument's state.  This
 * header is the ONE definition of the protocol; `host/ethtimer/proto.py`
 * mirrors it and `host/tests/test_proto.py` checks the two agree.
 *
 * WHY BINARY RATHER THAN THE TEXT PROTOCOL THIS REPLACES.  The device already
 * streamed records as binary, so a text command channel made the link
 * half-and-half and the framing ambiguous.  Hex-encoding a request also doubled
 * it on the wire -- a 128-byte SNMPv3 GET became 256 characters.  Most of all,
 * a text protocol with no length and no checksum cannot tell a truncated reply
 * from a short one, and every command was fire-and-forget: the host could not
 * ask the device what state it was actually in.  That last gap is what made the
 * old instrument's sticky capture window cost two debugging sessions, so
 * ET_CMD_GET_CONFIG exists specifically to close it.
 *
 * FRAME (both directions, little-endian):
 *
 *     0      'E'
 *     1      'T'
 *     2      type        u8
 *     3..4   len         u16   payload length
 *     5..    payload     len bytes
 *     last2  crc16       u16   CCITT-FALSE over bytes [2 .. 5+len)
 *
 * The CRC covers type and length as well as payload, so a corrupted length
 * cannot silently reframe the stream.
 */
#ifndef ETHTIMING_H
#define ETHTIMING_H

#include <stdint.h>

#define ET_MAGIC0            0x45u          /* 'E' */
#define ET_MAGIC1            0x54u          /* 'T' */
/* 2: the request BANK, the TCP transport and RELAY.  A v1 board answers
 * ET_ERR_BAD_ARG to every command added in v2, which reads as "unknown
 * command" and not as a wrong answer -- and the host checks the version out of
 * GET_INFO before it uses any of them, so a stale board is named rather than
 * left to fail three commands later. */
#define ET_PROTO_VERSION     2u
#define ET_FW_VERSION        2u

/* Limits.  MAX_REQ and MAX_WIN are RAM, not protocol: raise them and the batch
 * gets shorter, because the record ring is what dominates.  They are reported
 * in ET_RSP_INFO so a host never has to guess. */
#define ET_MAX_REQ           512u           /* request payload bytes */
/* Raised from 128 in v2 for TLS, where one exchange yields FIFTEEN usable
 * AES pairs: a CBC record is header || explicit IV || C_1..C_n, so keeping
 * the IV and fifteen ciphertext blocks is 256 bytes and gives fifteen
 * (input, output) pairs under one measured turnaround.  Using all of them
 * rather than the first is the difference between a 13-hour capture and a
 * one-hour one, so the limit is what had to move.  It costs no .bss -- the
 * reply buffer is ET_MAX_REPLY either way -- only batch length, because
 * rec_len grows and the ring is fixed. */
#define ET_MAX_WIN           320u           /* reply bytes kept per exchange */
#define ET_MAX_REPLY         1500u          /* one frame's worth */
#define ET_REC_OVERHEAD      12u            /* dt_hw + dt_sw + seq */
/* Record ring. The batch length is ET_RING_BYTES/rec_len, so this is the one
 * number that decides how many exchanges a single RUN can hold. It is set
 * per board in boards/<b>/board.mk because the F429ZI has materially less
 * usable RAM than the F746ZG -- 192 KB overflowed its .bss by 46 848 bytes.
 * Whatever it is, the device reports it in ET_RSP_INFO so a host never has
 * to guess and never has to be rebuilt to find out. */
#ifndef ET_RING_BYTES
#define ET_RING_BYTES        (128u * 1024u)
#endif

/* Request bank.  A victim that refuses a repeated request -- an OSCORE replay
 * window, a TLS record sequence number -- cannot be driven by the single held
 * request that SET_REQUEST loads, so the host uploads a run of DISTINCT
 * requests and the device plays it once.  Played once, never cycled: re-sending
 * a sequence number the victim has already retired is answered with a reject
 * rather than the reply this measures, and it arrives as a batch of timeouts
 * minutes into a capture rather than at the first exchange.
 *
 * The pool is bytes, not entries, because the entries differ in length; both
 * limits are reported in ET_RSP_INFO so the host sizes its uploads from what
 * the device has rather than from a constant it was built with. */
#ifndef ET_BANK_BYTES
#define ET_BANK_BYTES        (64u * 1024u)
#endif
#ifndef ET_BANK_MAX
#define ET_BANK_MAX          2048u          /* entries */
#endif
/* One SET_BANK frame carries several entries, so that a bank of 1 800 short
 * OSCORE requests costs ~40 serial round trips instead of 1 800.  That is the
 * whole difference between tens of exchanges a second and the victim's own
 * limit. */
#define ET_MAX_FRAME_IN      2048u

/* ---- host -> device ---------------------------------------------------- */
#define ET_CMD_PING          0x01u
#define ET_CMD_GET_INFO      0x02u
#define ET_CMD_SET_NET       0x03u   /* ip4 mask4 gw4 victim4                */
#define ET_CMD_SET_TARGET    0x04u   /* proto u8, port u16, timeout_ms u16   */
#define ET_CMD_SET_REQUEST   0x05u   /* raw request bytes                    */
#define ET_CMD_SET_WINDOW    0x06u   /* off u16, len u16                     */
#define ET_CMD_GET_CONFIG    0x07u
#define ET_CMD_RUN           0x08u   /* n u32, gap_us u32                    */
#define ET_CMD_ONESHOT       0x09u   /* raw bytes; replies with what came back */
#define ET_CMD_RESET         0x0Au
/* v2 ------------------------------------------------------------------- */
#define ET_CMD_SET_BANK      0x0Bu   /* flags u8, then {len u16, bytes}...   */
#define ET_CMD_TCP_CONNECT   0x0Cu   /* open a connection to victim:port     */
#define ET_CMD_TCP_CLOSE     0x0Du
#define ET_CMD_RELAY         0x0Eu   /* want u16, bytes -> ET_RSP_RELAY      */

/* SET_BANK flags */
#define ET_BANK_RESET        0x01u   /* clear the bank before appending      */

/* ---- device -> host ---------------------------------------------------- */
#define ET_RSP_ACK           0x81u   /* cmd u8, status u8                    */
#define ET_RSP_ERR           0x82u   /* cmd u8, code u8, text                */
#define ET_RSP_INFO          0x83u
#define ET_RSP_CONFIG        0x84u
#define ET_RSP_BATCH_HDR     0x85u
#define ET_RSP_BATCH_DATA    0x86u
#define ET_RSP_BATCH_END     0x87u
#define ET_RSP_ONESHOT       0x88u
#define ET_RSP_RELAY         0x89u   /* dt_hw u32, dt_sw u32, len u16, bytes */

/* ---- error codes ------------------------------------------------------- */
#define ET_ERR_NONE          0u
#define ET_ERR_BAD_LEN       1u
#define ET_ERR_BAD_ARG       2u
#define ET_ERR_NO_REQUEST    3u   /* RUN with no request loaded             */
#define ET_ERR_LINK_DOWN     4u
#define ET_ERR_NOT_IMPL      5u   /* e.g. TCP transport                     */
#define ET_ERR_TOO_BIG       6u
#define ET_ERR_WINDOW        7u   /* window cannot fit any observed reply   */
#define ET_ERR_TCP           8u   /* connect refused, or no open connection */
#define ET_ERR_BANK          9u   /* bank empty, full, or shorter than RUN  */

/* ---- transports -------------------------------------------------------- */
#define ET_PROTO_UDP         0u
#define ET_PROTO_TCP         1u   /* needs TCP_CONNECT before RUN or RELAY  */

/* Link speeds as reported in INFO/CONFIG.  The distinction matters: a 10 Mbit
 * half-duplex link reads as "up" exactly like 100 Mbit full and silently
 * changes every measurement, so the speed is reported, never inferred. */
#define ET_LINK_DOWN         0u
#define ET_LINK_10H          1u
#define ET_LINK_10F          2u
#define ET_LINK_100H         3u
#define ET_LINK_100F         4u

/* One record, as written into the ring and streamed to the host:
 *
 *     [0 .. win_len)          the reply window
 *     [win_len + 0)   u32     dt_hw   TX-complete IRQ -> RX IRQ, DWT cycles
 *     [win_len + 4)   u32     dt_sw   pre-send -> post-receive, DWT cycles
 *     [win_len + 8)   u32     seq     exchange index within the batch
 *
 * `seq` replaces the old firmware's optional victim-DWT slot, which was written
 * at a fixed offset that fell INSIDE the window whenever the window was 32
 * bytes -- silently corrupting four bytes of every captured reply.  Nothing is
 * ever written inside the window here, and rec_len is reported in BATCH_HDR so
 * the host parses what the device actually produced rather than what it
 * assumed.
 */

typedef struct
{
    uint8_t  ip[4], mask[4], gw[4], victim[4];
    uint16_t port;
    uint16_t timeout_ms;
    uint8_t  proto;
    uint16_t win_off, win_len;
    uint16_t req_len;
    uint16_t req_crc;
    /* v2.  `want` is how many reply bytes an exchange waits for before it stops
     * collecting, and it exists for TCP: a UDP reply arrives as one datagram
     * and its end is unambiguous, whereas a TLS record can be split across
     * segments and "the reply" is only complete at a length the host knows and
     * the device does not.  SET_WINDOW with 4 bytes leaves it at win_off +
     * win_len, which is what every UDP target wants.
     *
     * bank_n / bank_crc are the read-back that makes an upload verifiable, for
     * the same reason every other setter has one. */
    uint16_t want;
    uint16_t bank_n;
    uint16_t bank_crc;
    uint8_t  tcp_state;      /* 0 idle, 1 connecting, 2 open, 3 closed */
} et_config_t;

/* g_tx_cyc / g_rx_cyc / g_tx_evt / g_rx_evt are declared in board.h, next to
 * the board whose interrupt file defines them.  They used to be declared here,
 * which meant a second application sharing this board support had to include
 * the instrument's wire protocol to see its own board's interrupt variables. */

void     et_init(void);
void     et_poll(void);
uint16_t et_crc16(const uint8_t *p, uint32_t n);

#endif /* ETHTIMING_H */
