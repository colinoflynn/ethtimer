/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* The reference responder with no TCP/IP stack: reply from the interrupt.
 *
 * Same wire format as firmware/victim -- same magic, same fields, same offsets,
 * the same `--target jitter` on the host -- so the two are directly comparable
 * and that comparison is the point. See ../victim/inc/et_victim.h.
 *
 * WHAT THIS DOES DIFFERENTLY, and why it is worth a second firmware.
 *
 * The lwIP responder answers from the main loop: the Ethernet interrupt sets a
 * flag, `ethernetif_input()` lifts the frame into a pbuf, lwIP routes it, the
 * UDP callback builds a reply, `udp_sendto` allocates another pbuf and hands it
 * to the HAL. Measured, that is about 165 us with 62 us of variation -- all of
 * which it reports, so the host subtracts it and the PATH figure comes out
 * clean at 21.378 us +/- 0.082.
 *
 * But not all of it is reported. The interval a responder can put in its own
 * reply has to END before the frame leaves, so everything from "hand it to the
 * driver" to "first bit on the wire" is outside it and lands in the path figure
 * as a constant. With lwIP that tail is a pbuf allocation, a copy, a route
 * lookup and HAL_ETH_Transmit_IT's descriptor bookkeeping -- and it is only a
 * *constant* to the extent that none of those branch.
 *
 * Here there is no tail worth the name. The reply frame is built once at
 * start-up, complete with Ethernet, IP and UDP headers. A transmit descriptor
 * is pre-armed to point at it. The receive interrupt latches the cycle counter,
 * reads about twenty bytes of the incoming frame to decide it is ours, stores
 * four words into the reply, and sets one OWN bit. There is no allocation, no
 * copy, no stack, and nothing in the path between the interrupt and the MAC
 * that can take a different number of cycles on different exchanges.
 *
 * THE IP CHECKSUM IS DONE BY THE MAC. ETH_DMATXDESC_CIC_IPV4HEADER makes the
 * transmit DMA compute and insert the header checksum, so the reply length can
 * mirror the request's -- which is the frame-size sweep -- without the hot path
 * computing anything. The UDP checksum is left at zero, which IPv4 permits and
 * every stack accepts.
 *
 * F429 ONLY, and that is not laziness. The whole file is the F4 Ethernet DMA's
 * descriptor format and its DMASR/DMATPDR registers; the H7's DMA is a
 * different generation with different descriptors and a per-channel status
 * register. A board-generic version of this would be a board-generic version of
 * the thing it exists to avoid. The lwIP responder is the portable one and it
 * builds for every board.
 *
 * WHAT IS NOT HANDLED, deliberately: IP fragmentation, IP options, VLAN tags,
 * ICMP, DHCP, TCP, and any frame that is not an ARP request for this address or
 * a UDP datagram to this port. Everything else is dropped without being looked
 * at twice. A responder that parses more is a responder with more branches in
 * it.
 */
#include <string.h>

#include "board.h"
#include "et_victim.h"
#include "lan8742.h"
#include "rawnet.h"

/* ------------------------------------------------------------- addresses -- */

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

static const uint8_t MY_IP[4]  = { ETV_IP0, ETV_IP1, ETV_IP2, ETV_IP3 };
/* Locally administered (bit 1 of the first octet), so it cannot collide with a
 * real vendor's. Different from the lwIP responder's by one byte, so that both
 * can sit on one segment while they are being compared. */
static const uint8_t MY_MAC[6] = { 0x02u, 0x00u, 0x00u, 0x00u, 0x00u, 0x02u };

/* ------------------------------------------------------------- the rings -- */

/* Small on purpose. Four of each is what the HAL's own port uses, and a deeper
 * receive ring would only let the DMA queue frames this responder is about to
 * answer anyway -- a queue is latency it cannot report. */
#define RX_N   4u
#define TX_N   4u
#define BUF_SZ 1536u

static ETH_DMADescTypeDef rx_desc[RX_N];
static ETH_DMADescTypeDef tx_desc[TX_N];
static uint8_t rx_buf[RX_N][BUF_SZ] __attribute__((aligned(4)));

/* THE REPLY LIVES IN ONE BUFFER, BUILT ONCE. Its transmit descriptor points at
 * it permanently; sending is setting a length and an OWN bit. */
