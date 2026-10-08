/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* ethtimer -- the instrument core: binary control protocol and capture loop.
 *
 * Nothing here is protocol-aware above UDP.  The host supplies the request
 * bytes and says which slice of the reply to keep; what those bytes mean is the
 * host's business.  See inc/ethtimer.h for the frame format and the reasoning
 * behind it, and ../README.md for the instrument as a whole.
 */
#include "board.h"
#include "ethtimer.h"

#include <string.h>

#include "lwip/udp.h"
#include "lwip/tcp.h"
#include "lwip/pbuf.h"
#include "lwip/ip_addr.h"
#include "lwip/netif.h"
#include "lwip/etharp.h"
#include "lwip/timeouts.h"

/* ------------------------------------------------------------------ state -- */

static struct udp_pcb *g_pcb;
static ip_addr_t       g_victim;
static uint16_t        g_port       = 161;
static uint16_t        g_timeout_ms = 200;
static uint8_t         g_proto      = ET_PROTO_UDP;

static uint8_t  g_req[ET_MAX_REQ];
static uint16_t g_req_len;
static uint16_t g_req_crc;

static uint16_t g_win_off, g_win_len = 32;
/* How many reply bytes an exchange waits for.  0 means "win_off + win_len",
 * which is right for every UDP target; TCP needs it set explicitly because a
 * reply can arrive in more than one segment.  See et_config_t. */
static uint16_t g_want;

static uint8_t  g_rx_buf[ET_MAX_REPLY];
static volatile uint16_t g_rx_len;
static volatile uint32_t g_rx_seq;
static volatile uint32_t g_rx_sw_cyc;       /* DWT at the receive callback */

static uint8_t  g_ring[ET_RING_BYTES];

/* ---- the request bank (v2) ---------------------------------------------- */
static uint8_t  g_bank[ET_BANK_BYTES];
static uint16_t g_bank_off[ET_BANK_MAX];    /* start of entry i in g_bank     */
static uint16_t g_bank_len[ET_BANK_MAX];
static uint16_t g_bank_n;                   /* entries appended               */
static uint32_t g_bank_used;                /* bytes of g_bank in use         */
static uint16_t g_bank_crc;                 /* over every byte appended       */

/* ---- TCP (v2) ------------------------------------------------------------ */
static struct tcp_pcb   *g_tcp;
static volatile uint8_t  g_tcp_state;       /* 0 idle 1 connecting 2 open 3 closed */
static uint8_t           g_trx[ET_MAX_REPLY];
static volatile uint16_t g_trx_len;
static volatile uint32_t g_trx_seq;

/* g_tx_cyc / g_rx_cyc / g_tx_evt / g_rx_evt are DEFINED in the board's
 * interrupt file, next to the ISR that writes them, and only declared in
 * ethtimer.h.  Defining them here as well linked until the board file was
 * added and then failed with four "multiple definition" errors. */

/* ---------------------------------------------------------------- crc16 --- */

/* CCITT-FALSE: init 0xFFFF, poly 0x1021, no reflection, no final xor.  Chosen
 * because it is the variant the host's `binascii`-free implementation in
 * proto.py mirrors line for line, and test_proto.py checks the two agree on a
 * fixed vector so they cannot drift apart. */
uint16_t et_crc16(const uint8_t *p, uint32_t n)
{
    uint16_t c = 0xFFFFu;
    uint32_t i;
    int b;
    for (i = 0; i < n; i++)
    {
        c ^= (uint16_t)((uint16_t)p[i] << 8);
        for (b = 0; b < 8; b++)
        {
            c = (uint16_t)((c & 0x8000u) ? (uint16_t)((c << 1) ^ 0x1021u)
                                         : (uint16_t)(c << 1));
        }
    }
    return c;
}

/* ------------------------------------------------------------ frame out --- */

static void put_u16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;         p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static uint16_t get_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Send one frame.  The header and CRC are built in a small stack buffer so the
 * payload never has to be copied -- a batch chunk is tens of kilobytes. */
