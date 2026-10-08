/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* The bare-metal responder's own interface. See src/rawnet.c for the design.
 *
 * The WIRE format is not here: it is `../../victim/inc/et_victim.h`, shared
 * unchanged with the lwIP responder, so that one host adapter measures either
 * and the two are directly comparable. That is the whole reason this firmware
 * can be justified -- "removing lwIP helped by this much" is only a sentence if
 * both ends speak the same protocol.
 */
#ifndef RAWNET_H
#define RAWNET_H

#include <stdint.h>

/* Bring up the MAC, the PHY and the descriptor rings. 0 on success; negative
 * values distinguish HAL_ETH_Init from the two PHY steps, because "the network
 * did not come up" is three different repairs. */
int  raw_init(void);

/* Poll the PHY and start, stop or reconfigure the MAC to match. Rate-limit it
 * in the caller: it costs MDIO transactions, which are tens of microseconds and
 * must never land inside an exchange. */
void raw_link_tick(void);

int      raw_link_100f(void);
uint8_t  raw_link_speed(void);       /* 0 down, 1 10H, 2 10F, 3 100H, 4 100F */

/* Counters, for the status line. Read from the main loop; written in the
 * interrupt, each by a single store, so a torn read is not possible on a
 * 32-bit core. */
uint32_t raw_seq(void);              /* replies sent                        */
uint32_t raw_seen(void);             /* requests accepted                   */
uint32_t raw_arp(void);              /* ARP requests answered               */
uint32_t raw_drop(void);             /* frames looked at and not for us     */
uint32_t raw_frames(void);           /* receive descriptors processed, total */
/* short, not-IPv4, not-UDP, wrong-port, wrong-magic.  Six causes of silence,
 * six different repairs; one counter cannot tell them apart. */
void     raw_drop_reasons(uint32_t out[5]);
uint16_t raw_reply_len(void);        /* current whole-frame reply length     */
const uint8_t *raw_my_ip(void);

#endif /* RAWNET_H */