static uint8_t  tx_reply[BUF_SZ] __attribute__((aligned(4)));
/* A second buffer for everything that is not the measured reply -- which is
 * only ARP. Separate so that an ARP reply can never be sitting in the buffer a
 * measured reply is about to be built in. */
static uint8_t  tx_other[BUF_SZ] __attribute__((aligned(4)));

static volatile uint32_t rx_idx;      /* next descriptor we expect to own */
static volatile uint32_t tx_idx;      /* next descriptor to hand to the DMA */

ETH_HandleTypeDef EthHandle;          /* the board's it.c declares this extern */
static lan8742_Object_t LAN8742;

/* ------------------------------------------------------------- the reply -- */

/* Byte offsets into a reply frame. Ethernet 14, IPv4 20, UDP 8 = 42, then the
 * payload this project's wire format defines. */
#define ETH_HDR   14u
#define IP_HDR    20u
#define UDP_HDR    8u
#define PAY_OFF   (ETH_HDR + IP_HDR + UDP_HDR)      /* 42 */

#define IP_TOTLEN_OFF (ETH_HDR + 2u)
#define IP_CSUM_OFF   (ETH_HDR + 10u)
#define UDP_LEN_OFF   (ETH_HDR + IP_HDR + 4u)

static uint16_t reply_len;            /* whole frame, including headers */

static uint32_t g_seq;                /* replies sent */
static uint32_t g_seen;               /* frames accepted on our port */
static uint32_t g_arp;                /* ARP requests answered */
static uint32_t g_drop;               /* frames looked at and not for us */
/* WHY THE REJECT PATH IS COUNTED BY REASON. "A frame arrived and was not
 * answered" has six causes here and they are six different repairs: a cable
 * carrying something else, an IP header this does not parse, a port mismatch, a
 * stale protocol version, a frame too short, or a receive ring that has lost
 * step with the DMA. One counter cannot tell them apart, and the first thing
 * anyone asks of a silent responder is which of those it is. */
static uint32_t g_frames;             /* descriptors processed, total */
static uint32_t g_n_short;            /* too short to be a request at all */
static uint32_t g_n_notip;            /* not IPv4 and not ARP */
static uint32_t g_n_notudp;           /* IPv4 but not UDP, or with options */
static uint32_t g_n_port;             /* UDP to some other port */
static uint32_t g_n_magic;            /* our port, wrong magic or version */

/* The peer this responder is answering. Learned from the first request and
 * from any ARP request; a change costs one slow rebuild of the template and
 * then nothing. */
static uint8_t peer_mac[6];
static uint8_t peer_ip[4];
static uint16_t peer_port;
static uint8_t  peer_known;