static void frame_send(uint8_t type, const uint8_t *payload, uint16_t len)
{
    uint8_t hdr[5], crc[2];
    uint16_t c;

    hdr[0] = ET_MAGIC0;
    hdr[1] = ET_MAGIC1;
    hdr[2] = type;
    put_u16(&hdr[3], len);

    c = et_crc16(&hdr[2], 3);
    /* Continue the CRC over the payload without concatenating: same polynomial
     * state, fed in two parts. */
    {
        uint16_t cc = c;
        uint32_t i;
        int b;
        for (i = 0; i < len; i++)
        {
            cc ^= (uint16_t)((uint16_t)payload[i] << 8);
            for (b = 0; b < 8; b++)
            {
                cc = (uint16_t)((cc & 0x8000u) ? (uint16_t)((cc << 1) ^ 0x1021u)
                                           : (uint16_t)(cc << 1));
            }
        }
        c = cc;
    }
    put_u16(crc, c);

    uart_write(hdr, 5);
    if (len) { uart_write(payload, len); }
    uart_write(crc, 2);
}

static void send_ack(uint8_t cmd)
{
    uint8_t p[2];
    p[0] = cmd; p[1] = ET_ERR_NONE;
    frame_send(ET_RSP_ACK, p, 2);
}

static void send_err(uint8_t cmd, uint8_t code, const char *msg)
{
    uint8_t p[66];
    uint16_t n = 0;
    p[n++] = cmd;
    p[n++] = code;
    while (*msg && n < sizeof(p)) { p[n++] = (uint8_t)*msg++; }
    frame_send(ET_RSP_ERR, p, n);
}

/* --------------------------------------------------------------- link ----- */

static uint8_t link_speed(void) { return board_link_speed(); }

/* -------------------------------------------------------------- transport - */

static void udp_rx(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                   const ip_addr_t *addr, u16_t port)
{
    (void)arg; (void)pcb; (void)addr; (void)port;
    g_rx_sw_cyc = DWT->CYCCNT;
    g_rx_len = (uint16_t)pbuf_copy_partial(p, g_rx_buf, (u16_t)sizeof(g_rx_buf), 0);
    pbuf_free(p);
    g_rx_seq++;
}

/* Returns the DWT value taken immediately before handing the buffer to lwIP. */
static uint32_t transport_send(const uint8_t *buf, uint16_t len)
{
    struct pbuf *q = pbuf_alloc(PBUF_TRANSPORT, (u16_t)len, PBUF_RAM);
    uint32_t t;
    if (!q) { return 0; }
    memcpy(q->payload, buf, len);
    t = DWT->CYCCNT;
    udp_sendto(g_pcb, q, &g_victim, g_port);
    pbuf_free(q);
    return t;
}

/* Pump the stack until a reply arrives or the timeout expires. 1 on reply. */
static int transport_wait(uint32_t seq0, uint32_t timeout_ms)
{
    uint32_t t0 = HAL_GetTick();
    while (g_rx_seq == seq0)
    {
        board_tick();
        if ((HAL_GetTick() - t0) > timeout_ms) { return 0; }
    }
    return 1;
}

/* ------------------------------------------------------------------- TCP -- */
/* A held-open connection, one record out and one reply back per exchange.
 *
 * WHY TCP IS A DIFFERENT SHAPE FROM UDP.  A datagram arrives whole and its end
 * needs no interpretation; a TCP reply is a byte stream that may be split
 * across segments, so "the reply has arrived" is a statement only the host can
 * make.  `g_want` is that statement, carried down from SET_WINDOW.
 *
 * The receive buffer is deliberately NOT cleared at the start of an exchange.
 * Bytes that arrived between commands are still part of the stream, and
 * dropping them corrupts it -- which the peer reports as an unexpected-message
 * alert several exchanges later, a long way from the cause.  It is drained
 * once it has been reported.
 */

static err_t tcp_recv_cb(void *arg, struct tcp_pcb *pcb, struct pbuf *p, err_t err)
{
    (void)arg; (void)err;
    if (p == NULL)
    {
        /* The peer closed.  Tear the callbacks down BEFORE closing: letting a
         * later command reach tcp_write() through a freed pcb is a
         * use-after-free, and it wedged the board rather than failing. */
        tcp_recv(pcb, NULL);
        tcp_err(pcb, NULL);
        tcp_close(pcb);
        if (pcb == g_tcp) { g_tcp = NULL; }
        g_tcp_state = 3;
        return ERR_OK;
    }
    if (g_trx_len < sizeof(g_trx))
    {
        uint16_t room = (uint16_t)(sizeof(g_trx) - g_trx_len);
        uint16_t n = pbuf_copy_partial(p, &g_trx[g_trx_len], room, 0);
        g_trx_len = (uint16_t)(g_trx_len + n);
    }
    g_trx_seq++;
    tcp_recved(pcb, p->tot_len);
    pbuf_free(p);
    return ERR_OK;
}

