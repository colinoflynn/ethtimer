/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* et_victim.h -- the reference responder's wire format, defined once.
 *
 * `ethtimer` measures the turnaround of whatever is at the other end of the
 * cable. That makes the thing at the other end part of the measurement, and
 * for a question like "how much jitter does this switch add" it is the part you
 * most want to be boring. A general-purpose device is not boring: an SNMP agent
 * schedules, a web server allocates, a router does work that depends on what
 * else it is doing.
 *
 * This is the boring one. It answers a UDP request with a reply built ahead of
 * time, and -- the part that matters -- it reports **its own** receive-to-send
 * interval in every reply, counted in its own cycles. So the host gets:
 *
 *     dt_hw           the instrument's transmit-complete to receive interrupt
 *     tx_cyc - rx_cyc this responder's receive interrupt to its send
 *     the difference  everything outside both endpoints' software
 *
 * WHAT THE DIFFERENCE IS AND IS NOT. It is two frame serialisations, two MAC
 * transmit paths, and whatever is between them -- cable, switch, router. Those
 * first terms are CONSTANT for a fixed frame size, so the *variance* of the
 * difference is the path's variance and nothing else. The *absolute* value is
 * not any device's latency: it is that device's latency plus a constant this
 * header cannot tell you. Measure a direct cable first and subtract; the
 * difference of two runs is the device under test. `demos/jitter/README.md`
 * says this again at more length, because it is the one claim worth getting
 * right.
 *
 * THE HOST MIRRORS THIS FILE and `tests/test_victim_proto.py` holds the two
 * together, the same way `tests/test_proto.py` does for the instrument's own
 * protocol. Two definitions of one wire format is how the text protocol this
 * project replaced came to drift.
 */
#ifndef ET_VICTIM_H
#define ET_VICTIM_H

#include <stdint.h>

/* Bumped when a field moves. The host checks it in every reply rather than
 * assuming, because a responder flashed last month answering a host from today
 * produces plausible garbage in the timing fields otherwise. */
#define ETV_VERSION          1u

/* The UDP port the responder listens on. 7777 is unassigned by IANA and is not
 * the port of anything that might also be on the subnet. */
#define ETV_PORT             7777u

/* 'E' 'T' 'R' 'Q' and 'E' 'T' 'V' 'R', as little-endian u32 so a single compare
 * identifies a frame. Spelled out as bytes in the table below. */
#define ETV_MAGIC_REQ        0x51525445u   /* "ETRQ" */
#define ETV_MAGIC_RSP        0x52565445u   /* "ETVR" */

/* ---- request: host -> responder ---------------------------------------- *
 *
 *   0..3   magic   u32   ETV_MAGIC_REQ
 *   4      ver     u8    ETV_VERSION
 *   5      cmd     u8    ETV_CMD_*
 *   6..7   rsvd    u16   0
 *   8..11  tag     u32   echoed verbatim; the host's to use as it likes
 *   12..   padding, any length
 *
 * THE REPLY MIRRORS THE REQUEST'S TOTAL LENGTH, which is the whole frame-size
 * control: one `--pad` on the host sweeps both directions at once. A request
 * shorter than the reply header is answered at the header length, because a
 * reply cannot be shorter than the fields it has to carry.
 */
#define ETV_REQ_HDR          12u
#define ETV_CMD_MEASURE      0u   /* the normal case: reply as fast as possible */
#define ETV_CMD_STATUS       1u   /* same reply, plus a console line. Not for a
                                   * capture: printing is tens of microseconds
                                   * of UART and would be measured. */
