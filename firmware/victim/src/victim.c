/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* The reference responder: answer a UDP request, and say how long that took.
 *
 * See inc/et_victim.h for the wire format and for what the host can and cannot
 * conclude from it.
 *
 * THE DESIGN DECISION WORTH ARGUING WITH. This runs on lwIP, in the main loop,
 * not as a hand-written frame handler in the receive interrupt. An interrupt
 * handler that pokes a pre-armed transmit descriptor would answer in a few
 * hundred nanoseconds with almost no variation, and that sounds like the right
 * thing for a jitter reference.
 *
 * It is the wrong thing here, for two reasons.
 *
 * First, it would have to share the Ethernet DMA descriptor rings with lwIP --
 * which still has to answer ARP, or the instrument cannot find this board at
 * all -- and reaching into the HAL's descriptor bookkeeping from an interrupt
 * while lwIP walks the same ring from the main loop is a race whose symptom is
 * a receive path that stops. The whole point of a reference responder is that
 * you can believe it.
 *
 * Second, and this is the part that makes the first one cheap: **it does not
 * matter, because the responder reports its own interval.** `rx_cyc` is
 * latched in the Ethernet receive interrupt, before any software has run, and
 * `tx_cyc` immediately before the reply is handed to lwIP. Whatever happens
 * between them -- loop latency, a pbuf allocation taking a slower path, an
 * interrupt arriving -- is inside `tx_cyc - rx_cyc` and the host subtracts it.
 * A responder that is slow but honest is more useful than one that is fast and
 * assumed constant, and this one measures itself every single exchange.
 *
 * What is NOT inside that interval, and so has to be constant rather than
 * measured, is the MAC's own transmit path: from lwIP handing the frame on to
 * the first bit on the wire. That is descriptor setup and serialisation, it
 * does not branch on anything, and it is why the absolute path number is a
 * latency plus a constant rather than a latency. The jitter is unaffected.
 *
 * SO THE MAIN LOOP DOES AS LITTLE AS POSSIBLE. It pumps lwIP, it rate-limits
 * the PHY read (tens of microseconds of MDIO, which must not land inside an
 * exchange), and it prints only when asked. There is no console polling, no
 * periodic statistics, nothing on a timer.
 */
#include <string.h>

#include "board.h"
#include "et_victim.h"
#include "pwcheck.h"
#include "lwip/udp.h"
#include "lwip/pbuf.h"
#include "lwip/ip_addr.h"

/* The reply, built in place and sent as-is.  Static, so no allocation happens
 * on the measured path beyond lwIP's own pbuf for the send. */
static uint8_t  g_rsp[ETV_MAX_REPLY];
static uint16_t g_rsp_len = ETV_RSP_HDR;

static struct udp_pcb *g_pcb;

/* THE RESPONDER'S OWN ADDRESS, and it must not be the instrument's.
 *
 * The instrument's address is runtime state -- the host sends SET_NET and it is
 * verified by read-back -- so one instrument binary drives any subnet. This has
 * no control link at all, by design: a command channel is a thing that could
 * run while an exchange is being measured. So its address is set here, once, at
 * start-up.
 *
 * .10 rather than the netif's compiled-in default, which is .20 on both boards
 * because both inherit the same main.h. Two boards at one address on a direct
 * cable is an ARP cache that answers for the wrong one, and the symptom is a
 * capture that works until it doesn't. .10 is also where every demo README
 * already says the measured device is.
 *
 * Override the whole address at build time when the subnet is somebody else's:
 *
 *     make APP=victim BOARD=f429 EXTRA_DEFS='-DETV_IP2=0 -DETV_IP3=77'
 */
#ifndef ETV_IP0
#define ETV_IP0  192u
#endif
#ifndef ETV_IP1
#define ETV_IP1  168u
#endif
#ifndef ETV_IP2
#define ETV_IP2  7u
#endif
#ifndef ETV_IP3
#define ETV_IP3  10u
#endif

static uint32_t g_seq;        /* replies sent; the host's loss/reorder detector */
static uint32_t g_seen;       /* frames delivered to this port, valid or not    */
static uint32_t g_bad_magic;
static uint32_t g_bad_ver;
static uint32_t g_short;      /* frames too short to be a request at all        */
static uint32_t g_pw;         /* password checks actually run                   */