static err_t tcp_conn_cb(void *arg, struct tcp_pcb *pcb, err_t err)
{
    (void)arg; (void)pcb;
    g_tcp_state = (err == ERR_OK) ? 2u : 3u;
    return ERR_OK;
}

static void tcp_err_cb(void *arg, err_t err)
{
    (void)arg; (void)err;
    /* lwIP has already freed the pcb by the time this runs. */
    g_tcp = NULL;
    g_tcp_state = 3;
}

static int et_tcp_open(void)
{
    uint32_t t0;

    if (g_tcp) { tcp_abort(g_tcp); g_tcp = NULL; }
    g_tcp_state = 0;
    g_trx_len = 0;

    g_tcp = tcp_new();
    if (!g_tcp) { return 0; }
    tcp_arg(g_tcp, NULL);
    tcp_recv(g_tcp, tcp_recv_cb);
    tcp_err(g_tcp, tcp_err_cb);
    /* One record, one segment.  Nagle would coalesce two records into one
     * send and there would be no turnaround to measure between them. */
    tcp_nagle_disable(g_tcp);
    g_tcp_state = 1;
    if (tcp_connect(g_tcp, &g_victim, g_port, tcp_conn_cb) != ERR_OK)
    {
        g_tcp_state = 3;
        return 0;
    }
    t0 = HAL_GetTick();
    while (g_tcp_state == 1)
    {
        board_tick();
        if ((HAL_GetTick() - t0) > 5000u) { g_tcp_state = 3; break; }
    }
    return g_tcp_state == 2;
}

static void et_tcp_close(void)
{
    if (g_tcp)
    {
        tcp_recv(g_tcp, NULL);
        tcp_err(g_tcp, NULL);
        if (tcp_close(g_tcp) != ERR_OK) { tcp_abort(g_tcp); }
        g_tcp = NULL;
    }
    g_tcp_state = 0;
    g_trx_len = 0;
}

/* One TCP exchange.  Returns the bytes collected, 0 on failure, and fills
 * dt_hw/dt_sw exactly as the UDP path does: ISR-latched transmit-complete to
 * the FIRST byte back, and a pre-send software timestamp to the same instant.
 * Collection continues past that point for the rest of the reply, which must
 * not be allowed to move the timestamp -- hence `got_first`. */
static uint32_t tcp_exchange(const uint8_t *buf, uint16_t len, uint16_t want,
                             uint32_t *dt_hw, uint32_t *dt_sw)
{
    uint32_t t_sw, tx_evt0, t0, t_first = 0, rx_first = 0, seq0;
    int got_first = 0;

    if (g_tcp_state != 2 || !g_tcp) { return 0; }
    seq0 = g_trx_seq;
    board_tx_release();

    tx_evt0 = g_tx_evt;
    t_sw = DWT->CYCCNT;
    if (tcp_write(g_tcp, buf, len, TCP_WRITE_FLAG_COPY) != ERR_OK) { return 0; }
    tcp_output(g_tcp);

    t0 = HAL_GetTick();
    for (;;)
    {
        board_tick();
        if (!got_first && g_trx_seq != seq0)
        {
            rx_first = g_rx_cyc;
            got_first = 1;
            t_first = HAL_GetTick();
        }
        /* Stop at `want`, or shortly after the first byte if the peer sent
         * less than expected -- a 40 ms grace, long next to a 1 ms exchange
         * and short next to the timeout, so a short reply is reported rather
         * than charged the full timeout. */
        if (got_first && (g_trx_len >= want || (HAL_GetTick() - t_first) > 40u)) { break; }
        if ((HAL_GetTick() - t0) > g_timeout_ms) { break; }
        if (g_tcp_state != 2) { break; }
    }
    *dt_hw = (got_first && g_tx_evt != tx_evt0) ? (rx_first - g_tx_cyc) : 0u;
    *dt_sw = got_first ? (rx_first - t_sw) : 0u;
    return g_trx_len;
}