static void put16be(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void put32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static uint32_t get32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint16_t get16be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

/* Build everything in the reply that does not change between exchanges.
 *
 * Called once at start-up and again whenever the peer or the frame length
 * changes -- both of which happen at most once per capture, never inside one.
 * The hot path writes five words and no more. */
static void reply_template(uint16_t payload_len)
{
    uint8_t *f = tx_reply;
    uint16_t ip_total  = (uint16_t)(IP_HDR + UDP_HDR + payload_len);
    uint16_t udp_total = (uint16_t)(UDP_HDR + payload_len);

    memset(f, 0, PAY_OFF);

    memcpy(&f[0], peer_mac, 6);
    memcpy(&f[6], MY_MAC, 6);
    put16be(&f[12], 0x0800u);                       /* IPv4 */

    f[ETH_HDR + 0] = 0x45u;                         /* v4, 20-byte header */
    f[ETH_HDR + 1] = 0x00u;                         /* DSCP/ECN */
    put16be(&f[IP_TOTLEN_OFF], ip_total);
    put16be(&f[ETH_HDR + 4], 0x0000u);              /* identification */
    put16be(&f[ETH_HDR + 6], 0x4000u);              /* don't fragment */
    f[ETH_HDR + 8] = 64u;                           /* TTL */
    f[ETH_HDR + 9] = 17u;                           /* UDP */
    /* The header checksum is left at zero: the transmit DMA computes and
     * inserts it, because the descriptor asks for IPv4 header insertion. That
     * is what lets the reply length follow the request's without this path
     * computing anything. */
    put16be(&f[IP_CSUM_OFF], 0x0000u);
    memcpy(&f[ETH_HDR + 12], MY_IP, 4);
    memcpy(&f[ETH_HDR + 16], peer_ip, 4);

    put16be(&f[ETH_HDR + IP_HDR + 0], (uint16_t)ETV_PORT);   /* source */
    put16be(&f[ETH_HDR + IP_HDR + 2], peer_port);            /* destination */
    put16be(&f[UDP_LEN_OFF], udp_total);
    /* UDP checksum stays zero. IPv4 makes it optional and zero means "not
     * computed"; computing one would be a pass over the payload on the hot
     * path, which is the one thing this firmware is for not doing. */
    put16be(&f[ETH_HDR + IP_HDR + 6], 0x0000u);

    /* The payload's constant fields. */
    memset(&f[PAY_OFF], 0, payload_len);
    put32le(&f[PAY_OFF + 0], ETV_MAGIC_RSP);
    f[PAY_OFF + 4] = (uint8_t)ETV_VERSION;
    put32le(&f[PAY_OFF + 24], board_cyccnt_hz());

    reply_len = (uint16_t)(PAY_OFF + payload_len);
}

/* --------------------------------------------------------------- sending -- */

/* Hand a descriptor to the DMA and poke it. Four stores and a register write.
 *
 * `cic` asks the MAC to insert the IPv4 header checksum for the measured reply;
 * ARP has no IP header and asks for nothing. */
static void tx_send(const uint8_t *buf, uint16_t len, uint32_t cic)
{
    ETH_DMADescTypeDef *d = &tx_desc[tx_idx];

    d->DESC2 = (uint32_t)buf;
    d->DESC1 = len;
    d->DESC0 = ETH_DMATXDESC_OWN | ETH_DMATXDESC_FS | ETH_DMATXDESC_LS
             | ETH_DMATXDESC_TCH | ETH_DMATXDESC_IC | cic;

    tx_idx = (tx_idx + 1u) % TX_N;

    /* Clear the "transmit buffer unavailable" status and poll-demand, which is
     * what restarts a suspended transmit DMA. Both are single writes; neither
     * reads back. */
    ETH->DMASR   = ETH_DMASR_TBUS;
    ETH->DMATPDR = 0u;
}

/* ----------------------------------------------------------- the hot path -- */

/* Called from ETH_IRQHandler with the cycle counter already latched.
 *
 * Everything below is straight-line but for the three tests that decide the
 * frame is ours, and those are the same three tests on every exchange. The
 * reply is already built; what changes is four words.
 */
int board_eth_isr_hook(uint32_t cyc, uint32_t dmasr)
{
    ETH_DMADescTypeDef *d;
    const uint8_t *f;
    uint32_t len;
    uint8_t handled = 0u;

    if (!(dmasr & ETH_DMASR_RS))
    {
        /* Not a receive. Let the transmit-complete and error paths alone --
         * the board's ISR has already latched g_tx_cyc, and nothing here needs
         * to know. Clearing TS and returning keeps the HAL out of it. */
        ETH->DMASR = dmasr & (ETH_DMASR_TS | ETH_DMASR_NIS | ETH_DMASR_AIS
                              | ETH_DMASR_TBUS | ETH_DMASR_RBUS);
        return 1;
    }

    /* Walk every descriptor the DMA has finished with. More than one per
     * interrupt is normal under load and dropping the extras would be a frame
     * the host counts as a timeout. */
    for (;;)
    {
        d = &rx_desc[rx_idx];
        if (d->DESC0 & ETH_DMARXDESC_OWN) { break; }

        f   = (const uint8_t *)d->DESC2;
        /* THE ETHERNET FRAME LENGTH IS NOT THE PAYLOAD LENGTH, and working back
         * from it is how the first version of this replied 60 bytes to a
         * 64-byte request. Two reasons, both invisible from the descriptor:
         *
         *  - the MAC strips the 4-byte CRC from type frames (MACCR's CSTF),
         *    so subtracting 4 subtracts it twice;
         *  - a frame shorter than 60 bytes arrives padded, so the Ethernet
         *    length over-reports a short datagram.
         *
         * So `len` is used only as a floor -- "is there enough frame here to
         * hold a request" -- and the reply's length comes from the UDP header's
         * own length field below, which is authoritative and independent of
         * both. */
        len = ((d->DESC0 & ETH_DMARXDESC_FL) >> 16);

        g_frames++;

        if (len >= (PAY_OFF + ETV_REQ_HDR)
            && get16be(&f[12]) == 0x0800u                 /* IPv4        */
            && f[ETH_HDR + 9] == 17u                      /* UDP         */
            && (f[ETH_HDR] & 0x0Fu) == 5u                 /* no options  */
            && get16be(&f[ETH_HDR + IP_HDR + 2]) == (uint16_t)ETV_PORT
            && get32le(&f[PAY_OFF]) == ETV_MAGIC_REQ
            && f[PAY_OFF + 4] == (uint8_t)ETV_VERSION)
        {
            /* The payload length the sender meant, from its UDP header. */
            uint16_t udp_len = get16be(&f[ETH_HDR + IP_HDR + 4]);
            uint16_t want = (udp_len > UDP_HDR)
                          ? (uint16_t)(udp_len - UDP_HDR) : 0u;
            uint8_t  changed = 0u;

            g_seen++;

            /* Learn the peer, and notice a change. Both are off the hot path
             * in every capture: the instrument's address does not move while
             * it is measuring. */
            if (!peer_known
                || memcmp(peer_mac, &f[6], 6) != 0
                || memcmp(peer_ip, &f[ETH_HDR + 12], 4) != 0
                || peer_port != get16be(&f[ETH_HDR + IP_HDR + 0]))
            {
                memcpy(peer_mac, &f[6], 6);
                memcpy(peer_ip, &f[ETH_HDR + 12], 4);
                peer_port = get16be(&f[ETH_HDR + IP_HDR + 0]);
                peer_known = 1u;
                changed = 1u;
            }
            if (want < ETV_RSP_HDR)   { want = ETV_RSP_HDR; }
            if (want > ETV_MAX_REPLY) { want = ETV_MAX_REPLY; }
            if (changed || reply_len != (uint16_t)(PAY_OFF + want))
            {
                reply_template(want);
            }

            /* The four words that are this exchange's. */
            put32le(&tx_reply[PAY_OFF + 8],  get32le(&f[PAY_OFF + 8]));   /* tag  */
            put32le(&tx_reply[PAY_OFF + 12], ++g_seq);
            put32le(&tx_reply[PAY_OFF + 16], cyc);
            put32le(&tx_reply[PAY_OFF + 28], g_seen);
            tx_reply[PAY_OFF + 5] = (uint8_t)(raw_link_100f() ? ETV_F_LINK_100F : 0u);

            /* Last, and immediately before the descriptor is armed. */
            put32le(&tx_reply[PAY_OFF + 20], DWT->CYCCNT);
            if (get32le(&tx_reply[PAY_OFF + 20]) < cyc)
            {
                tx_reply[PAY_OFF + 5] |= (uint8_t)ETV_F_CYC_WRAP;
            }

            tx_send(tx_reply, reply_len, ETH_DMATXDESC_CIC_IPV4HEADER);
            handled = 1u;
        }
        else if (len >= 42u && get16be(&f[12]) == 0x0806u    /* ARP        */
                 && get16be(&f[20]) == 0x0001u               /* request    */
                 && memcmp(&f[38], MY_IP, 4) == 0)           /* for us     */
        {
            /* Answered here rather than from the main loop so that one place
             * owns the transmit ring. It happens once per capture. */
            uint8_t *r = tx_other;
            memset(r, 0, 42u);
            memcpy(&r[0], &f[6], 6);
            memcpy(&r[6], MY_MAC, 6);
            put16be(&r[12], 0x0806u);
            put16be(&r[14], 0x0001u);          /* Ethernet   */
            put16be(&r[16], 0x0800u);          /* IPv4       */
            r[18] = 6u; r[19] = 4u;
            put16be(&r[20], 0x0002u);          /* reply      */
            memcpy(&r[22], MY_MAC, 6);
            memcpy(&r[28], MY_IP, 4);
            memcpy(&r[32], &f[22], 6);         /* their MAC  */
            memcpy(&r[38], &f[28], 4);         /* their IP   */
            tx_send(r, 42u, 0u);
            g_arp++;
            handled = 1u;
        }
        else
        {
            /* Split by reason, cheaply and in the order the tests above run. */
            g_drop++;
            if (len < (PAY_OFF + ETV_REQ_HDR))              { g_n_short++;  }
            else if (get16be(&f[12]) != 0x0800u)            { g_n_notip++;  }
            else if (f[ETH_HDR + 9] != 17u
                     || (f[ETH_HDR] & 0x0Fu) != 5u)         { g_n_notudp++; }
            else if (get16be(&f[ETH_HDR + IP_HDR + 2])
                     != (uint16_t)ETV_PORT)                 { g_n_port++;   }
            else                                            { g_n_magic++;  }
        }

        /* Give the descriptor back and move on. */
        d->DESC0 = ETH_DMARXDESC_OWN;
        rx_idx = (rx_idx + 1u) % RX_N;
    }

    (void)handled;

    /* Clear what we consumed, and restart the receive DMA if it had run out of
     * descriptors while we were in here. */
    ETH->DMASR   = dmasr & (ETH_DMASR_RS | ETH_DMASR_TS | ETH_DMASR_NIS
                            | ETH_DMASR_AIS | ETH_DMASR_RBUS | ETH_DMASR_TBUS);
    ETH->DMARPDR = 0u;
    return 1;
}

/* ------------------------------------------------------------------ init -- */

/* The PHY's MDIO, through the HAL, as the lwIP port does. */
static int32_t phy_init(void)      { return 0; }
static int32_t phy_deinit(void)    { return 0; }
static int32_t phy_tick(void)      { return (int32_t)HAL_GetTick(); }
static int32_t phy_read(uint32_t a, uint32_t r, uint32_t *v)
{
    return (HAL_ETH_ReadPHYRegister(&EthHandle, a, r, v) == HAL_OK) ? 0 : -1;
}
static int32_t phy_write(uint32_t a, uint32_t r, uint32_t v)
{
    return (HAL_ETH_WritePHYRegister(&EthHandle, a, r, v) == HAL_OK) ? 0 : -1;
}
static lan8742_IOCtx_t phy_io = { phy_init, phy_deinit, phy_write, phy_read,
                                  phy_tick };

static uint8_t link_speed;         /* ET_LINK_* style code, 0 = down */

int raw_link_100f(void) { return link_speed == 4u; }
uint8_t raw_link_speed(void) { return link_speed; }

uint32_t raw_seq(void)  { return g_seq; }
uint32_t raw_seen(void) { return g_seen; }
uint32_t raw_arp(void)  { return g_arp; }
uint32_t raw_drop(void) { return g_drop; }
uint32_t raw_frames(void) { return g_frames; }
void raw_drop_reasons(uint32_t out[5])
{
    out[0] = g_n_short; out[1] = g_n_notip; out[2] = g_n_notudp;
    out[3] = g_n_port;  out[4] = g_n_magic;
}
uint16_t raw_reply_len(void) { return reply_len; }
const uint8_t *raw_my_ip(void) { return MY_IP; }

/* The ETH pins and clocks. The lwIP port has an identical function; this
 * application does not compile that file, so it carries its own. */
void HAL_ETH_MspInit(ETH_HandleTypeDef *heth)
{
    GPIO_InitTypeDef g = {0};
    (void)heth;

    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOG_CLK_ENABLE();

    /* RMII_REF_CLK PA1, RMII_MDIO PA2, RMII_CRS_DV PA7, RMII_TXD1 PB13,
       RMII_MDC PC1, RMII_RXD0 PC4, RMII_RXD1 PC5, RMII_RXER PG2,
       RMII_TX_EN PG11, RMII_TXD0 PG13 */
    g.Speed = GPIO_SPEED_HIGH;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_NOPULL;
    g.Alternate = GPIO_AF11_ETH;

    g.Pin = GPIO_PIN_1 | GPIO_PIN_2 | GPIO_PIN_7;  HAL_GPIO_Init(GPIOA, &g);
    g.Pin = GPIO_PIN_13;                           HAL_GPIO_Init(GPIOB, &g);
    g.Pin = GPIO_PIN_1 | GPIO_PIN_4 | GPIO_PIN_5;  HAL_GPIO_Init(GPIOC, &g);
    g.Pin = GPIO_PIN_2 | GPIO_PIN_11 | GPIO_PIN_13; HAL_GPIO_Init(GPIOG, &g);

    /* Priority 0: nothing on this board may delay the reply, and nothing else
     * on it has anything to do. */
    HAL_NVIC_SetPriority(ETH_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(ETH_IRQn);
    __HAL_RCC_ETH_CLK_ENABLE();
}

/* Build the two rings by hand, chained, with the receive buffers attached. */
static void rings_init(void)
{
    uint32_t i;

    for (i = 0; i < RX_N; i++)
    {
        rx_desc[i].DESC0 = ETH_DMARXDESC_OWN;
        rx_desc[i].DESC1 = ETH_DMARXDESC_RCH | BUF_SZ;
        rx_desc[i].DESC2 = (uint32_t)rx_buf[i];
        rx_desc[i].DESC3 = (uint32_t)&rx_desc[(i + 1u) % RX_N];
    }
    for (i = 0; i < TX_N; i++)
    {
        tx_desc[i].DESC0 = ETH_DMATXDESC_TCH;
        tx_desc[i].DESC1 = 0u;
        tx_desc[i].DESC2 = 0u;
        tx_desc[i].DESC3 = (uint32_t)&tx_desc[(i + 1u) % TX_N];
    }
    rx_idx = 0u;
    tx_idx = 0u;

    ETH->DMARDLAR = (uint32_t)rx_desc;
    ETH->DMATDLAR = (uint32_t)tx_desc;
}

int raw_init(void)
{
    uint8_t mac[6];
    memcpy(mac, MY_MAC, 6);

    EthHandle.Instance = ETH;
    EthHandle.Init.MACAddr = mac;
    EthHandle.Init.MediaInterface = HAL_ETH_RMII_MODE;
    EthHandle.Init.RxDesc = rx_desc;
    EthHandle.Init.TxDesc = tx_desc;
    EthHandle.Init.RxBuffLen = BUF_SZ;

    if (HAL_ETH_Init(&EthHandle) != HAL_OK) { return -1; }

    /* HAL_ETH_Init set the MAC up and wrote its own descriptor lists; replace
     * them with ours. From here the HAL is never asked to move a frame. */
    rings_init();

    if (LAN8742_RegisterBusIO(&LAN8742, &phy_io) != LAN8742_STATUS_OK) { return -2; }
    if (LAN8742_Init(&LAN8742) != LAN8742_STATUS_OK) { return -3; }

    peer_known = 0u;
    memset(peer_mac, 0xFF, 6);          /* broadcast until a request arrives */
    memset(peer_ip, 0xFF, 4);
    peer_port = (uint16_t)ETV_PORT;
    reply_template(ETV_RSP_HDR);

    return 0;
}

/* Poll the PHY and start or stop the MAC to match. Rate-limited by the caller;
 * it costs MDIO transactions, which are tens of microseconds. */
void raw_link_tick(void)
{
    int32_t st = LAN8742_GetLinkState(&LAN8742);
    uint8_t now = 0u;
    ETH_MACConfigTypeDef cfg = {0};

    switch (st)
    {
        case LAN8742_STATUS_100MBITS_FULLDUPLEX: now = 4u; break;
        case LAN8742_STATUS_100MBITS_HALFDUPLEX: now = 3u; break;
        case LAN8742_STATUS_10MBITS_FULLDUPLEX:  now = 2u; break;
        case LAN8742_STATUS_10MBITS_HALFDUPLEX:  now = 1u; break;
        default:                                 now = 0u; break;
    }
    if (now == link_speed) { return; }

    if (now == 0u)
    {
        HAL_ETH_Stop_IT(&EthHandle);
    }
    else
    {
        HAL_ETH_GetMACConfig(&EthHandle, &cfg);
        cfg.DuplexMode = (now == 4u || now == 2u) ? ETH_FULLDUPLEX_MODE
                                                  : ETH_HALFDUPLEX_MODE;
        cfg.Speed = (now >= 3u) ? ETH_SPEED_100M : ETH_SPEED_10M;
        HAL_ETH_SetMACConfig(&EthHandle, &cfg);
        if (link_speed == 0u)
        {
            /* HAL_ETH_Start_IT would install the HAL's own descriptor
             * bookkeeping over ours, so the rings are rebuilt after it. */
            HAL_ETH_Start_IT(&EthHandle);
            rings_init();
            ETH->DMAOMR |= ETH_DMAOMR_SR | ETH_DMAOMR_ST;
            ETH->DMAIER |= ETH_DMAIER_NISE | ETH_DMAIER_RIE;
        }
    }
    link_speed = now;
}