/* Set by a ETV_CMD_STATUS request, consumed by etv_poll().  A flag rather than
 * a print: printing from the receive callback would put a UART transmit inside
 * the interval the host is measuring, which is exactly the kind of thing this
 * firmware exists not to do. */
static volatile uint32_t g_status_req;

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v);
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* ------------------------------------------------------------------ reply -- *
 *
 * Everything that does not change between exchanges is written once, here, so
 * that the measured path writes five words and nothing else.
 */
static void rsp_template(void)
{
    memset(g_rsp, 0, sizeof(g_rsp));
    put_u32(&g_rsp[0], ETV_MAGIC_RSP);
    g_rsp[4] = (uint8_t)ETV_VERSION;
    put_u32(&g_rsp[24], board_cyccnt_hz());
    /* The padding is left as zeroes deliberately: a reply whose padding is
     * uninitialised stack or heap would leak whatever was there, and a host
     * checking the frame it got back cannot tell noise from a window that
     * landed in the wrong place. */
}

/* ----------------------------------------------------------- the fast path -- *
 *
 * lwIP calls this from `ethernetif_input()`, which the main loop calls.  By the
 * time it runs, `g_rx_cyc` has already been latched by the Ethernet receive
 * interrupt -- that is the start of the interval, and it is taken before any of
 * this code exists.
 */