static void spin_us(uint32_t us)
{
    uint32_t hz = board_cyccnt_hz();
    uint32_t ticks = (uint32_t)((uint64_t)us * hz / 1000000u);
    uint32_t t0 = DWT->CYCCNT;
    while ((DWT->CYCCNT - t0) < ticks) { }
}

/* ----------------------------------------------------------- the capture -- */

static void do_run(uint32_t n, uint32_t gap_us)
{
    uint16_t rec_len = (uint16_t)(g_win_len + ET_REC_OVERHEAD);
    uint32_t cap     = ET_RING_BYTES / rec_len;
    uint32_t got = 0, n_to = 0, n_short = 0, n_bad_dt = 0;
    uint32_t i, start_ms;
    uint8_t  hdr[14];
    /* An exchange cannot legitimately have taken longer than its own timeout,
     * so anything above this is not a duration.  In practice what it is, is a
     * SUBTRACTION THAT CAME OUT NEGATIVE and wrapped: the receive interrupt
     * latched before the transmit-complete one, which happens when a stray
     * segment lands between the two.  Measured once in 5 000 TCP exchanges,
     * and one such record is enough to take a capture's standard deviation
     * from 4 us to 281 000 us while leaving the median untouched -- so it is
     * rejected and counted rather than recorded. */
    uint32_t max_dt = (uint32_t)((uint64_t)g_timeout_ms
                                 * board_cyccnt_hz() / 1000u);
    /* The bank, when loaded, is what a run plays -- once, in order.  Without
     * one the single SET_REQUEST request is repeated, which is right for any
     * victim that supplies the varying input itself. */
    uint16_t want = g_want ? g_want : (uint16_t)(g_win_off + g_win_len);

    if (n > cap) { n = cap; }
    if (g_bank_n && n > g_bank_n) { n = g_bank_n; }

    put_u32(&hdr[0], n);
    put_u16(&hdr[4], rec_len);
    put_u16(&hdr[6], g_win_off);
    put_u16(&hdr[8], g_win_len);
    put_u32(&hdr[10], board_cyccnt_hz());
    frame_send(ET_RSP_BATCH_HDR, hdr, 14);

    start_ms = HAL_GetTick();
    for (i = 0; i < n; i++)
    {
        uint32_t seq0 = g_rx_seq;
        uint32_t tx0  = g_tx_evt;
        uint32_t sw_send, dt_hw, dt_sw;
        uint8_t *rec;
        const uint8_t *req = g_req;
        uint16_t rlen = g_req_len;
        const uint8_t *reply;
        uint32_t reply_len;

        if (g_bank_n)
        {
            req  = &g_bank[g_bank_off[i]];
            rlen = g_bank_len[i];
        }

        if (g_proto == ET_PROTO_TCP)
        {
            /* The peer has gone.  Every remaining exchange would cost the full
             * timeout and return nothing, so a 550-entry bank against a closed
             * connection is minutes of grinding that ends in an empty batch.
             * Stop and let the host see a SHORT batch with the connection
             * reported closed, which says what happened; a batch of timeouts
             * does not. */
            if (g_tcp_state != 2) { break; }
            reply_len = tcp_exchange(req, rlen, want, &dt_hw, &dt_sw);
            reply     = g_trx;
            if (!reply_len || !dt_hw) { n_to++; g_trx_len = 0; goto gap; }
        }
        else
        {
            sw_send = transport_send(req, rlen);
            if (!transport_wait(seq0, g_timeout_ms)) { n_to++; goto gap; }

            /* Only count the exchange if the ISR actually saw a
             * transmit-complete; without it dt_hw is a stale difference rather
             * than this exchange's. */
            if (g_tx_evt == tx0) { n_short++; goto gap; }
            dt_hw     = g_rx_cyc - g_tx_cyc;
            dt_sw     = g_rx_sw_cyc - sw_send;
            reply     = g_rx_buf;
            reply_len = g_rx_len;
        }

        /* A reply too short for the window is COUNTED, not skipped in silence.
         * The firmware this replaces did `continue` here, so a window that did
         * not fit returned fewer records with timeouts=0 -- or none at all --
         * and read exactly like a dead victim.  It cost two debugging sessions,
         * so n_short is a first-class number in BATCH_END. */
        if (reply_len < (uint32_t)g_win_off + g_win_len)
        {
            n_short++;
            if (g_proto == ET_PROTO_TCP) { g_trx_len = 0; }
            goto gap;
        }

        if (max_dt && dt_hw > max_dt)
        {
            n_bad_dt++;
            if (g_proto == ET_PROTO_TCP) { g_trx_len = 0; }
            goto gap;
        }

        rec = &g_ring[got * rec_len];
        memcpy(rec, &reply[g_win_off], g_win_len);
        put_u32(rec + g_win_len + 0, dt_hw);
        put_u32(rec + g_win_len + 4, dt_sw);
        put_u32(rec + g_win_len + 8, i);
        got++;
        /* Drained: the next exchange's reply starts at zero.  On TCP this is
         * the ONLY place the stream buffer is reset -- see tcp_recv_cb. */
        if (g_proto == ET_PROTO_TCP) { g_trx_len = 0; }

    gap:
        if (gap_us) { spin_us(gap_us); }
    }

    /* A bank is consumed by the run that played it.  Leaving it loaded would
     * let a second RUN replay sequence numbers the victim has retired, which
     * is the failure this whole mechanism exists to prevent -- and it would do
     * it silently, as a batch of timeouts. */
    if (g_bank_n) { g_bank_n = 0; g_bank_used = 0; g_bank_crc = 0; }

    /* Stream the ring in chunks so a host reading with a modest buffer never
     * has to hold the whole batch, and so a truncated transfer is visible as a
     * missing frame rather than as a short final read. */
    {
        uint32_t off = 0, total = got * rec_len;
        while (off < total)
        {
            uint32_t chunk = total - off;
            if (chunk > 4096u) { chunk = 4096u; }
            frame_send(ET_RSP_BATCH_DATA, &g_ring[off], (uint16_t)chunk);
            off += chunk;
        }
    }

    {
        uint8_t end[20];
        put_u32(&end[0], got);
        put_u32(&end[4], n_to);
        put_u32(&end[8], n_short);
        put_u32(&end[12], (HAL_GetTick() - start_ms));
        put_u32(&end[16], n_bad_dt);
        frame_send(ET_RSP_BATCH_END, end, 20);
    }
}

