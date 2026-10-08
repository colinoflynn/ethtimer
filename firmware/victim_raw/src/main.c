/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* The bare-metal responder's entry point.
 *
 * THE MAIN LOOP DOES NOT ANSWER ANYTHING. Every reply is built and sent inside
 * the Ethernet receive interrupt (see rawnet.c), so this loop is housekeeping
 * only: poll the PHY at 10 Hz, and print a line when the link changes. If it
 * stopped running altogether the responder would keep answering.
 *
 * That is the difference from the lwIP responder, whose loop IS the response
 * path. It is also why this file is shorter than that one's comment about why
 * it is not.
 *
 * Nothing here reads the console. There are no commands -- a command channel is
 * something that could run while an exchange is being measured -- and the
 * responder's address is a build-time constant for the same reason.
 */
#include "board.h"
#include "et_victim.h"
#include "rawnet.h"

static void banner(void)
{
    const uint8_t *ip = raw_my_ip();
    int i;

    if (!board_dwt_ok())
    {
        /* Loud and first: every interval this responder reports would be zero,
         * and zero is not an error downstream -- it is an endpoint that appears
         * to contribute nothing, so a path figure silently becomes the whole
         * round trip. See dwt_enable() in the board's board.c. */
        uart_puts("\r\n*** CYCLE COUNTER NOT RUNNING: every reported interval "
                  "will be 0 and every path figure will be the whole round "
                  "trip. Power-cycle the board. ***\r\n");
    }

    uart_puts("\r\n" BOARD_NAME " ethtimer reference responder v");
    uart_putdec(ETV_VERSION);
    uart_puts(" (bare metal, no lwIP)\r\n  UDP port ");
    uart_putdec(ETV_PORT);
    uart_puts(", cycle counter ");
    uart_putdec(board_cyccnt_hz() / 1000000u);
    uart_puts(" MHz\r\n  address ");
    for (i = 0; i < 4; i++)
    {
        uart_putdec(ip[i]);
        uart_puts(i < 3 ? "." : "\r\n");
    }
    uart_puts("  the reply is built in the receive interrupt; this loop only "
              "watches the link\r\n");
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

int main(void)
{
    uint8_t  last_link = 0xFFu;
    uint32_t last_tick = 0u;
    int rc;

    /* The clock tree and console from the board support, shared with the
     * instrument. The lwIP half of that file is never called and is dropped by
     * --gc-sections. */
    board_clock_console_init();

    rc = raw_init();
    if (rc != 0)
    {
        uart_puts("\r\n*** raw_init failed: ");
        uart_putdec((uint32_t)(-rc));
        uart_puts(" (1 = HAL_ETH_Init, 2 = PHY bus, 3 = PHY init) ***\r\n");
        for (;;) { }
    }

    banner();

    for (;;)
    {
        uint32_t now = HAL_GetTick();

        /* 10 Hz, because it costs MDIO. Never inside an exchange: the exchange
         * is handled in the interrupt and does not come through here at all,
         * which is the one real advantage this responder has over the other. */
        if ((now - last_tick) >= 100u)
        {
            uint8_t link;

            last_tick = now;
            raw_link_tick();

            /* The cycle counter can stop, and a stopped counter reports every
             * interval as exactly zero -- which is not an error, it is an
             * endpoint that appears to contribute nothing. The instrument gets
             * this from board_link_tick(); this application does not call that
             * one, and spent an afternoon reporting zeroes before this line
             * existed. See board_dwt_tick() in the board's board.c. */
            board_dwt_tick();

            link = raw_link_speed();
            if (link != last_link)
            {
                /* Edge-triggered. A periodic print would put a UART transmit
                 * into somebody's jitter histogram at regular intervals, which
                 * is a pattern that looks exactly like a path artefact -- and
                 * the thing worth knowing is not the link's state, it is that
                 * it CHANGED, because a link that renegotiated mid-capture
                 * explains a distribution with two modes in it. */
                last_link = link;
                uart_puts("victim: link ");
                uart_puts(speed_name(link));
                uart_puts("\r\n");
            }

            /* Counters, ON REQUEST ONLY -- never on a timer.
             *
             * THIS LINE USED TO PRINT ONCE A SECOND whenever frames were
             * moving, which during a capture is always, and it CORRUPTED THE
             * MEASUREMENT. A ninety-character line at 921 600 baud is about a
             * millisecond, and a group of fifteen exchanges at a 50 us gap is
             * about 0.8 ms -- so a whole group could land inside one print and
             * read 60 cycles high. The interrupt is not delayed by a UART poll,
             * but the instruction cache is: the main loop thrashing flash
             * evicts the handler's own code.
             *
             * The symptom was a password scan that picked a candidate whose
             * entire group was slow, which no amount of trimming inside a group
             * can fix, and then reported five positions of "no separation". The
             * diagnostic added to understand the measurement was perturbing it.
             *
             * So it is a reply to an explicit ETV_CMD_STATUS request now, like
             * the lwIP responder's. Ask for it when you want it; nothing is
             * printed while measuring. */
            {
                static uint32_t served;
                uint32_t asked = raw_status_req();

                if (asked != served)
                {
                    uint32_t why[5];

                    served = asked;
                    raw_drop_reasons(why);
                    uart_puts("victim: frames="); uart_putdec(raw_frames());
                    uart_puts(" seen=");          uart_putdec(raw_seen());
                    uart_puts(" sent=");          uart_putdec(raw_seq());
                    uart_puts(" pw=");            uart_putdec(raw_pw());
                    uart_puts(" arp=");           uart_putdec(raw_arp());
                    uart_puts(" drop=");          uart_putdec(raw_drop());
                    uart_puts(" (short=");        uart_putdec(why[0]);
                    uart_puts(" notip=");         uart_putdec(why[1]);
                    uart_puts(" notudp=");        uart_putdec(why[2]);
                    uart_puts(" port=");          uart_putdec(why[3]);
                    uart_puts(" magic=");         uart_putdec(why[4]);
                    uart_puts(")\r\n");
                }
            }
        }
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line) { (void)file; (void)line; while (1) {} }
#endif