static void on_udp(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                   const ip_addr_t *addr, u16_t port)
{
    /* Big enough for the header AND a password guess. Only the header is
     * copied for a MEASURE request, so that path's timing is unchanged by this
     * buffer existing. */
    uint8_t  req[ETV_PW_GUESS_OFF + ETV_PW_MAX_GUESS];
    uint16_t copied, want, have;
    uint32_t tag, rx;
    uint8_t  flags, cmd;
    struct pbuf *q;

    (void)arg;

    g_seen++;

    /* RELEASE COMPLETED TRANSMIT DESCRIPTORS FIRST, EVERY TIME.
     *
     * ST's NO_SYS lwIP port only sweeps them when a send finds the ring full,
     * so one exchange in ETH_TX_DESC_CNT pays for the sweep and the others do
     * not. Measured here before this line existed, 20 000 exchanges with the
     * descriptor count at 4: the path figure was BIMODAL, 14 940 records at
     * 21.1 us and 5 000 at 27.6 us -- a 25 % population exactly 6.5 us slower,
     * which is one frame time at 100 Mbit. A quarter of the capture in a
     * second mode is not noise and it is not the network; it is the responder,
     * and it would have been read as the network.
     *
     * Doing it here, at the top of the handler, puts the cost INSIDE the
     * interval this responder reports -- so it is subtracted rather than left
     * in the path figure -- and leaves the send itself never finding the ring
     * full. Paying a cost every exchange to stop paying it unpredictably every
     * fourth is the right trade for a reference: this firmware's job is to be
     * boring, not fast.
     *
     * The instrument does the same thing on its TCP path and for the same
     * reason; `board_tx_release()` exists for it. */
    board_tx_release();

    if (p->tot_len < ETV_REQ_HDR) { g_short++; pbuf_free(p); return; }

    /* Only the header is copied out, never the padding: the padding's job is to
     * make the frame longer on the wire, and touching it would make the
     * responder's own interval grow with frame size for no reason. */
    copied = pbuf_copy_partial(p, req, ETV_REQ_HDR, 0);
    want   = p->tot_len;
    have   = want;

    /* A PASSWORD GUESS NEEDS MORE THAN THE HEADER, and the pbuf is about to go.
     * Done as a second copy conditional on the command rather than by always
     * copying more, so that a MEASURE request still copies exactly twelve bytes
     * -- the baseline a PWCHECK capture is compared against has to be the same
     * code path minus the check, not minus the check and a longer memcpy. */
    cmd = (copied >= ETV_REQ_HDR) ? req[5] : (uint8_t)ETV_CMD_MEASURE;
    if (cmd == (uint8_t)ETV_CMD_PWCHECK || cmd == (uint8_t)ETV_CMD_PWCHECK_CT)
    {
        copied = pbuf_copy_partial(p, req, (u16_t)sizeof(req), 0);
    }
    pbuf_free(p);

    if (copied < ETV_REQ_HDR)                      { g_short++;     return; }
    if (get_u32(req) != ETV_MAGIC_REQ)             { g_bad_magic++; return; }
    if (req[4] != (uint8_t)ETV_VERSION)            { g_bad_ver++;   return; }

    tag = get_u32(&req[8]);
    if (cmd == (uint8_t)ETV_CMD_STATUS) { g_status_req++; }

    /* The reply mirrors the request's length, bounded by what one frame holds
     * and floored at the header.  Clamped rather than refused: a host sweeping
     * frame size should get a reply at every size it asks for, and the length
     * it actually got is on the wire for it to see. */
    if (want < ETV_RSP_HDR)    { want = ETV_RSP_HDR; }
    if (want > ETV_MAX_REPLY)  { want = ETV_MAX_REPLY; }
    g_rsp_len = want;

    rx = g_rx_cyc;

    flags = 0u;
    if (board_link_speed() == 4u) { flags |= ETV_F_LINK_100F; }

    /* THE WORK WHOSE DURATION IS THE MEASUREMENT.
     *
     * Between rx (latched in the Ethernet interrupt, before any software ran)
     * and tx_cyc (taken below), so this responder's own reported interval
     * contains it. See firmware/common/pwcheck.h.
     *
     * A WORD OF WARNING, which demos/password/README.md repeats: on THIS
     * responder the leak is real and almost certainly not recoverable. The step
     * is about 30 ns per byte and this responder's own interval varies by
     * hundreds of nanoseconds, so averaging it out takes millions of exchanges
     * per candidate byte. Use the bare-metal responder for the demo; this one
     * is here so the command exists on every board, and so that the difference
     * between the two is a thing you can measure rather than be told. */
    if (cmd == (uint8_t)ETV_CMD_PWCHECK || cmd == (uint8_t)ETV_CMD_PWCHECK_CT)
    {
        uint32_t glen = req[ETV_PW_OFF];

        if (glen > ETV_PW_MAX_GUESS || (ETV_PW_GUESS_OFF + glen) > have)
        {
            /* No check was run, so this record's timing means nothing. Said in
             * a flag rather than by replying differently, because a different
             * reply would be a second channel. */
            flags |= (uint8_t)ETV_F_PW_BAD_REQ;
        }
        else
        {
            int ok = (cmd == (uint8_t)ETV_CMD_PWCHECK)
                   ? pw_check_early(&req[ETV_PW_GUESS_OFF], glen)
                   : pw_check_const(&req[ETV_PW_GUESS_OFF], glen);
            if (ok) { flags |= (uint8_t)ETV_F_PW_MATCH; }
            g_pw++;
        }
    }

    put_u32(&g_rsp[8],  tag);
    put_u32(&g_rsp[12], ++g_seq);
    put_u32(&g_rsp[16], rx);
    put_u32(&g_rsp[28], g_seen);

    q = pbuf_alloc(PBUF_TRANSPORT, g_rsp_len, PBUF_RAM);
    if (!q) { return; }           /* counted by the host as a timeout, correctly:
                                   * no reply was sent and none is coming */

    /* tx_cyc IS TAKEN AS LATE AS IT CAN BE AND STILL BE IN THE REPLY: after the
     * buffer exists, before anything is copied into it.  The copy and the send
     * come after and are therefore NOT in the reported interval -- they are
     * part of the constant that the path number carries, along with the MAC's
     * transmit path and the serialisation.  Putting them inside the interval
     * instead would mean reading the counter after the frame had gone, which
     * cannot then be put in that frame.
     *
     * No barrier is needed to keep this store ahead of the memcpy below: the
     * memcpy reads `g_rsp`, and a compiler may not move a read of an object
     * ahead of a write to it. */
    put_u32(&g_rsp[20], DWT->CYCCNT);

    if (get_u32(&g_rsp[20]) < rx) { g_rsp[5] = (uint8_t)(flags | ETV_F_CYC_WRAP); }
    else                          { g_rsp[5] = flags; }

    memcpy(q->payload, g_rsp, g_rsp_len);
    udp_sendto(pcb, q, addr, port);
    pbuf_free(q);
}