/* ----------------------------------------------------------- config out --- */

static void send_config(void)
{
    uint8_t p[40];
    uint8_t ip[4], mask[4], gw[4];
    uint16_t n = 0;
    int i;

    board_netif_get(ip, mask, gw);
    for (i = 0; i < 4; i++) { p[n++] = ip[i]; }
    for (i = 0; i < 4; i++) { p[n++] = mask[i]; }
    for (i = 0; i < 4; i++) { p[n++] = gw[i]; }
    for (i = 0; i < 4; i++) { p[n++] = ip4_addr_get_u32(&g_victim) >> (8 * i); }
    put_u16(&p[n], g_port);        n += 2;
    put_u16(&p[n], g_timeout_ms);  n += 2;
    p[n++] = g_proto;
    put_u16(&p[n], g_win_off);     n += 2;
    put_u16(&p[n], g_win_len);     n += 2;
    put_u16(&p[n], g_req_len);     n += 2;
    put_u16(&p[n], g_req_crc);     n += 2;
    p[n++] = link_speed();
    /* v2 tail.  The two sides move together and ET_PROTO_VERSION says so, so a
     * v1 host is REFUSED by the version check rather than left to misparse
     * this -- which is the only safe way to grow a fixed-offset record. */
    put_u16(&p[n], g_want);        n += 2;
    put_u16(&p[n], g_bank_n);      n += 2;
    put_u16(&p[n], g_bank_crc);    n += 2;
    p[n++] = g_tcp_state;
    /* Lost UART bytes.  Reported because the symptom of losing one is a
     * command that is never answered, which from the host looks like a dead
     * board rather than like a dropped byte. */
    {
        uint32_t lost = board_uart_lost();
        put_u16(&p[n], (uint16_t)(lost > 0xFFFFu ? 0xFFFFu : lost)); n += 2;
    }
    frame_send(ET_RSP_CONFIG, p, n);
}