/* CHECK A PASSWORD GUESS, and take as long doing it as the guess deserves.
 *
 * The reply says only whether the guess was right, in one flag bit, which is all
 * a real login gives back. What it also says, in how long it took, is how many
 * leading bytes were right -- because ETV_CMD_PWCHECK compares them one at a
 * time and stops at the first wrong one.
 *
 * ETV_CMD_PWCHECK_CT is the same check written the way it should be: every byte
 * read whatever happens, no branch on the secret. It is the control, and it is
 * not optional. A flat result from the leaky one and a flat result from this one
 * look identical, so without both a capture cannot tell "constant time" from
 * "my instrument is too noisy to see 30 nanoseconds".
 *
 * The guess follows the request header:
 *
 *   12     guess_len  u8    bytes of guess that follow, <= ETV_PW_MAX_GUESS
 *   13     rsvd       u8    0
 *   14..   guess      guess_len bytes
 *
 * A request shorter than that, or one whose guess_len runs past the end of the
 * datagram, is counted and dropped rather than checked against a shorter guess:
 * a check of the wrong length is a timing measurement of the wrong thing. */
#define ETV_CMD_PWCHECK      2u
#define ETV_CMD_PWCHECK_CT   3u

#define ETV_PW_OFF           12u  /* guess_len lives here                     */
#define ETV_PW_GUESS_OFF     14u  /* the guess itself                         */
#define ETV_PW_MAX_GUESS     32u  /* bound, so a bad length cannot walk off    */

/* ---- reply: responder -> host ------------------------------------------ *
 *
 *   0..3   magic   u32   ETV_MAGIC_RSP
 *   4      ver     u8    ETV_VERSION
 *   5      flags   u8    ETV_F_*
 *   6..7   rsvd    u16   0
 *   8..11  tag     u32   copied from the request
 *   12..15 seq     u32   this responder's reply counter, +1 per reply
 *   16..19 rx_cyc  u32   DWT->CYCCNT latched in the Ethernet receive interrupt
 *   20..23 tx_cyc  u32   DWT->CYCCNT immediately before the reply is handed on
 *   24..27 clk_hz  u32   the rate those two count at
 *   28..31 n_seen  u32   frames delivered to the responder's port, valid or not
 *   32..   padding to the request's length
 *
 * `seq` IS WHAT DETECTS A LOST OR REORDERED EXCHANGE. The instrument counts
 * timeouts, which tells you an exchange produced no reply; it cannot tell you
 * that reply 400 arrived after reply 401. A counter the responder owns can, and
 * through a switch under load that is a thing that happens.
 *
 * `n_seen` minus the records a capture kept is how many frames reached the
 * responder and produced nothing the host used -- a malformed frame, a version
 * mismatch, or a reply that was lost on the way back. Those three are
 * indistinguishable from the host's side alone.
 */
#define ETV_RSP_HDR          32u

#define ETV_F_LINK_100F      0x01u  /* the link was 100 Mbit full duplex. A
                                     * 10 Mbit link reads as up and serialises
                                     * ten times more slowly. */
#define ETV_F_PW_MATCH       0x04u  /* the guess in a PWCHECK request was right.
                                     * ONE BIT, like a real login. Everything
                                     * else a capture learns about the secret is
                                     * in rx_cyc/tx_cyc. */
#define ETV_F_PW_BAD_REQ     0x08u  /* a PWCHECK request whose guess did not fit
                                     * in the datagram. No check was run, so its
                                     * timing means nothing and the host must
                                     * drop the record rather than average it. */

#define ETV_F_CYC_WRAP       0x02u  /* tx_cyc < rx_cyc: the 32-bit cycle counter
                                     * wrapped inside this exchange. At 180 MHz
                                     * that is once per 23.9 s of uptime and the
                                     * subtraction is still correct modulo 2^32,
                                     * so this is a note rather than an error --
                                     * but an interval that looks like 23 s is
                                     * this, not the switch. */

/* The largest reply the responder will build. Not a protocol limit: the
 * instrument keeps at most ET_MAX_WIN bytes of any reply anyway, and a longer
 * frame only changes the serialisation time, which is arithmetic. 1472 is the
 * IPv4 UDP payload that still fits one untagged Ethernet frame. */
#define ETV_MAX_REPLY        1472u

#ifdef __cplusplus
extern "C" {
#endif

void etv_init(void);
/* Called from the main loop. Prints the status line a ETV_CMD_STATUS request
 * asked for, and nothing else -- the measured path does not come through
 * here. */
void etv_poll(void);

#ifdef __cplusplus
}
#endif

#endif /* ET_VICTIM_H */