void etv_init(void)
{
    {
        static const uint8_t ip[4]   = { ETV_IP0, ETV_IP1, ETV_IP2, ETV_IP3 };
        static const uint8_t mask[4] = { NETMASK_ADDR0, NETMASK_ADDR1,
                                         NETMASK_ADDR2, NETMASK_ADDR3 };
        static const uint8_t gw[4]   = { GW_ADDR0, GW_ADDR1, GW_ADDR2, GW_ADDR3 };
        board_netif_set(ip, mask, gw);
    }

    rsp_template();

    g_pcb = udp_new();
    if (!g_pcb) { uart_puts("victim: udp_new failed\r\n"); return; }
    if (udp_bind(g_pcb, IP_ANY_TYPE, (u16_t)ETV_PORT) != ERR_OK)
    {
        uart_puts("victim: udp_bind failed\r\n");
        return;
    }
    udp_recv(g_pcb, on_udp, NULL);

    if (!board_dwt_ok())
    {
        /* Loud, and before anything else, because every interval this
         * responder reports would be exactly zero -- and a zero interval is
         * not an error downstream, it is a path figure with the whole round
         * trip left in it. See board.c's dwt_enable(). */
        uart_puts("\r\n*** CYCLE COUNTER NOT RUNNING: every reported interval "
                  "will be 0 and every path figure will be the whole round "
                  "trip. Power-cycle the board. ***\r\n");
    }

    uart_puts("\r\n" BOARD_NAME " ethtimer reference responder v");
    uart_putdec(ETV_VERSION);
    uart_puts("  UDP port ");
    uart_putdec(ETV_PORT);
    uart_puts("\r\n  cycle counter ");
    uart_putdec(board_cyccnt_hz() / 1000000u);
    uart_puts(" MHz\r\n");
    {
        uint8_t ip[4], mask[4], gw[4];
        int i;
        board_netif_get(ip, mask, gw);
        uart_puts("  address ");
        for (i = 0; i < 4; i++)
        {
            uart_putdec(ip[i]);
            uart_puts(i < 3 ? "." : "\r\n");
        }
    }
    uart_puts("  no reply is sent until a request arrives; nothing is printed "
              "while measuring\r\n");
}

static const char *speed_name(uint8_t s)
{
    switch (s)
    {
        case 4u: return "100F";
        case 3u: return "100H (not full duplex: collisions are back)";
        case 2u: return "10F  (ten times slower on the wire)";
        case 1u: return "10H  (ten times slower on the wire)";
        default: return "down";
    }
}

void etv_poll(void)
{
    static uint32_t served;
    static uint8_t  last_link = 0xFFu;    /* 0xFF: nothing reported yet */
    uint8_t link = board_link_speed();

    /* THE LINK IS REPORTED ON CHANGE, NOT ON A TIMER.
     *
     * Edge-triggered for two reasons. A periodic print would put a UART
     * transmit -- tens of microseconds a line at 921 600 baud -- inside
     * somebody's jitter histogram at regular intervals, which is a pattern
     * that looks exactly like a path artefact. And the thing worth knowing is
     * not the link's state, it is that it CHANGED: a link that renegotiated
     * mid-capture explains a distribution with two modes in it, and nothing
     * else in the output would say so.
     *
     * It is also why the start-up banner no longer prints a speed. At
     * `etv_init()` the PHY has had a few milliseconds since reset and
     * autonegotiation takes a second or more, so the banner used to report
     * 100H -- and shout about it -- on a link that settled at 100F a moment
     * later. A first reading taken too early is worse than no reading: it is a
     * warning about nothing, and this file's whole job is to not do that. */
    if (link != last_link)
    {
        last_link = link;
        uart_puts("victim: link ");
        uart_puts(speed_name(link));
        uart_puts("\r\n");
    }

    if (g_status_req == served) { return; }
    served = g_status_req;

    uart_puts("victim: seq=");         uart_putdec(g_seq);
    uart_puts(" seen=");               uart_putdec(g_seen);
    uart_puts(" short=");              uart_putdec(g_short);
    uart_puts(" bad_magic=");          uart_putdec(g_bad_magic);
    uart_puts(" bad_ver=");            uart_putdec(g_bad_ver);
    uart_puts(" pw=");                 uart_putdec(g_pw);
    uart_puts(" reply_len=");          uart_putdec(g_rsp_len);
    uart_puts(" link=");               uart_putdec(board_link_speed());
    uart_puts("\r\n");
}