static void send_info(void)
{
    uint8_t p[48];
    uint16_t n = 0;
    const char *name = BOARD_NAME;

    p[n++] = ET_PROTO_VERSION;
    p[n++] = ET_FW_VERSION;
    put_u32(&p[n], board_cyccnt_hz()); n += 4;
    put_u16(&p[n], ET_MAX_REQ);            n += 2;
    put_u16(&p[n], ET_MAX_WIN);            n += 2;
    put_u32(&p[n], ET_RING_BYTES);         n += 4;
    p[n++] = link_speed();
    /* v2 tail: the bank's two limits, so the host sizes an upload from what
     * this board has rather than from a constant it was built with. */
    put_u32(&p[n], ET_BANK_BYTES);         n += 4;
    put_u16(&p[n], ET_BANK_MAX);           n += 2;
    while (*name && n < sizeof(p) - 1) { p[n++] = (uint8_t)*name++; }
    frame_send(ET_RSP_INFO, p, n);
}

/* -------------------------------------------------------------- dispatch -- */

static void handle(uint8_t type, const uint8_t *p, uint16_t len)
{
    switch (type)
    {
    case ET_CMD_PING:
        send_ack(type);
        break;

    case ET_CMD_GET_INFO:
        send_info();
        break;

    case ET_CMD_GET_CONFIG:
        send_config();
        break;

    case ET_CMD_SET_NET:
        if (len != 16) { send_err(type, ET_ERR_BAD_LEN, "need 16 bytes"); break; }
        board_netif_set(&p[0], &p[4], &p[8]);
        IP4_ADDR(&g_victim, p[12], p[13], p[14], p[15]);
        send_ack(type);
        break;

    case ET_CMD_SET_TARGET:
        if (len != 5) { send_err(type, ET_ERR_BAD_LEN, "need 5 bytes"); break; }
        if (p[0] != ET_PROTO_UDP && p[0] != ET_PROTO_TCP)
        {
            send_err(type, ET_ERR_BAD_ARG, "proto");
            break;
        }
        /* Changing transport or port drops any open connection: it belonged to
         * the old target and reusing it would measure the wrong victim. */
        if (p[0] != g_proto || get_u16(&p[1]) != g_port) { et_tcp_close(); }
        g_proto      = p[0];
        g_port       = get_u16(&p[1]);
        g_timeout_ms = get_u16(&p[3]);
        send_ack(type);
        break;

    case ET_CMD_SET_REQUEST:
        if (len > ET_MAX_REQ) { send_err(type, ET_ERR_TOO_BIG, "request"); break; }
        memcpy(g_req, p, len);
        g_req_len = len;
        g_req_crc = et_crc16(g_req, g_req_len);
        send_ack(type);
        break;

    case ET_CMD_SET_WINDOW:
        if (len != 4 && len != 6)
        {
            send_err(type, ET_ERR_BAD_LEN, "need 4 or 6 bytes");
            break;
        }
        if (get_u16(&p[2]) > ET_MAX_WIN) { send_err(type, ET_ERR_TOO_BIG, "window"); break; }
        g_win_off = get_u16(&p[0]);
        g_win_len = get_u16(&p[2]);
        /* 4 bytes leaves `want` at win_off + win_len, which is what a datagram
         * transport wants: the reply is whole when it arrives. */
        g_want = (len == 6) ? get_u16(&p[4]) : 0u;
        if (g_want && g_want > ET_MAX_REPLY)
        {
            send_err(type, ET_ERR_TOO_BIG, "want exceeds the reply buffer");
            break;
        }
        if (g_want && g_want < (uint16_t)(g_win_off + g_win_len))
        {
            /* A `want` shorter than the window means every exchange stops
             * collecting before the bytes it is meant to keep have arrived,
             * and every one is counted short.  Refuse it here rather than
             * report it 4 000 times. */
            send_err(type, ET_ERR_WINDOW, "want is shorter than the window");
            break;
        }
        send_ack(type);
        break;

    case ET_CMD_SET_BANK:
    {
        uint16_t i = 1;                  /* p[0] is flags */
        uint8_t  code = ET_ERR_NONE;
        const char *why = "";

        if (!len) { send_err(type, ET_ERR_BAD_LEN, "need flags"); break; }
        if (p[0] & ET_BANK_RESET)
        {
            g_bank_n = 0; g_bank_used = 0; g_bank_crc = 0;
        }
        while (i + 2u <= len && code == ET_ERR_NONE)
        {
            uint16_t elen = get_u16(&p[i]);
            i = (uint16_t)(i + 2u);
            if ((uint32_t)i + elen > len)
            { code = ET_ERR_BAD_LEN; why = "entry runs past the frame"; break; }
            if (elen > ET_MAX_REQ)
            { code = ET_ERR_TOO_BIG; why = "entry"; break; }
            if (g_bank_n >= ET_BANK_MAX)
            { code = ET_ERR_BANK; why = "bank full (entries)"; break; }
            if (g_bank_used + elen > ET_BANK_BYTES)
            { code = ET_ERR_BANK; why = "bank full (bytes)"; break; }
            memcpy(&g_bank[g_bank_used], &p[i], elen);
            g_bank_off[g_bank_n] = (uint16_t)g_bank_used;
            g_bank_len[g_bank_n] = elen;
            g_bank_n++;
            g_bank_used += elen;
            i = (uint16_t)(i + elen);
        }
        /* The CRC is over the whole pool, computed once per frame rather than
         * once per entry -- it is what makes the upload verifiable, since the
         * host runs the same CRC over what it meant to send and compares it
         * against GET_CONFIG.  A dropped frame is then caught before a capture
         * rather than showing up afterwards as a bank that played the wrong
         * sequence numbers. */
        g_bank_crc = g_bank_used ? et_crc16(g_bank, g_bank_used) : 0u;
        if (code == ET_ERR_NONE) { send_ack(type); }
        else                     { send_err(type, code, why); }
        break;
    }

    case ET_CMD_TCP_CONNECT:
        if (g_proto != ET_PROTO_TCP)
        {
            send_err(type, ET_ERR_BAD_ARG, "set_target proto=TCP first");
            break;
        }
        if (link_speed() == ET_LINK_DOWN) { send_err(type, ET_ERR_LINK_DOWN, "link"); break; }
        if (et_tcp_open()) { send_ack(type); }
        else { send_err(type, ET_ERR_TCP, "connect failed"); }
        break;

    case ET_CMD_TCP_CLOSE:
        et_tcp_close();
        send_ack(type);
        break;

    case ET_CMD_RELAY:
    {
        /* One exchange, the host in the loop, the WHOLE reply returned.  This
         * is what a handshake needs -- the host cannot pre-generate a record
         * whose content depends on the one before it -- and it is how a TLS
         * session is brought up before the bulk phase switches to the bank. */
        uint32_t dt_hw = 0, dt_sw = 0, rl;
        uint16_t want_n;
        uint8_t  out[12];
        if (len < 2) { send_err(type, ET_ERR_BAD_LEN, "need want u16"); break; }
        if (g_proto != ET_PROTO_TCP)
        {
            send_err(type, ET_ERR_BAD_ARG, "RELAY is TCP-only");
            break;
        }
        if (g_tcp_state != 2) { send_err(type, ET_ERR_TCP, "not connected"); break; }
        want_n = get_u16(&p[0]);
        rl = tcp_exchange(&p[2], (uint16_t)(len - 2u), want_n, &dt_hw, &dt_sw);
        put_u32(&out[0], dt_hw);
        put_u32(&out[4], dt_sw);
        put_u16(&out[8], (uint16_t)rl);
        put_u16(&out[10], 0);
        /* Header and body in one frame so the host never has to correlate two.
         * g_trx is drained here, and only here, for the same reason the
         * capture loop drains it: bytes that arrive between commands belong to
         * the stream and discarding them corrupts it. */
        {
            uint8_t *buf = g_ring;               /* borrow the ring: no capture is running */
            uint32_t total = 12u + rl;
            if (total > ET_RING_BYTES) { total = ET_RING_BYTES; }
            memcpy(buf, out, 12);
            if (total > 12u) { memcpy(buf + 12, g_trx, total - 12u); }
            frame_send(ET_RSP_RELAY, buf, (uint16_t)total);
        }
        g_trx_len = 0;
        break;
    }

    case ET_CMD_ONESHOT:
    {
        uint32_t seq0 = g_rx_seq;
        const uint8_t *req = p;
        uint16_t rlen = len;
        if (!rlen) { req = g_req; rlen = g_req_len; }
        if (!rlen) { send_err(type, ET_ERR_NO_REQUEST, "no request"); break; }
        (void)transport_send(req, rlen);
        if (!transport_wait(seq0, g_timeout_ms))
        {
            frame_send(ET_RSP_ONESHOT, 0, 0);       /* empty = no reply */
        }
        else
        {
            frame_send(ET_RSP_ONESHOT, g_rx_buf, g_rx_len);
        }
        break;
    }

    case ET_CMD_RUN:
        if (len != 8) { send_err(type, ET_ERR_BAD_LEN, "need 8 bytes"); break; }
        /* Either source of requests will do: a bank to play, or a single held
         * request to repeat. */
        if (!g_req_len && !g_bank_n)
        { send_err(type, ET_ERR_NO_REQUEST, "no request and no bank"); break; }
        if (link_speed() == ET_LINK_DOWN) { send_err(type, ET_ERR_LINK_DOWN, "link"); break; }
        if (g_proto == ET_PROTO_TCP && g_tcp_state != 2)
        { send_err(type, ET_ERR_TCP, "not connected"); break; }
        do_run(get_u32(&p[0]), get_u32(&p[4]));
        break;

    case ET_CMD_RESET:
        send_ack(type);
        /* Let the ACK drain before the core resets. */
        HAL_Delay(50);
        NVIC_SystemReset();
        break;

    default:
        send_err(type, ET_ERR_BAD_ARG, "unknown command");
        break;
    }
}

/* ------------------------------------------------------------- frame in --- */

/* Big enough for a SET_BANK frame carrying many entries, not just for one
 * request: an ACK per entry would be a serial round trip per entry, which is
 * what holds a host-in-the-loop relay to tens of exchanges a second. */
static uint8_t  rx_frame[ET_MAX_FRAME_IN + 16];
static uint16_t rx_n;
static uint16_t rx_want;
static uint8_t  rx_state;               /* 0 magic0, 1 magic1, 2 header, 3 body */

void et_poll(void)
{
    int c;
    while ((c = uart_getchar_nb()) >= 0)
    {
        uint8_t b = (uint8_t)c;
        switch (rx_state)
        {
        case 0:
            if (b == ET_MAGIC0) { rx_state = 1; }
            break;
        case 1:
            /* A stray 'E' before the real one must not desynchronise us. */
            if (b == ET_MAGIC1)      { rx_state = 2; rx_n = 0; }
            else if (b == ET_MAGIC0) { rx_state = 1; }
            else                     { rx_state = 0; }
            break;
        case 2:
            rx_frame[rx_n++] = b;
            if (rx_n == 3)
            {
                rx_want = get_u16(&rx_frame[1]);
                if (rx_want > sizeof(rx_frame) - 5u)
                {
                    send_err(rx_frame[0], ET_ERR_TOO_BIG, "frame");
                    rx_state = 0;
                }
                else { rx_state = 3; }
            }
            break;
        case 3:
            rx_frame[rx_n++] = b;
            if (rx_n == (uint16_t)(3 + rx_want + 2))
            {
                uint16_t want = get_u16(&rx_frame[3 + rx_want]);
                uint16_t have = et_crc16(rx_frame, (uint32_t)(3 + rx_want));
                if (want == have) { handle(rx_frame[0], &rx_frame[3], rx_want); }
                else              { send_err(rx_frame[0], ET_ERR_BAD_ARG, "crc"); }
                rx_state = 0;
            }
            break;
        default:
            rx_state = 0;
            break;
        }
    }
}

/* ------------------------------------------------------------------ init -- */

void et_init(void)
{
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
#if BOARD_DWT_NEEDS_UNLOCK
    /* The M7's DWT is lock-protected; without this CYCCNT reads 0 forever and
     * every measurement silently becomes a constant. */
    DWT->LAR = 0xC5ACCE55u;
#endif
    DWT->CYCCNT = 0;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

    IP4_ADDR(&g_victim, VICTIM_IP0, VICTIM_IP1, VICTIM_IP2, VICTIM_IP3);

    g_pcb = udp_new();
    udp_bind(g_pcb, IP_ADDR_ANY, 40000);
    udp_recv(g_pcb, udp_rx, NULL);
}
